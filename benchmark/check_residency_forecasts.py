"""Offline holdout calibration; labels are never passed to the cache policy."""

import argparse
import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import reuse_policy_experiment as replay


def check(folder):
    model = json.loads((folder / "model.json").read_text())
    edges = np.asarray(model["reuse_edges_s"])
    curves = np.asarray(model["reuse_survival"])
    keep = np.asarray(model["reuse_prefix_keep"])
    sessions, rows = {}, []

    def finish(prev, gap, following):
        t, group, before = prev
        lcp = 0
        for a, b in zip(before, following):
            if a != b:
                break
            lcp += 1
        n = len(before)
        sizes, retained = [], []
        for d in range(3):
            lo, hi = (d * n + 2) // 3, ((d + 1) * n + 2) // 3
            sizes.append(hi - lo)
            retained.append(max(0, min(hi, lcp) - lo))
        rows.append([t / 1e9, group, gap, *sizes, *retained])

    for batch in pq.ParquetFile(folder / "evaluation.parquet").iter_batches(batch_size=512):
        for r in batch.to_pylist():
            t, sid, hashes = r["timestamp_ns"], r["affinity_id"], r["hash_ids"]
            prev = sessions.get(sid)
            gap = None
            if prev is not None:
                gap = (t - prev[0]) / 1e9
                finish(prev, gap, hashes)
            sessions[sid] = (t, replay.group(len(hashes), gap), hashes)
    for prev in sessions.values():
        finish(prev, float("inf"), [])
    data = np.asarray(rows)
    g = data[:, 1].astype(int)
    output = []
    for h in [60, 300]:
        for age in [0, 30, 120, 300]:
            observed = (data[:, 0] + age + h <= 86400) & ((data[:, 2] > age) | (age == 0))
            s = np.asarray([np.interp(age, edges, row) for row in curves])
            later = np.asarray([np.interp(age + h, edges, row) for row in curves])
            p = np.divide(s - later, s, out=np.zeros(9), where=s > 0)
            joint = np.zeros((9, 3))
            for k in range(len(edges) - 1):
                lo, hi = max(age, edges[k]), min(age + h, edges[k + 1])
                if hi > lo:
                    mass = np.asarray(
                        [np.interp(lo, edges, row) - np.interp(hi, edges, row) for row in curves]
                    )
                    joint += mass[:, None] * keep[:, k, :]
            joint = np.divide(joint, s[:, None], out=np.zeros_like(joint), where=s[:, None] > 0)
            event = data[:, 2] <= age + h
            sizes, retained = data[:, 3:6], data[:, 6:9]
            weight = sizes[observed].sum()
            output.append(
                {
                    "age_s": age,
                    "horizon_s": h,
                    "requests": int(observed.sum()),
                    "return_predicted": float(p[g[observed]].mean()),
                    "return_observed": float(event[observed].mean()),
                    "return_brier": float(np.mean((p[g[observed]] - event[observed]) ** 2)),
                    "block_reuse_predicted": float((joint[g] * sizes)[observed].sum() / weight),
                    "block_reuse_observed": float(
                        (retained * event[:, None])[observed].sum() / weight
                    ),
                }
            )
    return output


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=Path("build/residency-nohint"))
    a = p.parse_args()
    result = {name: check(a.output / name) for name in replay.SOURCES}
    (a.output / "forecast_check.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
