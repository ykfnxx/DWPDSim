"""Thin Python facade over the C++ simulation core."""

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
from dwpdsim.config import (
    MemoryPolicyConfig,
    PlacementPolicyConfig,
    SimulationConfig,
    StorageEvictionPolicyConfig,
)
from dwpdsim.models import Request

logger = logging.getLogger(__name__)


class DWPDSimulator:
    """Replay KV block requests through the C++ cache state machine."""

    def __init__(
        self,
        config: SimulationConfig,
        trace_path: str | PathLike[str],
        *,
        memory_policy: MemoryPolicyConfig | None = None,
        placement_policy: PlacementPolicyConfig | None = None,
        storage_eviction_policy: StorageEvictionPolicyConfig | None = None,
    ) -> None:
        memory = memory_policy or MemoryPolicyConfig()
        placement = placement_policy or PlacementPolicyConfig()
        storage_eviction = storage_eviction_policy or StorageEvictionPolicyConfig()

        core_config = _core.SimulationConfig()
        core_config.block_size_bytes = config.block_size_bytes
        core_config.memory_capacity_bytes = config.memory_capacity_bytes
        core_config.timestamp_unit = config.timestamp_unit
        core_config.progress_interval_requests = config.progress_interval_requests
        core_config.slc = self._medium_config(config.slc)
        core_config.tlc = self._medium_config(config.tlc)

        self.config = config
        self.trace_path = Path(trace_path)
        self._core = _core.Simulator(
            core_config,
            str(self.trace_path),
            memory_policy=memory.kind,
            admit_storage_hits=memory.admit_storage_hits,
            memory_eviction_action=memory.eviction_action,
            placement_policy=placement.kind,
            fixed_medium=placement.fixed_medium,
            fixed_stream_id=placement.fixed_stream_id,
            slc_write_ratio=placement.slc_write_ratio,
            storage_eviction_policy=storage_eviction.kind,
        )
        self._processed_requests = 0
        self._next_progress_request = config.progress_interval_requests
        self._started_at = time.perf_counter()
        logger.info(
            "started DWPDSim block_size=%d memory_bytes=%d slc_bytes=%d tlc_bytes=%d",
            config.block_size_bytes,
            config.memory_capacity_bytes,
            config.slc.capacity_bytes,
            config.tlc.capacity_bytes,
        )

    @staticmethod
    def _medium_config(config: Any):
        result = _core.MediumConfig()
        result.capacity_bytes = config.capacity_bytes
        result.stream_count = config.stream_count
        return result

    def process(self, timestamp: int, hash_ids: Sequence[int]) -> None:
        """Process one request. Prefer ``process_batch`` for large datasets."""

        self._core.process(timestamp, hash_ids)
        self._processed_requests += 1
        self._log_progress()

    def process_batch(self, timestamps: Any, offsets: Any, hash_ids: Any) -> None:
        """Process contiguous uint64 request buffers without per-block Python calls."""

        self._core.process_batch(timestamps, offsets, hash_ids)
        self._processed_requests += len(timestamps)
        self._log_progress()

    def run(self, requests: Iterable[Request]) -> dict[str, Any]:
        """Convenience path for small iterables of requests."""

        for request in requests:
            self.process(request.timestamp, request.hash_ids)
        return self.stats()

    def stats(self) -> dict[str, Any]:
        return self._core.stats()

    def write_stats(self, path: str | PathLike[str]) -> None:
        Path(path).write_text(json.dumps(self.stats(), indent=2) + "\n", encoding="utf-8")

    def finish(self) -> None:
        self._core.finish()
        stats = self.stats()
        logger.info(
            "finished DWPDSim requests=%d accesses=%d hit_rate=%.6f trace_events=%d elapsed_s=%.3f",
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
                "DWPDSim progress requests=%d accesses=%d memory_blocks=%d slc_blocks=%d "
                "tlc_blocks=%d",
                stats["accesses"]["requests"],
                stats["accesses"]["total"],
                stats["memory"]["resident_blocks"],
                stats["storage"]["slc"]["resident_blocks"],
                stats["storage"]["tlc"]["resident_blocks"],
            )
            self._next_progress_request = (self._processed_requests // interval + 1) * interval
