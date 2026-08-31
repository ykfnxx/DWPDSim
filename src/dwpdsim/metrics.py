"""Raw simulation statistics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dwpdsim.config import SimulationConfig
from dwpdsim.managers.storage import StorageManager
from dwpdsim.models import AccessResult, Medium, Timestamp


class MetricsCollector:
    """Count only values directly produced by the simulation."""

    def __init__(self, config: SimulationConfig) -> None:
        self.config = config
        self.query_count = 0
        self.access_count = 0
        self.dram_hits = 0
        self.slc_hits = 0
        self.tlc_hits = 0
        self.global_misses = 0
        self.start_timestamp: Timestamp | None = None
        self.end_timestamp: Timestamp | None = None

    def record_query(self, timestamp: Timestamp) -> None:
        self.query_count += 1
        if self.start_timestamp is None:
            self.start_timestamp = timestamp
        self.end_timestamp = timestamp

    def record_access(self, result: AccessResult) -> None:
        self.access_count += 1
        if result is AccessResult.DRAM_HIT:
            self.dram_hits += 1
        elif result is AccessResult.SLC_HIT:
            self.slc_hits += 1
        elif result is AccessResult.TLC_HIT:
            self.tlc_hits += 1
        else:
            self.global_misses += 1

    def snapshot(self, storage: StorageManager) -> dict[str, Any]:
        block_size = self.config.block_size_bytes
        dram_misses = self.slc_hits + self.tlc_hits + self.global_misses
        storage_hits = self.slc_hits + self.tlc_hits
        all_hits = self.dram_hits + storage_hits
        duration = (
            self.end_timestamp - self.start_timestamp
            if self.start_timestamp is not None and self.end_timestamp is not None
            else 0
        )

        writes_from_dram = storage.writes_from_dram
        transfers = storage.transfers
        return {
            "time": {
                "unit": "seconds",
                "start_timestamp": self.start_timestamp,
                "end_timestamp": self.end_timestamp,
                "duration_seconds": duration,
            },
            "configuration": {
                "block_size_bytes": block_size,
                "dram_capacity_bytes": self.config.dram_capacity_bytes,
                "slc_capacity_bytes": self.config.slc.capacity_bytes,
                "tlc_capacity_bytes": self.config.tlc.capacity_bytes,
            },
            "accesses": {
                "queries": self.query_count,
                "total": self.access_count,
                "dram_hits": self.dram_hits,
                "slc_hits": self.slc_hits,
                "tlc_hits": self.tlc_hits,
                "global_misses": self.global_misses,
                "dram_hit_rate": self._rate(self.dram_hits, self.access_count),
                "storage_hit_rate": self._rate(storage_hits, dram_misses),
                "total_hit_rate": self._rate(all_hits, self.access_count),
            },
            "created": self._block_count(self.global_misses, block_size),
            "writes_from_dram": {
                "slc": self._block_count(writes_from_dram[Medium.SLC], block_size),
                "tlc": self._block_count(writes_from_dram[Medium.TLC], block_size),
            },
            "transfers": {
                "slc_to_tlc": self._block_count(transfers[(Medium.SLC, Medium.TLC)], block_size),
                "tlc_to_slc": self._block_count(transfers[(Medium.TLC, Medium.SLC)], block_size),
            },
            "erases": {
                "slc": {
                    "direct": storage.slc.whole_erase_count,
                    "non_full": storage.slc.non_full_erase_count,
                },
                "tlc": {
                    "direct": storage.tlc.whole_erase_count,
                    "non_full": storage.tlc.non_full_erase_count,
                },
            },
            "stream_writes": {
                "slc": self._stream_writes(storage.slc.logical_writes_by_stream, block_size),
                "tlc": self._stream_writes(storage.tlc.logical_writes_by_stream, block_size),
            },
        }

    def write_json(self, path: str | Path, storage: StorageManager) -> None:
        Path(path).write_text(
            json.dumps(self.snapshot(storage), indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _rate(numerator: int, denominator: int) -> float:
        return numerator / denominator if denominator else 0.0

    @staticmethod
    def _block_count(blocks: int, block_size: int) -> dict[str, int]:
        return {"blocks": blocks, "bytes": blocks * block_size}

    @classmethod
    def _stream_writes(
        cls,
        counts: tuple[int, ...],
        block_size: int,
    ) -> dict[str, dict[str, int]]:
        return {
            str(stream_id): cls._block_count(blocks, block_size)
            for stream_id, blocks in enumerate(counts)
        }
