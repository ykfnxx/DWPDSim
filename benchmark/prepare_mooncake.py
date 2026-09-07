"""Explicitly normalize Mooncake's millisecond trace into the DWPDSim input schema.

Request/affinity IDs are row numbers because the source has neither field.
Rows are not sorted or shuffled; reject a source that is not chronological.
"""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    source = pq.read_table(args.source, columns=["timestamp", "hash_ids"])
    if args.limit is not None:
        source = source.slice(0, args.limit)
    timestamps = source["timestamp"].combine_chunks().to_numpy()
    if np.any(timestamps < 0) or np.any(timestamps[1:] < timestamps[:-1]):
        raise ValueError("expected nonnegative, nondecreasing millisecond timestamps")
    if len(timestamps) and int(timestamps[-1]) > (2**64 - 1) // 1_000_000:
        raise ValueError("nanosecond conversion exceeds uint64")
    ids = pa.array(np.arange(len(source), dtype=np.uint64))
    table = pa.table(
        {
            "timestamp_ns": pa.array(timestamps.astype(np.uint64) * np.uint64(1_000_000)),
            "request_id": ids,
            "affinity_id": ids,
            "hash_ids": source["hash_ids"].cast(pa.list_(pa.uint64()), safe=True),
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, args.output, row_group_size=1024)
    with args.source.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    args.output.with_suffix(".source.json").write_text(
        json.dumps(
            {
                "source": str(args.source.resolve()),
                "source_sha256": sha,
                "rows": len(source),
                "timestamp_conversion": "relative milliseconds * 1000000, no origin shift",
                "request_id": "zero-based source row",
                "affinity_id": "zero-based source row",
                "hash_ids": "unchanged uint64 identities",
                "limit": args.limit,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"wrote {len(source)} requests to {args.output}")


if __name__ == "__main__":
    main()
