"""Chunk and stream state for one SSD medium."""

from __future__ import annotations

from dataclasses import dataclass, field

from dwpdsim.config import SSDConfig
from dwpdsim.errors import (
    BlockNotFoundError,
    InvalidPolicyDecisionError,
    StorageCapacityError,
)
from dwpdsim.models import BlockId, ChunkState, ChunkView, Medium, MediumView
from dwpdsim.policies.gc import GCPolicy, NaiveGCPolicy


@dataclass(frozen=True, slots=True)
class BlockLocation:
    chunk_id: int
    slot: int
    stream_id: int


@dataclass(slots=True)
class _Chunk:
    chunk_id: int
    slots_per_chunk: int
    state: ChunkState = ChunkState.FREE
    stream_id: int | None = None
    slots: list[BlockId | None] = field(init=False)
    erase_count: int = 0

    def __post_init__(self) -> None:
        self.slots = [None] * self.slots_per_chunk

    def view(self) -> ChunkView:
        return ChunkView(
            chunk_id=self.chunk_id,
            state=self.state,
            stream_id=self.stream_id,
            slots=tuple(self.slots),
            erase_count=self.erase_count,
        )


class SSDManager:
    """Own the chunks, streams, and block locations of one medium."""

    def __init__(
        self,
        medium: Medium,
        config: SSDConfig,
        block_size_bytes: int,
        gc_policy: GCPolicy | None = None,
    ) -> None:
        self.medium = medium
        self.config = config
        self.block_size_bytes = block_size_bytes
        self.slots_per_chunk = config.chunk_size_bytes // block_size_bytes
        self._gc_policy = gc_policy if gc_policy is not None else NaiveGCPolicy()
        self._chunks = [
            _Chunk(chunk_id, self.slots_per_chunk) for chunk_id in range(config.chunk_count)
        ]
        self._active_chunks: dict[int, int] = {}
        self._locations: dict[BlockId, BlockLocation] = {}
        self._logical_writes_by_stream = [0] * config.stream_count
        self.whole_erase_count = 0
        self.non_full_erase_count = 0

    @property
    def capacity_blocks(self) -> int:
        return (self.config.chunk_count - self.config.gc_reserve_chunks) * self.slots_per_chunk

    @property
    def used_blocks(self) -> int:
        return len(self._locations)

    @property
    def active_chunk_ids(self) -> dict[int, int]:
        return dict(self._active_chunks)

    @property
    def chunks(self) -> tuple[ChunkView, ...]:
        return tuple(chunk.view() for chunk in self._chunks)

    @property
    def logical_writes_by_stream(self) -> tuple[int, ...]:
        return tuple(self._logical_writes_by_stream)

    def contains(self, block_id: BlockId) -> bool:
        return block_id in self._locations

    def location(self, block_id: BlockId) -> BlockLocation:
        try:
            return self._locations[block_id]
        except KeyError as error:
            raise BlockNotFoundError(
                f"block not found in {self.medium.value}: {block_id}"
            ) from error

    def stream_of(self, block_id: BlockId) -> int:
        return self.location(block_id).stream_id

    def view(self) -> MediumView:
        return MediumView(
            medium=self.medium,
            capacity_blocks=self.capacity_blocks,
            used_blocks=self.used_blocks,
            stream_free_slots=tuple(
                self.free_slots(stream_id) for stream_id in range(self.config.stream_count)
            ),
        )

    def free_slots(self, stream_id: int) -> int:
        self._validate_stream(stream_id)
        chunk_id = self._active_chunks.get(stream_id)
        if chunk_id is not None:
            return self._chunks[chunk_id].slots.count(None)
        return self.slots_per_chunk if self._can_allocate_normal(stream_id) else 0

    def can_program(self, stream_id: int) -> bool:
        return self.free_slots(stream_id) > 0

    def deletion_opens_write_slot(self, block_id: BlockId, stream_id: int) -> bool:
        """Return whether deleting this block makes the target stream writable."""

        location = self.location(block_id)
        if location.stream_id == stream_id:
            return True
        chunk = self._chunks[location.chunk_id]
        return sum(value is not None for value in chunk.slots) == 1

    def seed(self, block_id: BlockId, stream_id: int) -> BlockLocation:
        return self.program(block_id, stream_id, count_logical_write=False)

    def program(
        self,
        block_id: BlockId,
        stream_id: int,
        *,
        count_logical_write: bool = True,
    ) -> BlockLocation:
        """Append one block to the active chunk of a stream."""

        if block_id in self._locations:
            raise ValueError(f"block already exists in {self.medium.value}: {block_id}")
        self._validate_stream(stream_id)
        location = self._append(block_id, stream_id, allow_reserve=False)
        if count_logical_write:
            self._logical_writes_by_stream[stream_id] += 1
        return location

    def prepare_delete(self, block_id: BlockId) -> tuple[BlockId, ...]:
        """Validate the GC choice before changing the chunk."""

        location = self.location(block_id)
        chunk = self._chunks[location.chunk_id]
        remaining_slots = tuple(
            None if slot == location.slot else value for slot, value in enumerate(chunk.slots)
        )
        remaining = tuple(value for value in remaining_slots if value is not None)
        if not remaining:
            return ()

        view = ChunkView(
            chunk_id=chunk.chunk_id,
            state=chunk.state,
            stream_id=chunk.stream_id,
            slots=remaining_slots,
            erase_count=chunk.erase_count,
        )
        order = tuple(self._gc_policy.relocation_order(view))
        if len(order) != len(remaining) or set(order) != set(remaining):
            raise InvalidPolicyDecisionError(
                "GCPolicy must return every valid block in the chunk exactly once"
            )
        return order

    def delete(
        self,
        block_id: BlockId,
        relocation_order: tuple[BlockId, ...] | None = None,
    ) -> None:
        """Invalidate one block and immediately erase or compact its chunk."""

        if relocation_order is None:
            relocation_order = self.prepare_delete(block_id)

        location = self.location(block_id)
        chunk = self._chunks[location.chunk_id]
        stream_id = location.stream_id

        del self._locations[block_id]
        chunk.slots[location.slot] = None

        if not relocation_order:
            self._erase(chunk, whole=True)
            return

        for moved_block in relocation_order:
            moved_location = self._locations.pop(moved_block)
            chunk.slots[moved_location.slot] = None

        if self._active_chunks.get(stream_id) == chunk.chunk_id:
            del self._active_chunks[stream_id]
            chunk.state = ChunkState.SEALED

        for moved_block in relocation_order:
            self._append(moved_block, stream_id, allow_reserve=True)

        self._erase(chunk, whole=False)

    def _append(self, block_id: BlockId, stream_id: int, *, allow_reserve: bool) -> BlockLocation:
        chunk_id = self._active_chunks.get(stream_id)
        if chunk_id is None:
            chunk_id = self._allocate_chunk(stream_id, allow_reserve=allow_reserve)

        chunk = self._chunks[chunk_id]
        slot = chunk.slots.index(None)
        chunk.slots[slot] = block_id
        location = BlockLocation(chunk_id=chunk_id, slot=slot, stream_id=stream_id)
        self._locations[block_id] = location

        if None not in chunk.slots:
            chunk.state = ChunkState.SEALED
            del self._active_chunks[stream_id]
        return location

    def _allocate_chunk(self, stream_id: int, *, allow_reserve: bool) -> int:
        free_chunks = [chunk for chunk in self._chunks if chunk.state is ChunkState.FREE]
        if not free_chunks or (not allow_reserve and not self._can_allocate_normal(stream_id)):
            raise StorageCapacityError(
                f"{self.medium.value} has no writable chunk for stream {stream_id}"
            )

        chunk = free_chunks[0]
        chunk.state = ChunkState.ACTIVE
        chunk.stream_id = stream_id
        self._active_chunks[stream_id] = chunk.chunk_id
        return chunk.chunk_id

    def _can_allocate_normal(self, stream_id: int) -> bool:
        del stream_id
        free_count = sum(chunk.state is ChunkState.FREE for chunk in self._chunks)
        return free_count > self.config.gc_reserve_chunks

    def _erase(self, chunk: _Chunk, *, whole: bool) -> None:
        if (
            chunk.stream_id is not None
            and self._active_chunks.get(chunk.stream_id) == chunk.chunk_id
        ):
            del self._active_chunks[chunk.stream_id]
        chunk.slots = [None] * self.slots_per_chunk
        chunk.state = ChunkState.FREE
        chunk.stream_id = None
        chunk.erase_count += 1
        if whole:
            self.whole_erase_count += 1
        else:
            self.non_full_erase_count += 1

    def _validate_stream(self, stream_id: int) -> None:
        if not isinstance(stream_id, int) or not 0 <= stream_id < self.config.stream_count:
            raise InvalidPolicyDecisionError(f"invalid {self.medium.value} stream: {stream_id!r}")
