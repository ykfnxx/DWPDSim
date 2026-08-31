"""Small immutable data objects shared by simulator components."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

BlockId: TypeAlias = int
Timestamp: TypeAlias = int | float


class Medium(str, Enum):
    """Persistent SSD media modeled by the simulator."""

    SLC = "slc"
    TLC = "tlc"


class ChunkState(str, Enum):
    """Lifecycle state of one SSD chunk."""

    FREE = "free"
    ACTIVE = "active"
    SEALED = "sealed"


class AccessResult(str, Enum):
    """Final lookup outcome for one input position."""

    DRAM_HIT = "dram_hit"
    SLC_HIT = "slc_hit"
    TLC_HIT = "tlc_hit"
    GLOBAL_MISS = "global_miss"


@dataclass(frozen=True, slots=True)
class Query:
    """One timestamped, ordered sequence of block hashes."""

    timestamp: Timestamp
    hash_ids: tuple[BlockId, ...]
    other_info: object | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "hash_ids", tuple(self.hash_ids))


@dataclass(frozen=True, slots=True)
class BlockHistory:
    """Read-only access history for one block."""

    first_seen_timestamp: Timestamp
    last_seen_timestamp: Timestamp
    access_count: int


@dataclass(frozen=True, slots=True)
class SequenceNodeView:
    """Read-only view of one forward-prefix node."""

    node_id: int
    parent_id: int | None
    block_id: BlockId | None
    depth: int
    access_count: int


@dataclass(frozen=True, slots=True)
class AccessContext:
    """Sequence context for one input position."""

    query: Query
    position: int
    block_id: BlockId
    history: BlockHistory | None
    parent_node: SequenceNodeView
    prefix_node: SequenceNodeView | None


@dataclass(frozen=True, slots=True)
class Placement:
    """Persistent destination selected for one block."""

    medium: Medium
    stream_id: int


@dataclass(frozen=True, slots=True)
class PlacementContext:
    """Context used to place a DRAM-evicted block."""

    block_id: BlockId
    history: BlockHistory | None
    trigger: AccessContext


@dataclass(frozen=True, slots=True)
class DramBlockView:
    """Read-only DRAM metadata for one resident block."""

    block_id: BlockId
    insert_order: int
    last_access_order: int


@dataclass(frozen=True, slots=True)
class DramView:
    """Read-only view of current DRAM state."""

    capacity_blocks: int
    blocks: Mapping[BlockId, DramBlockView]

    @property
    def used_blocks(self) -> int:
        return len(self.blocks)

    @property
    def free_blocks(self) -> int:
        return self.capacity_blocks - self.used_blocks


@dataclass(frozen=True, slots=True)
class StorageBlockView:
    """Read-only persistent metadata for one block."""

    block_id: BlockId
    medium: Medium
    stream_id: int
    insert_order: int
    last_access_order: int


@dataclass(frozen=True, slots=True)
class MediumView:
    """Read-only capacity and stream state for one SSD medium."""

    medium: Medium
    capacity_blocks: int
    used_blocks: int
    stream_free_slots: tuple[int, ...]

    @property
    def free_blocks(self) -> int:
        return self.capacity_blocks - self.used_blocks


@dataclass(frozen=True, slots=True)
class StorageView:
    """Read-only state of the two persistent media."""

    slc: MediumView
    tlc: MediumView


@dataclass(frozen=True, slots=True)
class ChunkView:
    """Read-only logical contents of one SSD chunk."""

    chunk_id: int
    state: ChunkState
    stream_id: int | None
    slots: tuple[BlockId | None, ...]
    erase_count: int = 0
