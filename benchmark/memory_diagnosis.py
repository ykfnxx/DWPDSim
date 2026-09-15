import argparse
import json
from pathlib import Path

import numpy as np
import pyarrow.csv as pc
import pyarrow.parquet as pq

parser = argparse.ArgumentParser(
    description="Join eviction events with strictly later block accesses."
)
parser.add_argument("--dataset", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
out = args.output
table = pq.read_table(args.dataset)
lists = table["hash_ids"].combine_chunks()
ids = lists.values.to_numpy()
times = np.repeat(table["timestamp_ns"].to_numpy(), np.diff(lists.offsets.to_numpy()))
stride = len(ids) + 1
if int(ids.max()) > (np.iinfo(np.uint64).max - stride) // stride:
    raise ValueError("Remap hash IDs to dense integers before diagnosis")
keys = ids.astype(np.uint64) * stride + np.arange(len(ids), dtype=np.uint64)
keys.sort()
np.save(out / "keys.npy", keys)
np.save(out / "times.npy", times)
print("future access index ready", flush=True)
keys = np.load(out / "keys.npy", mmap_mode="r")
times = np.load(out / "times.npy", mmap_mode="r")
stride = len(times) + 1
selection = json.loads((out / "selection.json").read_text())
rows = []
for job in selection:
    name = job["name"]
    event_path = out / (name + ".evictions.csv")
    ev = pc.read_csv(event_path).to_pydict()
    ids = np.array(ev["node_id"], dtype=np.uint64)
    seq = np.array(ev["sequence"], dtype=np.uint64)
    t = np.array(ev["timestamp_ns"], dtype=np.uint64)
    action = np.array(ev["action"])
    d = action == "D"
    w = action == "W"
    query = ids * stride + seq + 1
    pos = np.searchsorted(keys, query)
    valid = pos < len(keys)
    bounded = np.minimum(pos, len(keys) - 1)
    reuse = valid & (keys[bounded] // stride == ids)
    nextseq = keys[bounded] % stride
    wait = np.zeros(len(ids))
    wait[reuse] = (times[nextseq[reuse]] - t[reuse]) / 1e9
    stats = json.loads((out / (name + ".json")).read_text())["stats"]
    assert len(ids) == stats["memory"]["evicted_blocks"]
    assert d.sum() == stats["memory"]["drop_blocks"]
    assert w.sum() == stats["storage"]["tlc"]["writes"]["blocks"]

    def repeat(mask, ids=ids):
        unique, counts = np.unique(ids[mask], return_counts=True)
        return {
            "unique_blocks": len(unique),
            "blocks_repeated": int((counts > 1).sum()),
            "extra_events": int((counts - 1).sum()),
        }

    gaps = wait[d & reuse]
    row = {
        "name": name,
        "retention_s": job["retention_s"],
        "beta": job["beta"],
        "drop": int(d.sum()),
        "drop_reused": int((d & reuse).sum()),
        "drop_reused_pct": float((d & reuse).sum() / d.sum() * 100),
        "drop_no_future": int((d & ~reuse).sum()),
        "drop_gap_s": {str(q): float(np.quantile(gaps, q)) for q in (0.1, 0.5, 0.9, 0.99)},
        "drop_reuse_within_s": {
            str(x): int((d & reuse & (wait <= x)).sum()) for x in (60, 120, 300, 600, 1800, 3600)
        },
        "drop_repetitions": repeat(d),
        "write": int(w.sum()),
        "write_no_future": int((w & ~reuse).sum()),
        "write_at_least_1h_before_end": int((w & (t <= times[-1] - 3600 * 10**9)).sum()),
        "write_no_future_at_least_1h_before_end": int((w & ~reuse & (t <= times[-1] - 3600 * 10**9)).sum()),
        "write_repetitions": repeat(w),
        "eviction_repetitions": repeat(np.ones(len(ids), dtype=bool)),
        "observed_evictions_no_future": int((~reuse).sum()),
    }
    rows.append(row)
    (out / (name + ".diagnosis.json")).write_text(json.dumps(row, indent=2))
    print(json.dumps(row), flush=True)
(out / "diagnosis.json").write_text(json.dumps(rows, indent=2))
