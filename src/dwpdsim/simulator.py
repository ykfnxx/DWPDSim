"""Python facade over the C++ simulation core."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable, Sequence
from os import PathLike
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from dwpdsim import _core
from dwpdsim.config import SimulationConfig
from dwpdsim.models import Request

logger = logging.getLogger(__name__)


class DWPDSimulator:
    """Replay requests through one shared RadixTree and two storage pools."""

    def __init__(
        self,
        config: SimulationConfig,
        trace_path: str | PathLike[str],
    ) -> None:
        core_config = _core.SimulationConfig()
        core_config.block_size_bytes = config.block_size_bytes
        core_config.memory = self._memory_config(config.memory)
        core_config.slc = self._storage_tier_config(config.slc)
        core_config.tlc = self._storage_tier_config(config.tlc)
        core_config.simulation_end_ns = config.simulation_end_ns
        core_config.progress_interval_requests = config.progress_interval_requests

        memory = config.memory_policy
        storage = config.storage_policy
        slc_host_share = storage.slc_host_share
        if slc_host_share is None:
            slc_host_share = {
                "wear_share_round_robin": 0.405,
                "wear_share_affinity": 0.68,
            }.get(storage.kind, 0.8333333333)

        self.config = config
        self.trace_path = Path(trace_path)
        self._core = _core.Simulator(
            core_config,
            str(self.trace_path),
            memory_policy=memory.kind,
            admit_storage_hits=memory.admit_storage_hits,
            storage_policy=storage.kind,
            fixed_tier=storage.fixed_tier,
            fixed_stream_id=storage.fixed_stream_id,
            slc_write_ratio=storage.slc_write_ratio,
            slc_host_share=slc_host_share,
            idle_multiplier=storage.idle_multiplier,
            promotion_seconds=storage.promotion_seconds,
            adaptation_gain=storage.adaptation_gain,
            direct_gain=storage.direct_gain,
            slc_soft_utilization=storage.slc_soft_utilization,
            occupancy_decay=storage.occupancy_decay,
            logical_fill_fraction=storage.logical_fill_fraction,
            slc_erase_budget=storage.slc_erase_budget,
            tlc_erase_budget=storage.tlc_erase_budget,
            background_period_ns=storage.background_period_ns,
        )
        self._processed_requests = 0
        self._next_progress_request = config.progress_interval_requests
        self._started_at = time.perf_counter()
        logger.info(
            "started DWPDSim block_size=%d memory_bytes=%d slc_bytes=%d tlc_bytes=%d",
            config.block_size_bytes,
            config.memory.capacity_bytes,
            config.slc.capacity_bytes,
            config.tlc.capacity_bytes,
        )

    @staticmethod
    def _memory_config(config: Any):
        result = _core.MemoryConfig()
        result.capacity_bytes = config.capacity_bytes
        return result

    @staticmethod
    def _storage_tier_config(config: Any):
        result = _core.StorageTierConfig()
        result.capacity_bytes = config.capacity_bytes
        result.stream_count = config.stream_count
        return result

    def process(
        self,
        timestamp_ns: int,
        request_id: int,
        affinity_id: int,
        hash_ids: Sequence[int],
    ) -> None:
        """Process one request on the nanosecond timeline."""

        self._core.process(timestamp_ns, request_id, affinity_id, hash_ids)
        self._processed_requests += 1
        self._log_progress()

    def process_batch(
        self,
        timestamps_ns: Any,
        request_ids: Any,
        affinity_ids: Any,
        offsets: Any,
        hash_ids: Any,
    ) -> None:
        """Process contiguous uint64 request buffers."""

        self._core.process_batch(
            timestamps_ns,
            request_ids,
            affinity_ids,
            offsets,
            hash_ids,
        )
        self._processed_requests += len(timestamps_ns)
        self._log_progress()

    def run(self, requests: Iterable[Request]) -> dict[str, Any]:
        """Process a small iterable of requests."""

        for request in requests:
            self.process(
                request.timestamp_ns,
                request.request_id,
                request.affinity_id,
                request.hash_ids,
            )
        return self.stats()

    def stats(self) -> dict[str, Any]:
        return self._core.stats()

    def write_stats(self, path: str | PathLike[str]) -> None:
        Path(path).write_text(json.dumps(self.stats(), indent=2) + "\n", encoding="utf-8")

    def finish(self) -> None:
        self._core.finish()
        stats = self.stats()
        logger.info(
            "finished DWPDSim requests=%d accesses=%d hit_rate=%.6f trace_events=%d "
            "elapsed_s=%.3f",
            stats["accesses"]["requests"],
            stats["accesses"]["total"],
            stats["accesses"]["total_hit_rate"],
            stats["trace"]["events"],
            time.perf_counter() - self._started_at,
        )

    close = finish

    @property
    def node_count(self) -> int:
        return self._core.node_count

    @property
    def trace_event_count(self) -> int:
        return self._core.trace_event_count

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.finish()

    def _log_progress(self) -> None:
        interval = self.config.progress_interval_requests
        if interval and self._processed_requests >= self._next_progress_request:
            stats = self.stats()
            logger.info(
                "DWPDSim progress requests=%d accesses=%d memory_blocks=%d "
                "slc_live_bytes=%d tlc_live_bytes=%d",
                stats["accesses"]["requests"],
                stats["accesses"]["total"],
                stats["memory"]["resident_blocks"],
                stats["storage"]["slc"]["live_bytes"],
                stats["storage"]["tlc"]["live_bytes"],
            )
            self._next_progress_request = (self._processed_requests // interval + 1) * interval
