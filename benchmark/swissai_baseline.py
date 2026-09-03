"""Replay the SwissAI trace through the C++ DWPDSim core."""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path

import numpy as np

from dwpdsim import (
    DWPDSimulator,
    PlacementPolicyConfig,
    SimulationConfig,
    StorageTierConfig,
)

MIB = 1024 * 1024
BLOCK_SIZE_BYTES = 8 * MIB
MEMORY_BLOCKS = 65_536
SLC_BLOCKS = 262_144
TLC_BLOCKS = 2_097_152
# The legacy naive segment precedes the newer UTC segment when interpreted as +08:00.
DEFAULT_NAIVE_UTC_OFFSET_HOURS = 8


def build_simulator(trace_path: Path) -> DWPDSimulator:
    config = SimulationConfig(
        block_size_bytes=BLOCK_SIZE_BYTES,
        memory_capacity_bytes=MEMORY_BLOCKS * BLOCK_SIZE_BYTES,
        slc=StorageTierConfig(SLC_BLOCKS * BLOCK_SIZE_BYTES, stream_count=1),
        tlc=StorageTierConfig(TLC_BLOCKS * BLOCK_SIZE_BYTES, stream_count=1),
        timestamp_unit="us",
    )
    return DWPDSimulator(
        config,
        trace_path,
        placement_policy=PlacementPolicyConfig(
            kind="fixed",
            fixed_tier="tlc",
            fixed_stream_id=0,
        ),
    )


def elapsed_microseconds(timestamp: datetime, origin: datetime) -> int:
    delta = timestamp - origin
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def parse_timestamp(value: str, naive_timezone: tzinfo) -> datetime:
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=naive_timezone)
    return timestamp.astimezone(UTC)


def run(
    dataset: Path,
    trace_path: Path,
    metrics_path: Path,
    *,
    batch_requests: int,
    progress_every: int,
    naive_utc_offset_hours: int,
) -> dict:
    simulator = build_simulator(trace_path)
    request_count = 0
    occurrence_count = 0
    canonical_reuses = 0
    first_timestamp: datetime | None = None
    batch_timestamps: list[int] = []
    batch_offsets = [0]
    batch_hashes: list[int] = []
    next_progress = progress_every
    started = time.perf_counter()
    naive_timezone = timezone(timedelta(hours=naive_utc_offset_hours))

    def flush_batch() -> None:
        if not batch_timestamps:
            return
        simulator.process_batch(
            np.asarray(batch_timestamps, dtype=np.uint64),
            np.asarray(batch_offsets, dtype=np.uint64),
            np.asarray(batch_hashes, dtype=np.uint64),
        )
        batch_timestamps.clear()
        batch_offsets[:] = [0]
        batch_hashes.clear()

    with dataset.open(encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            timestamp = parse_timestamp(record["created_at"], naive_timezone)
            if first_timestamp is None:
                first_timestamp = timestamp

            hash_ids = [int(value) for value in record["bucket_ids"]]
            batch_timestamps.append(elapsed_microseconds(timestamp, first_timestamp))
            batch_hashes.extend(hash_ids)
            batch_offsets.append(len(batch_hashes))

            request_count += 1
            occurrence_count += len(hash_ids)
            canonical_reuses += int(record["reused_buckets"])

            if len(batch_timestamps) == batch_requests:
                flush_batch()

            if progress_every and request_count >= next_progress:
                flush_batch()
                print(
                    f"requests={request_count} accesses={occurrence_count} "
                    f"nodes={simulator.node_count} elapsed_s={time.perf_counter() - started:.1f}",
                    flush=True,
                )
                next_progress += progress_every

    flush_batch()
    simulator.finish()
    simulator.write_stats(metrics_path)
    elapsed = time.perf_counter() - started

    return {
        "dataset": str(dataset.resolve()),
        "trace": str(trace_path.resolve()),
        "metrics": str(metrics_path.resolve()),
        "elapsed_seconds": elapsed,
        "requests": request_count,
        "block_occurrences": occurrence_count,
        "radix_tree_nodes": simulator.node_count,
        "dataset_reported_reuses": canonical_reuses,
        "naive_timestamp_utc_offset_hours": naive_utc_offset_hours,
        "configuration_blocks": {
            "memory": MEMORY_BLOCKS,
            "slc": SLC_BLOCKS,
            "tlc": TLC_BLOCKS,
        },
        "stats": simulator.stats(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--batch-requests", type=int, default=256)
    parser.add_argument("--progress-every", type=int, default=1_000)
    parser.add_argument(
        "--naive-utc-offset-hours",
        type=int,
        default=DEFAULT_NAIVE_UTC_OFFSET_HOURS,
        help="UTC offset for legacy created_at values without an explicit timezone",
    )
    args = parser.parse_args()

    args.trace.parent.mkdir(parents=True, exist_ok=True)
    args.metrics.parent.mkdir(parents=True, exist_ok=True)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    result = run(
        args.dataset,
        args.trace,
        args.metrics,
        batch_requests=args.batch_requests,
        progress_every=args.progress_every,
        naive_utc_offset_hours=args.naive_utc_offset_hours,
    )
    args.summary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
