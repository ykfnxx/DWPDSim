"""Fixed DRAM then storage access flow."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from dwpdsim.config import SimulationConfig
from dwpdsim.errors import OutOfOrderQueryError
from dwpdsim.managers import DRAMManager, StorageManager
from dwpdsim.metrics import MetricsCollector
from dwpdsim.models import (
    AccessContext,
    AccessResult,
    Medium,
    PlacementContext,
    Query,
)
from dwpdsim.policies.dram import DramPolicy
from dwpdsim.policies.gc import GCPolicy
from dwpdsim.policies.placement import PlacementPolicy
from dwpdsim.policies.storage_eviction import StorageEvictionPolicy
from dwpdsim.sequence import ROOT_NODE, SequenceIndex


class DWPDSimulator:
    """Process every hash ID in order with one fixed lookup flow."""

    def __init__(
        self,
        config: SimulationConfig,
        dram: DRAMManager,
        storage: StorageManager,
        sequence: SequenceIndex,
        metrics: MetricsCollector,
    ) -> None:
        self.config = config
        self.dram = dram
        self.storage = storage
        self.sequence = sequence
        self.metrics = metrics
        self._last_timestamp: int | float | None = None

    @classmethod
    def from_config(
        cls,
        config: SimulationConfig,
        *,
        dram_policy: DramPolicy | None = None,
        placement_policy: PlacementPolicy | None = None,
        storage_eviction_policy: StorageEvictionPolicy | None = None,
        slc_gc_policy: GCPolicy | None = None,
        tlc_gc_policy: GCPolicy | None = None,
    ) -> DWPDSimulator:
        storage = StorageManager(
            config,
            placement_policy=placement_policy,
            eviction_policy=storage_eviction_policy,
            slc_gc_policy=slc_gc_policy,
            tlc_gc_policy=tlc_gc_policy,
        )
        return cls(
            config=config,
            dram=DRAMManager(config.dram_capacity_blocks, dram_policy),
            storage=storage,
            sequence=SequenceIndex(),
            metrics=MetricsCollector(config),
        )

    def process_query(self, query: Query) -> tuple[AccessResult, ...]:
        self._validate_timestamp(query)
        if not query.hash_ids:
            self._last_timestamp = query.timestamp
            self.metrics.record_query(query.timestamp)
            return ()

        parent_node_id = ROOT_NODE
        results: list[AccessResult] = []
        for position, block_id in enumerate(query.hash_ids):
            prefix_node_id = self.sequence.existing_child(parent_node_id, block_id)
            context = AccessContext(
                query=query,
                position=position,
                block_id=block_id,
                history=self.sequence.history(block_id),
                parent_node=self.sequence.node(parent_node_id),
                prefix_node=(
                    self.sequence.node(prefix_node_id) if prefix_node_id is not None else None
                ),
            )
            result = self._process_access(context)
            if not results:
                self._last_timestamp = query.timestamp
                self.metrics.record_query(query.timestamp)
            parent_node_id = self.sequence.observe(
                block_id,
                query.timestamp,
                parent_node_id,
            )
            self.metrics.record_access(result)
            results.append(result)
        return tuple(results)

    def run(self, queries: Iterable[Query]) -> dict[str, Any]:
        for query in queries:
            self.process_query(query)
        return self.stats()

    def stats(self) -> dict[str, Any]:
        return self.metrics.snapshot(self.storage)

    def write_stats(self, path: str | Path) -> None:
        self.metrics.write_json(path, self.storage)

    def _process_access(self, context: AccessContext) -> AccessResult:
        if self.dram.access(context.block_id):
            return AccessResult.DRAM_HIT

        medium = self.storage.lookup(context.block_id)
        if medium is None:
            self._insert_into_dram(context)
            return AccessResult.GLOBAL_MISS

        result = AccessResult.SLC_HIT if medium is Medium.SLC else AccessResult.TLC_HIT
        if self.dram.should_admit(context):
            self._insert_into_dram(context)
        if self.storage.contains(context.block_id):
            self.storage.read(context.block_id)
        return result

    def _insert_into_dram(self, context: AccessContext) -> None:
        victim = self.dram.victim_for(context)
        if victim is not None and not self.storage.contains(victim):
            self.storage.write_from_dram(
                PlacementContext(
                    block_id=victim,
                    history=self.sequence.history(victim),
                    trigger=context,
                )
            )
        self.dram.insert(context.block_id, victim)

    def _validate_timestamp(self, query: Query) -> None:
        if self._last_timestamp is not None and query.timestamp < self._last_timestamp:
            raise OutOfOrderQueryError(
                f"query timestamp {query.timestamp} precedes {self._last_timestamp}"
            )
