"""Python facade over the C++ simulation core."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict
from itertools import islice
from os import PathLike
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from dwpdsim import _core
from dwpdsim.config import MemoryPolicyConfig, SimulationConfig
from dwpdsim.models import HybridEvent, HybridPrefillRequest, Request

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

        hybrid = _core.HybridAttentionConfig()
        for key, value in asdict(config.hybrid_attention).items():
            if key == "cost":
                cost = _core.JointCostConfig()
                for parameter, setting in value.items():
                    setattr(cost, parameter, setting)
                hybrid.cost = cost
            else:
                setattr(hybrid, key, value)
        core_config.hybrid_attention = hybrid
        if config.hybrid_attention.enabled and config.memory_policy != MemoryPolicyConfig():
            raise ValueError("hybrid mode uses hybrid_attention policies; legacy memory_policy must be default")
        core_config.profile_memory = config.memory_policy.profile
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
            memory_groups=memory.groups,
            memory_sampled_groups=memory.sampled_groups,
            memory_workers=memory.workers,
            memory_seed=memory.seed,
            memory_retention_ns=memory.retention_ns,
            memory_alpha=memory.alpha,
            memory_max_eviction_blocks=memory.max_eviction_blocks,
            memory_retention_growth_seconds_per_block=memory.retention_growth_seconds_per_block,
            memory_eviction_gap_reference_ns=memory.eviction_gap_reference_ns,
            memory_eviction_base_blocks=memory.eviction_base_blocks,
            memory_reuse_edges_s=memory.reuse_edges_s,
            memory_reuse_survival=memory.reuse_survival,
            memory_reuse_dump_probability=memory.reuse_dump_probability,
            memory_reuse_priority=memory.reuse_priority,
            memory_reuse_admit_probability=memory.reuse_admit_probability,
            memory_reuse_short_horizon_s=memory.reuse_short_horizon_s,
            memory_reuse_long_horizon_s=memory.reuse_long_horizon_s,
            memory_reuse_prefix_keep=memory.reuse_prefix_keep,
            memory_reuse_cost_weighted=memory.reuse_cost_weighted,
            memory_reuse_recompute_weight=memory.reuse_recompute_weight,
            storage_policy=storage.kind,
            rr_victim_search=storage.rr_victim_search,
            rr_subtree_counts=storage.rr_subtree_counts,
            rr_verify_victims=storage.rr_verify_victims,
            rr_profile=storage.rr_profile,
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
            online_tuning=storage.online_tuning,
            feedback_period_ns=storage.feedback_period_ns,
            shadow_page_bytes=storage.shadow_page_bytes,
            shadow_pages_per_block=storage.shadow_pages_per_block,
            shadow_overprovisioning=storage.shadow_overprovisioning,
            shadow_slc_nominal_bytes=storage.shadow_slc_nominal_bytes,
            shadow_tlc_nominal_bytes=storage.shadow_tlc_nominal_bytes,
            reuse_loss_budget=storage.reuse_loss_budget,
            min_reuse_blocks=storage.min_reuse_blocks,
            reuse_ema_scale_blocks=storage.reuse_ema_scale_blocks,
            shadow_slc_physical_blocks=storage.shadow_slc_physical_blocks,
            shadow_tlc_physical_blocks=storage.shadow_tlc_physical_blocks,
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

    @staticmethod
    def _hybrid_event(event: HybridEvent):
        if event.restore_blocks is not None and event.restore_blocks <= 0:
            raise ValueError("restore_blocks must be positive or None")
        if event.size_bytes is not None and event.size_bytes <= 0:
            raise ValueError("size_bytes must be positive or None")
        result = _core.HybridEvent()
        for key in ("timestamp_ns", "sequence", "action", "hash_ids", "request_id",
                    "components", "restore_blocks", "size_bytes"):
            value = getattr(event, key)
            setattr(result, key, 0 if value is None else value)
        return result

    @staticmethod
    def _hybrid_result(result) -> dict[str, Any]:
        return {key: getattr(result, key) for key in (
            "sequence", "request_id", "restore_blocks", "full", "linear",
            "all_available", "all_dram",
        )}

    def process_hybrid(self, event: HybridEvent) -> dict[str, Any] | None:
        """Process an explicit component event; legacy mode rejects this API."""
        result = self._core.process_hybrid_event(self._hybrid_event(event))
        if result is not None:
            self._processed_requests += 1
            self._log_progress()
        return None if result is None else self._hybrid_result(result)

    def process_hybrid_events(self, events: Sequence[HybridEvent]) -> list[dict[str, Any]]:
        """Replay an ordered event batch in C++, returning request results only."""
        results = self._core.process_hybrid_events([self._hybrid_event(e) for e in events])
        self._processed_requests += len(results)
        self._log_progress()
        return [self._hybrid_result(r) for r in results]

    def process_hybrid_batch(
        self, timestamps_ns: Any, sequences: Any, actions: Any, components: Any,
        request_ids: Any, restore_blocks: Any, sizes: Any, offsets: Any, hash_ids: Any,
    ) -> list[dict[str, Any]]:
        """Replay contiguous uint64 columns: actions observe/publish/request=0/1/2,
        components full/linear/both=0/1/2. Zero size/boundary uses configured defaults.
        """
        results = self._core.process_hybrid_batch(
            timestamps_ns, sequences, actions, components, request_ids,
            restore_blocks, sizes, offsets, hash_ids,
        )
        self._processed_requests += len(results)
        self._log_progress()
        return [self._hybrid_result(r) for r in results]

    def run_hybrid(self, events: Iterable[HybridEvent], batch_events: int = 1024) -> dict[str, Any]:
        """Bounded streaming replay; per-request results are not retained."""
        if batch_events <= 0:
            raise ValueError("batch_events must be positive")
        source = iter(events)
        while batch := list(islice(source, batch_events)):
            self.process_hybrid_events(batch)
        return self.hybrid_stats()

    def hybrid_stats(self) -> dict[str, Any]:
        return self._core.hybrid_stats()

    @staticmethod
    def _prefill_request(request: HybridPrefillRequest):
        if request.input_tokens is not None and request.input_tokens <= 0:
            raise ValueError("input_tokens must be positive or None")
        result = _core.HybridPrefillRequest()
        for key in ("timestamp_ns", "sequence", "request_id", "hash_ids", "max_prefix_tokens", "affinity_id"):
            setattr(result, key, getattr(request, key))
        result.input_tokens = 0 if request.input_tokens is None else request.input_tokens
        return result

    @staticmethod
    def _prefill_result(result) -> dict[str, Any]:
        return {key: getattr(result, key) for key in (
            "sequence", "request_id", "input_tokens", "matched_prefix_tokens",
            "full_available_prefix_tokens", "restore_tokens", "restore_hash_id", "full", "linear",
            "prefill_compute_tokens", "replayed_prefix_tokens", "new_suffix_tokens",
            "generated_checkpoint_tokens",
        )}

    def process_hybrid_prefill(self, request: HybridPrefillRequest) -> dict[str, Any]:
        """Restore the deepest usable prefix, then generate fixed-interval checkpoints."""
        result = self._core.process_hybrid_prefill(self._prefill_request(request))
        self._processed_requests += 1
        self._log_progress()
        return self._prefill_result(result)

    def process_hybrid_prefill_requests(
        self, requests: Sequence[HybridPrefillRequest],
    ) -> list[dict[str, Any]]:
        """Process a request batch in C++; generated supply remains causal."""
        results = self._core.process_hybrid_prefill_requests([self._prefill_request(q) for q in requests])
        self._processed_requests += len(results)
        self._log_progress()
        return [self._prefill_result(r) for r in results]

    def process_hybrid_prefill_batch(
        self, timestamps_ns: Any, sequences: Any, request_ids: Any, input_tokens: Any,
        max_prefix_tokens: Any, offsets: Any, hash_ids: Any,
    ) -> list[dict[str, Any]]:
        """Contiguous uint64 columns; input_tokens=0 infers size, limit=UINT64_MAX uses N-1."""
        results = self._core.process_hybrid_prefill_batch(
            timestamps_ns, sequences, request_ids, input_tokens, max_prefix_tokens, offsets, hash_ids,
        )
        self._processed_requests += len(results)
        self._log_progress()
        return [self._prefill_result(r) for r in results]

    def run_hybrid_prefill(
        self, requests: Iterable[HybridPrefillRequest], batch_requests: int = 1024,
    ) -> dict[str, Any]:
        """Stream prefill inputs without retaining per-request results."""
        if batch_requests <= 0:
            raise ValueError("batch_requests must be positive")
        source = iter(requests)
        while batch := list(islice(source, batch_requests)):
            self._core.run_hybrid_prefill_requests([self._prefill_request(q) for q in batch])
            self._processed_requests += len(batch)
            self._log_progress()
        return self.hybrid_stats()

    def hybrid_inspect(self, hash_id: int, component: str) -> dict[str, Any]:
        """Read component state without changing heat or residency."""
        return self._core.hybrid_inspect(hash_id, component)

    def storage_performance(self) -> dict[str, int]:
        """RR query/maintenance counters; timers require rr_profile=True."""
        return self._core.storage_performance()

    def enable_request_diagnostics(self, path: str | PathLike[str]) -> None:
        """Write per-request physical hit counts and continuous-prefix misses."""
        self._core.enable_request_diagnostics(str(path))

    def enable_memory_diagnostics(self, path: str | PathLike[str]) -> None:
        """Stream per-block evictions for offline diagnosis in infinite_storage mode."""
        self._core.enable_memory_diagnostics(str(path))

    def memory_performance(self) -> dict[str, int]:
        """Experimental work counters; timings require MemoryPolicyConfig.profile."""
        return self._core.memory_performance()

    def stats(self) -> dict[str, Any]:
        if self.config.hybrid_attention.enabled:
            return {"hybrid_attention": self.hybrid_stats()}
        return self._core.stats()

    def write_stats(self, path: str | PathLike[str]) -> None:
        Path(path).write_text(json.dumps(self.stats(), indent=2) + "\n", encoding="utf-8")

    def finish(self) -> None:
        self._core.finish()
        stats = self.stats()
        if self.config.hybrid_attention.enabled:
            logger.info("finished hybrid replay requests=%d elapsed_s=%.3f",
                        stats["hybrid_attention"]["requests"], time.perf_counter() - self._started_at)
            return
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
            if self.config.hybrid_attention.enabled:
                hybrid = self.hybrid_stats()
                logger.info("DWPDSim hybrid progress requests=%d full_dram_bytes=%d linear_dram_bytes=%d",
                            hybrid["requests"], hybrid["full"]["resident_bytes"],
                            hybrid["linear"]["resident_bytes"])
                self._next_progress_request = (self._processed_requests // interval + 1) * interval
                return
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
