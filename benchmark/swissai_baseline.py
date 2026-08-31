"""Replay the SwissAI Qwen3-80B Thinking trace with the simple TLC baseline."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from dwpdsim import DWPDSimulator, Query, SimulationConfig, SSDConfig

BLOCK_SIZE_BYTES = 4096
DRAM_BLOCKS = 65_536
SLC_BLOCKS = 262_144
TLC_BLOCKS = 2_097_152
CHUNK_BLOCKS = 1024


def build_simulator() -> DWPDSimulator:
    """Build the LRU DRAM and fixed-TLC control group."""

    config = SimulationConfig(
        block_size_bytes=BLOCK_SIZE_BYTES,
        dram_capacity_bytes=DRAM_BLOCKS * BLOCK_SIZE_BYTES,
        slc=SSDConfig(
            capacity_bytes=SLC_BLOCKS * BLOCK_SIZE_BYTES,
            chunk_size_bytes=CHUNK_BLOCKS * BLOCK_SIZE_BYTES,
            stream_count=1,
            gc_reserve_chunks=1,
        ),
        tlc=SSDConfig(
            capacity_bytes=TLC_BLOCKS * BLOCK_SIZE_BYTES,
            chunk_size_bytes=CHUNK_BLOCKS * BLOCK_SIZE_BYTES,
            stream_count=1,
            gc_reserve_chunks=1,
        ),
    )
    return DWPDSimulator.from_config(config)


def run(dataset: Path, output: Path, progress_every: int) -> dict:
    """Canonicalize prefix blocks and replay the complete service trace."""

    simulator = build_simulator()
    prefix_children: dict[tuple[int, int], int] = {}
    next_block_id = 1
    request_count = 0
    occurrence_count = 0
    canonical_reuses = 0
    first_timestamp: datetime | None = None
    started = time.perf_counter()

    with dataset.open(encoding="utf-8") as trace:
        for line in trace:
            record = json.loads(line)
            parent_id = 0
            hash_ids: list[int] = []
            reused = 0

            for bucket_id in record["bucket_ids"]:
                key = (parent_id, bucket_id)
                block_id = prefix_children.get(key)
                if block_id is None:
                    block_id = next_block_id
                    next_block_id += 1
                    prefix_children[key] = block_id
                else:
                    reused += 1
                hash_ids.append(block_id)
                parent_id = block_id

            if reused != record["reused_buckets"]:
                raise ValueError(f"reused_buckets mismatch for request {record['id']}")

            timestamp = datetime.fromisoformat(record["created_at"])
            if first_timestamp is None:
                first_timestamp = timestamp

            simulator.process_query(
                Query(
                    timestamp=(timestamp - first_timestamp).total_seconds(),
                    hash_ids=tuple(hash_ids),
                )
            )
            request_count += 1
            occurrence_count += len(hash_ids)
            canonical_reuses += reused

            if request_count % progress_every == 0:
                elapsed = time.perf_counter() - started
                print(
                    f"requests={request_count} accesses={occurrence_count} "
                    f"unique_blocks={next_block_id - 1} elapsed_s={elapsed:.1f}",
                    flush=True,
                )

    simulator.write_stats(output)
    elapsed = time.perf_counter() - started
    return {
        "dataset": str(dataset.resolve()),
        "output": str(output.resolve()),
        "elapsed_seconds": elapsed,
        "requests": request_count,
        "block_occurrences": occurrence_count,
        "unique_blocks": next_block_id - 1,
        "canonical_reuses": canonical_reuses,
        "configuration_blocks": {
            "dram": DRAM_BLOCKS,
            "slc": SLC_BLOCKS,
            "tlc": TLC_BLOCKS,
            "chunk": CHUNK_BLOCKS,
        },
        "stats": simulator.stats(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--progress-every", type=int, default=500)
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    result = run(args.dataset, args.output, args.progress_every)
    args.summary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
