"""Shared SLC/TLC storage manager."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from dwpdsim.config import SimulationConfig
from dwpdsim.errors import (
    BlockNotFoundError,
    InvalidPolicyDecisionError,
    StorageCapacityError,
)
from dwpdsim.managers.ssd import SSDManager
from dwpdsim.models import (
    BlockId,
    Medium,
    Placement,
    PlacementContext,
    StorageBlockView,
    StorageView,
)
from dwpdsim.policies.gc import GCPolicy
from dwpdsim.policies.placement import FixedPlacementPolicy, PlacementPolicy
from dwpdsim.policies.storage_eviction import (
    LRUStorageEvictionPolicy,
    StorageEvictionPolicy,
)


@dataclass(slots=True)
class _StorageEntry:
    medium: Medium
    stream_id: int
    insert_order: int
    last_access_order: int


class StorageManager:
    """Own the unique persistent location of every block."""

    def __init__(
        self,
        config: SimulationConfig,
        placement_policy: PlacementPolicy | None = None,
        eviction_policy: StorageEvictionPolicy | None = None,
        slc_gc_policy: GCPolicy | None = None,
        tlc_gc_policy: GCPolicy | None = None,
    ) -> None:
        self.config = config
        self.placement_policy = (
            placement_policy
            if placement_policy is not None
            else FixedPlacementPolicy(Medium.TLC, 0)
        )
        self.eviction_policy = (
            eviction_policy if eviction_policy is not None else LRUStorageEvictionPolicy()
        )
        self.slc = SSDManager(
            Medium.SLC,
            config.slc,
            config.block_size_bytes,
            slc_gc_policy,
        )
        self.tlc = SSDManager(
            Medium.TLC,
            config.tlc,
            config.block_size_bytes,
            tlc_gc_policy,
        )
        self._entries: dict[BlockId, _StorageEntry] = {}
        self._order = 0
        self._writes_from_dram: Counter[Medium] = Counter()
        self._transfers: Counter[tuple[Medium, Medium]] = Counter()

    @property
    def blocks(self) -> frozenset[BlockId]:
        return frozenset(self._entries)

    @property
    def writes_from_dram(self) -> dict[Medium, int]:
        return {medium: self._writes_from_dram[medium] for medium in Medium}

    @property
    def transfers(self) -> dict[tuple[Medium, Medium], int]:
        return {
            (Medium.SLC, Medium.TLC): self._transfers[(Medium.SLC, Medium.TLC)],
            (Medium.TLC, Medium.SLC): self._transfers[(Medium.TLC, Medium.SLC)],
        }

    def contains(self, block_id: BlockId) -> bool:
        return block_id in self._entries

    def lookup(self, block_id: BlockId) -> Medium | None:
        entry = self._entries.get(block_id)
        return entry.medium if entry is not None else None

    def medium_of(self, block_id: BlockId) -> Medium:
        try:
            return self._entries[block_id].medium
        except KeyError as error:
            raise BlockNotFoundError(f"block not found in storage: {block_id}") from error

    def stream_of(self, block_id: BlockId) -> int:
        try:
            return self._entries[block_id].stream_id
        except KeyError as error:
            raise BlockNotFoundError(f"block not found in storage: {block_id}") from error

    def blocks_in(self, medium: Medium) -> frozenset[BlockId]:
        return frozenset(
            block_id for block_id, entry in self._entries.items() if entry.medium is medium
        )

    def view(self) -> StorageView:
        return StorageView(slc=self.slc.view(), tlc=self.tlc.view())

    def read(self, block_id: BlockId) -> Medium:
        """Read and touch a block without changing its medium."""

        try:
            entry = self._entries[block_id]
        except KeyError as error:
            raise BlockNotFoundError(f"block not found in storage: {block_id}") from error
        self._order += 1
        entry.last_access_order = self._order
        return entry.medium

    def seed(self, block_id: BlockId, medium: Medium, stream_id: int = 0) -> None:
        """Add initial storage state without counting a simulated write."""

        if block_id in self._entries:
            raise ValueError(f"block already exists in storage: {block_id}")
        ssd = self._ssd(medium)
        if ssd.used_blocks >= ssd.capacity_blocks or not ssd.can_program(stream_id):
            raise StorageCapacityError(f"cannot seed block into {medium.value}: {block_id}")
        ssd.seed(block_id, stream_id)
        self._add_entry(block_id, medium, stream_id)

    def write_from_dram(self, context: PlacementContext) -> Placement | None:
        """Place an unbacked DRAM victim or drop it."""

        if context.block_id in self._entries:
            raise ValueError(f"block already exists in storage: {context.block_id}")
        placement = self.placement_policy.choose(context, self.view())
        if placement is None:
            return None
        self._validate_placement(placement)

        self._make_space(placement, context.block_id)
        self._ssd(placement.medium).program(context.block_id, placement.stream_id)
        self._add_entry(context.block_id, placement.medium, placement.stream_id)
        self._writes_from_dram[placement.medium] += 1
        return placement

    def transfer(
        self,
        block_id: BlockId,
        target_medium: Medium,
        target_stream: int,
    ) -> Placement:
        """Move one block to the explicitly requested peer medium."""

        source_medium = self.medium_of(block_id)
        if not isinstance(target_medium, Medium) or target_medium is source_medium:
            raise InvalidPolicyDecisionError(
                f"transfer target must be the other medium: {target_medium!r}"
            )
        placement = Placement(target_medium, target_stream)
        self._validate_placement(placement)

        source_ssd = self._ssd(source_medium)
        source_delete = source_ssd.prepare_delete(block_id)
        self._make_space(placement, block_id)

        self._ssd(target_medium).program(block_id, target_stream)
        self._add_entry(block_id, target_medium, target_stream)
        source_ssd.delete(block_id, source_delete)
        self._transfers[(source_medium, target_medium)] += 1
        return placement

    def remove(self, block_id: BlockId) -> None:
        """Delete one persistent block and run immediate GC if needed."""

        medium = self.medium_of(block_id)
        ssd = self._ssd(medium)
        relocation_order = ssd.prepare_delete(block_id)
        ssd.delete(block_id, relocation_order)
        del self._entries[block_id]

    def _make_space(self, placement: Placement, incoming_block_id: BlockId) -> None:
        ssd = self._ssd(placement.medium)
        lacks_logical_space = ssd.used_blocks >= ssd.capacity_blocks
        lacks_physical_space = not ssd.can_program(placement.stream_id)
        if not lacks_logical_space and not lacks_physical_space:
            return

        candidates = tuple(
            self._block_view(block_id, entry)
            for block_id, entry in self._entries.items()
            if entry.medium is placement.medium
            and (
                not lacks_physical_space
                or ssd.deletion_opens_write_slot(block_id, placement.stream_id)
            )
        )
        if not candidates:
            raise StorageCapacityError(
                f"{placement.medium.value} has no removable block for stream {placement.stream_id}"
            )

        victim = self.eviction_policy.choose(
            placement.medium,
            placement.stream_id,
            incoming_block_id,
            candidates,
        )
        candidate_ids = {candidate.block_id for candidate in candidates}
        if victim not in candidate_ids:
            raise InvalidPolicyDecisionError(
                f"StorageEvictionPolicy selected invalid block: {victim!r}"
            )

        relocation_order = ssd.prepare_delete(victim)
        ssd.delete(victim, relocation_order)
        del self._entries[victim]

    def _add_entry(self, block_id: BlockId, medium: Medium, stream_id: int) -> None:
        self._order += 1
        self._entries[block_id] = _StorageEntry(
            medium=medium,
            stream_id=stream_id,
            insert_order=self._order,
            last_access_order=self._order,
        )

    @staticmethod
    def _block_view(block_id: BlockId, entry: _StorageEntry) -> StorageBlockView:
        return StorageBlockView(
            block_id=block_id,
            medium=entry.medium,
            stream_id=entry.stream_id,
            insert_order=entry.insert_order,
            last_access_order=entry.last_access_order,
        )

    def _ssd(self, medium: Medium) -> SSDManager:
        if not isinstance(medium, Medium):
            raise InvalidPolicyDecisionError(f"invalid storage medium: {medium!r}")
        return self.slc if medium is Medium.SLC else self.tlc

    def _validate_placement(self, placement: Placement) -> None:
        if not isinstance(placement, Placement):
            raise InvalidPolicyDecisionError(
                f"PlacementPolicy must return Placement or None: {placement!r}"
            )
        self._ssd(placement.medium).free_slots(placement.stream_id)
