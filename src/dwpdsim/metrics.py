"""Central aggregation of hit-rate, I/O, and capacity metrics."""

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from dwpdsim.models import (
    IOEvent,
    IOOperation,
    IOReason,
    MemAdmissionResult,
    MemQueryResult,
    Query,
    StorageAccessResult,
    StorageTier,
)


@dataclass(frozen=True, slots=True)
class IOCount:
    """Aggregated physical I/O for one tier/operation/reason combination."""

    tier: StorageTier
    operation: IOOperation
    reason: IOReason
    operations: int
    blocks: int
    bytes: int


@dataclass(frozen=True, slots=True)
class SimulationMetrics:
    """Immutable counter snapshot for a simulation."""

    query_count: int
    block_access_count: int
    dram_hits: int
    dram_misses: int
    tlc_accesses: int
    qlc_accesses: int
    dram_evictions: int
    placement_rejections: int
    io_counts: tuple[IOCount, ...]

    @property
    def dram_hit_rate(self) -> float:
        return self.dram_hits / self.block_access_count if self.block_access_count else 0.0

    @property
    def tlc_hit_rate_on_dram_miss(self) -> float:
        return self.tlc_accesses / self.dram_misses if self.dram_misses else 0.0

    @property
    def qlc_hit_rate_on_dram_miss(self) -> float:
        return self.qlc_accesses / self.dram_misses if self.dram_misses else 0.0

    def io_operations(
        self,
        tier: StorageTier,
        operation: IOOperation,
        reason: IOReason | None = None,
    ) -> int:
        """Return operation count, optionally filtered by I/O reason."""

        return sum(
            count.operations
            for count in self.io_counts
            if count.tier is tier
            and count.operation is operation
            and (reason is None or count.reason is reason)
        )


@dataclass(frozen=True, slots=True)
class TierUsage:
    """Capacity and eviction information for one hierarchy tier."""

    capacity_blocks: int
    used_blocks: int
    peak_used_blocks: int
    eviction_count: int

    @property
    def utilization(self) -> float:
        return self.used_blocks / self.capacity_blocks


@dataclass(frozen=True, slots=True)
class SimulationReport:
    """Final metrics and tier capacity states."""

    metrics: SimulationMetrics
    dram: TierUsage
    tlc: TierUsage
    qlc: TierUsage


class MetricsCollector:
    """Accumulates structured query results without owning cache state."""

    def __init__(self, block_size_bytes: int) -> None:
        if block_size_bytes <= 0:
            raise ValueError("block_size_bytes must be positive")
        self._block_size_bytes = block_size_bytes
        self._query_count = 0
        self._block_access_count = 0
        self._dram_hits = 0
        self._dram_misses = 0
        self._tlc_accesses = 0
        self._qlc_accesses = 0
        self._dram_evictions = 0
        self._placement_rejections = 0
        self._io_operations: Counter[tuple[StorageTier, IOOperation, IOReason]] = Counter()
        self._io_blocks: Counter[tuple[StorageTier, IOOperation, IOReason]] = Counter()

    def record_query(self, result: MemQueryResult) -> None:
        """Record one completed query result."""

        self.record_query_start(result.query)
        for block_result in result.block_results:
            if block_result.memory.hit:
                self.record_memory_hit()
                continue

            storage_result = block_result.storage
            if storage_result is None:
                raise ValueError("DRAM miss must include a storage result")
            self.record_memory_miss(storage_result, block_result.admission)

    def record_query_start(self, query: Query) -> None:
        """Record one query without retaining it."""

        del query
        self._query_count += 1

    def record_memory_hit(self) -> None:
        """Record one DRAM hit."""

        self._block_access_count += 1
        self._dram_hits += 1

    def record_memory_miss(
        self,
        storage_result: StorageAccessResult,
        admission_result: MemAdmissionResult | None,
    ) -> None:
        """Record one DRAM miss and all I/O caused by it."""

        self._block_access_count += 1
        self._dram_misses += 1
        if storage_result.source_tier is StorageTier.TLC:
            self._tlc_accesses += 1
        else:
            self._qlc_accesses += 1

        if storage_result.placement_rejected:
            self._placement_rejections += 1

        if admission_result is not None and admission_result.evicted_block is not None:
            self._dram_evictions += 1

        if admission_result is not None and admission_result.writeback is not None:
            if admission_result.writeback.placement_rejected:
                self._placement_rejections += 1
            self._record_io_events(admission_result.writeback.io_events)

        self._record_io_events(storage_result.io_events)

    def _record_io_events(self, events: Iterable[IOEvent]) -> None:
        for event in events:
            key = (event.tier, event.operation, event.reason)
            self._io_operations[key] += 1
            self._io_blocks[key] += event.block_count

    def snapshot(self) -> SimulationMetrics:
        """Return an immutable metrics snapshot."""

        io_counts = tuple(
            IOCount(
                tier=tier,
                operation=operation,
                reason=reason,
                operations=self._io_operations[(tier, operation, reason)],
                blocks=block_count,
                bytes=block_count * self._block_size_bytes,
            )
            for (tier, operation, reason), block_count in sorted(
                self._io_blocks.items(),
                key=lambda item: tuple(value.value for value in item[0]),
            )
        )
        return SimulationMetrics(
            query_count=self._query_count,
            block_access_count=self._block_access_count,
            dram_hits=self._dram_hits,
            dram_misses=self._dram_misses,
            tlc_accesses=self._tlc_accesses,
            qlc_accesses=self._qlc_accesses,
            dram_evictions=self._dram_evictions,
            placement_rejections=self._placement_rejections,
            io_counts=io_counts,
        )
