"""Core immutable data models shared across simulator components."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

BlockId: TypeAlias = int
Timestamp: TypeAlias = int


class StorageTier(str, Enum):
    """Persistent storage tiers modeled by DWPDSim."""

    TLC = "tlc"
    QLC = "qlc"


class IOOperation(str, Enum):
    """Physical I/O operation type."""

    READ = "read"
    WRITE = "write"


class IOReason(str, Enum):
    """Reason a physical I/O operation occurred."""

    DEMAND = "demand"
    PROMOTION = "promotion"
    DEMOTION = "demotion"
    WRITEBACK = "writeback"


class StorageRequestType(str, Enum):
    """Logical request that caused a storage placement decision."""

    READ = "read"
    WRITEBACK = "writeback"


@dataclass(frozen=True, slots=True)
class Query:
    """One timestamped, ordered sequence of block accesses."""

    timestamp: Timestamp
    block_ids: tuple[BlockId, ...]
    query_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "block_ids", tuple(self.block_ids))


@dataclass(frozen=True, slots=True)
class AccessContext:
    """Policy context for one block access within a query."""

    block_id: BlockId
    timestamp: Timestamp
    ttl: int | None = None
    query: Query | None = None
    block_index: int | None = None

    @classmethod
    def from_query(
        cls,
        query: Query,
        block_index: int,
        ttl: int | None = None,
    ) -> AccessContext:
        if block_index < 0:
            raise ValueError(f"block_index out of range: {block_index}")
        try:
            block_id = query.block_ids[block_index]
        except IndexError as error:
            raise ValueError(f"block_index out of range: {block_index}") from error
        return cls(
            block_id=block_id,
            timestamp=query.timestamp,
            ttl=ttl,
            query=query,
            block_index=block_index,
        )

    @classmethod
    def for_block(
        cls,
        block_id: BlockId,
        timestamp: Timestamp = 0,
        ttl: int | None = None,
    ) -> AccessContext:
        return cls(block_id=block_id, timestamp=timestamp, ttl=ttl)


@dataclass(frozen=True, slots=True)
class CapacitySnapshot:
    """Read-only view of one tier's current capacity state."""

    capacity_blocks: int
    used_blocks: int

    @property
    def free_blocks(self) -> int:
        return self.capacity_blocks - self.used_blocks

    @property
    def is_full(self) -> bool:
        return self.used_blocks >= self.capacity_blocks

    @property
    def utilization(self) -> float:
        return self.used_blocks / self.capacity_blocks


@dataclass(frozen=True, slots=True)
class IOEvent:
    """One physical I/O event emitted by storage."""

    tier: StorageTier
    operation: IOOperation
    reason: IOReason
    block_id: BlockId
    timestamp: Timestamp
    block_count: int = 1

    def __post_init__(self) -> None:
        if self.block_count <= 0:
            raise ValueError("block_count must be positive")


@dataclass(frozen=True, slots=True)
class MemAccessResult:
    """Result of looking up one block in DRAM."""

    block_id: BlockId
    hit: bool


@dataclass(frozen=True, slots=True)
class MemAdmissionResult:
    """Result of attempting to admit one block into DRAM."""

    block_id: BlockId
    admitted: bool
    evicted_block: BlockId | None = None
    writeback: StorageWriteResult | None = None


@dataclass(frozen=True, slots=True)
class StorageAccessResult:
    """Storage lookup, placement decision, and resulting physical I/O."""

    block_id: BlockId
    source_tier: StorageTier
    requested_tier: StorageTier
    final_tier: StorageTier
    decision_reason: str
    placement_rejected: bool
    io_events: tuple[IOEvent, ...]


@dataclass(frozen=True, slots=True)
class StorageWriteResult:
    """Result of writing a DRAM-evicted block into persistent storage."""

    block_id: BlockId
    source_tier: StorageTier | None
    requested_tier: StorageTier
    final_tier: StorageTier
    decision_reason: str
    placement_rejected: bool
    io_events: tuple[IOEvent, ...]


@dataclass(frozen=True, slots=True)
class BlockAccessResult:
    """Complete result for one block in a query."""

    block_id: BlockId
    memory: MemAccessResult
    storage: StorageAccessResult | None = None
    admission: MemAdmissionResult | None = None
    inserted_on_storage_miss: bool = False


@dataclass(frozen=True, slots=True)
class MemQueryResult:
    """Ordered block-level outcomes for one query."""

    query: Query
    block_results: tuple[BlockAccessResult, ...]

    @property
    def memory_hits(self) -> int:
        return sum(result.memory.hit for result in self.block_results)

    @property
    def memory_misses(self) -> int:
        return len(self.block_results) - self.memory_hits

    @property
    def insertions(self) -> int:
        """Count blocks inserted directly into DRAM after a storage miss."""

        return sum(result.inserted_on_storage_miss for result in self.block_results)
