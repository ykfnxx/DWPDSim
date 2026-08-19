"""Top-level DWPDSim orchestration API."""

from collections.abc import Iterable

from dwpdsim.config import SimulationConfig
from dwpdsim.errors import OutOfOrderQueryError
from dwpdsim.memory import MemManager
from dwpdsim.metrics import MetricsCollector, SimulationReport, TierUsage
from dwpdsim.models import BlockId, MemQueryResult, Query
from dwpdsim.policies.base import (
    AdmissionPolicy,
    MemCachePolicy,
    PlacementPolicy,
    StorageCachePolicy,
)
from dwpdsim.storage import StorageManager


class DWPDSimulator:
    """Processes ordered queries and exposes cumulative simulation reports."""

    def __init__(
        self,
        memory: MemManager,
        storage: StorageManager,
        metrics: MetricsCollector,
    ) -> None:
        self.memory = memory
        self.storage = storage
        self.metrics = metrics
        self._last_timestamp: int | None = None

    @classmethod
    def from_config(
        cls,
        config: SimulationConfig,
        initial_blocks: Iterable[BlockId] = (),
        memory_cache_policy: MemCachePolicy | None = None,
        memory_admission_policy: AdmissionPolicy | None = None,
        storage_placement_policy: PlacementPolicy | None = None,
        storage_cache_policy: StorageCachePolicy | None = None,
    ) -> "DWPDSimulator":
        """Build a simulator with optional injected policies."""

        storage = StorageManager(
            tlc_config=config.tlc,
            qlc_config=config.qlc,
            placement_policy=storage_placement_policy,
            storage_cache_policy=storage_cache_policy,
            initial_blocks=initial_blocks,
        )
        memory = MemManager(
            config=config.dram,
            lower_storage=storage,
            mem_cache_policy=memory_cache_policy,
            admission_policy=memory_admission_policy,
        )
        return cls(
            memory=memory,
            storage=storage,
            metrics=MetricsCollector(config.block_size_bytes),
        )

    def process_query(self, query: Query) -> MemQueryResult:
        """Process and record one query, enforcing timestamp order."""

        if self._last_timestamp is not None and query.timestamp < self._last_timestamp:
            raise OutOfOrderQueryError(
                f"query timestamp {query.timestamp} precedes {self._last_timestamp}"
            )

        result = self.memory.process_query(query)
        self.metrics.record_query(result)
        self._last_timestamp = query.timestamp
        return result

    def run(self, queries: Iterable[Query]) -> SimulationReport:
        """Process an iterable of queries and return the cumulative report."""

        for query in queries:
            self.process_query(query)
        return self.report()

    def report(self) -> SimulationReport:
        """Return current hit-rate, I/O, and capacity statistics."""

        dram_capacity = self.memory.capacity
        tlc_capacity = self.storage.tlc_capacity
        qlc_capacity = self.storage.qlc_capacity
        return SimulationReport(
            metrics=self.metrics.snapshot(),
            dram=TierUsage(
                capacity_blocks=dram_capacity.capacity_blocks,
                used_blocks=dram_capacity.used_blocks,
                peak_used_blocks=self.memory.peak_used_blocks,
                eviction_count=self.memory.eviction_count,
            ),
            tlc=TierUsage(
                capacity_blocks=tlc_capacity.capacity_blocks,
                used_blocks=tlc_capacity.used_blocks,
                peak_used_blocks=self.storage.tlc_peak_used_blocks,
                eviction_count=self.storage.tlc_eviction_count,
            ),
            qlc=TierUsage(
                capacity_blocks=qlc_capacity.capacity_blocks,
                used_blocks=qlc_capacity.used_blocks,
                peak_used_blocks=self.storage.qlc_peak_used_blocks,
                eviction_count=0,
            ),
        )
