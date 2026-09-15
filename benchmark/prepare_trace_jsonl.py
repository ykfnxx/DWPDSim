"""Convert traceGen JSONL to canonical Parquet without changing replay order."""

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
src, out = args.source, args.output
out.mkdir(parents=True, exist_ok=True)
hashes = {}
sessions = {}
rows = []
lengths = []
times = []
total = 0
hits = 0
cost = 0
schema = pa.schema(
    [
        ("timestamp_ns", pa.uint64()),
        ("request_id", pa.uint64()),
        ("affinity_id", pa.uint64()),
        ("hash_ids", pa.list_(pa.uint64())),
    ]
)
with pq.ParquetWriter(out / "input.parquet", schema) as writer, src.open() as f:
    for i, line in enumerate(f):
        r = json.loads(line, parse_float=Decimal)
        ids = []
        prefix = True
        misses = 0
        for h in r["hash_ids"]:
            if h not in hashes:
                hashes[h] = len(hashes) + 1
                prefix = False
            ids.append(hashes[h])
            if prefix:
                hits += 1
            else:
                misses += 1
        n = len(ids)
        total += n
        cost += n * misses
        t = int(r["timestamp"] * 1000000000)
        if times:
            assert t >= times[-1]
        times.append(t)
        lengths.append(n)
        sid = r["session_id"]
        sessions.setdefault(sid, len(sessions) + 1)
        rows.append(
            {"timestamp_ns": t, "request_id": i, "affinity_id": sessions[sid], "hash_ids": ids}
        )
        if len(rows) == 512:
            writer.write_table(pa.Table.from_pylist(rows, schema=schema))
            rows = []
    if rows:
        writer.write_table(pa.Table.from_pylist(rows, schema=schema))
with src.open("rb") as f:
    sha = hashlib.file_digest(f, "sha256").hexdigest()
meta = {
    "source": str(src),
    "sha256": sha,
    "requests": len(times),
    "accesses": total,
    "unique_blocks": len(hashes),
    "sessions": len(sessions),
    "start_ns": times[0],
    "end_ns": times[-1],
    "duration_s": (times[-1] - times[0]) / 1e9,
    "context_blocks": {str(q): float(np.quantile(lengths, q)) for q in [0, 0.5, 0.9, 0.99, 1]},
    "storage_only_baseline_hits": hits,
    "storage_only_baseline_hit_rate": hits / total,
    "storage_only_compute_cost": cost,
}
(out / "dataset.json").write_text(json.dumps(meta, indent=2))
print(json.dumps(meta, indent=2))
