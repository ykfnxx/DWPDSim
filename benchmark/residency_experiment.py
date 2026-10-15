"""No-hint soft residency: frozen return and prefix models, chronological replay."""

from __future__ import annotations

import argparse
import fcntl
import json
import subprocess
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import reuse_policy_experiment as replay

ROOT = replay.ROOT
DEFAULT = ROOT / "build/residency-nohint"
PREVIOUS = ROOT / "build/reuse-policy-experiment"


def variants():
    base = {
        "kind": "reuse_lru",
        "admit_storage_hits": True,
        "alpha": 0.1,
        "max_eviction_blocks": 64,
        "retention_ns": 300 * 10**9,
    }
    return {
        "dump-all": {**base, "retention_ns": None},
        "lru-300": base,
        "return-60": {**base, "reuse_priority": True},
        "prefix-60": {**base, "reuse_priority": True},
        "value-60": {**base, "reuse_priority": True, "reuse_cost_weighted": True},
        "value-300": {
            **base,
            "reuse_priority": True,
            "reuse_cost_weighted": True,
            "reuse_short_horizon_s": 300,
        },
    }


def prepare(out):
    out.mkdir(parents=True, exist_ok=True)
    manifest = {
        "no_hint": True,
        "model_input_fields": ["timestamp_ns", "affinity_id", "hash_ids"],
        "train_seconds": [0, 21600],
        "evaluation_seconds": [21600, 86400],
        "variants": variants(),
        "sources": {},
    }
    for name, source in replay.SOURCES.items():
        dest = out / name
        dest.mkdir(exist_ok=True)
        old = PREVIOUS / name
        info = json.loads((source / "dataset.json").read_text())
        assert replay.sha(info["source"]) == info["sha256"]
        for filename in ["evaluation.parquet", "requests.npy", "fit.json"]:
            target = dest / filename
            if not target.exists():
                target.symlink_to(old / filename)
        events = np.zeros((9, len(replay.EDGES) - 1), dtype=np.int64)
        total = np.zeros((*events.shape, 3), dtype=np.int64)
        kept = np.zeros_like(total)
        sessions = {}
        returns = [[] for _ in range(9)]
        for batch in pq.ParquetFile(source / "input.parquet").iter_batches(batch_size=512):
            if batch.column("timestamp_ns")[0].as_py() >= replay.SPLIT:
                break
            for r in batch.to_pylist():
                t, sid, hashes = r["timestamp_ns"], r["affinity_id"], r["hash_ids"]
                if t >= replay.SPLIT:
                    break
                prev = sessions.get(sid)
                gap = None
                if prev is not None:
                    pt, g, before = prev
                    gap = (t - pt) / 1e9
                    returns[g].append((gap, True))
                    k = min(
                        len(replay.EDGES) - 2,
                        int(np.searchsorted(replay.EDGES, gap, side="right")) - 1,
                    )
                    lcp = 0
                    for a, b in zip(before, hashes):
                        if a != b:
                            break
                        lcp += 1
                    n = len(before)
                    events[g, k] += 1
                    for depth in range(3):
                        lo, hi = (depth * n + 2) // 3, ((depth + 1) * n + 2) // 3
                        total[g, k, depth] += hi - lo
                        kept[g, k, depth] += max(0, min(hi, lcp) - lo)
                sessions[sid] = (t, replay.group(len(hashes), gap), hashes)
        for t, g, _ in sessions.values():
            returns[g].append(((replay.SPLIT - t) / 1e9, False))
        pooled = [x for row in returns for x in row]
        curves = [replay.km(row if len(row) >= 50 else pooled) for row in returns]
        # Sparse cells pool across gap bins within group, then all training pairs.
        probabilities = np.ones_like(total, dtype=float)
        fallbacks = []
        for g in range(9):
            for k in range(events.shape[1]):
                a, b = kept[g, k], total[g, k]
                fallback = "cell"
                if events[g, k] < 50:
                    a, b = kept[g].sum(axis=0), total[g].sum(axis=0)
                    fallback = "group"
                if events[g].sum() < 50:
                    a, b = kept.sum(axis=(0, 1)), total.sum(axis=(0, 1))
                    fallback = "pooled"
                probabilities[g, k] = np.divide(a, b, out=np.ones(3), where=b > 0)
                fallbacks.append([g, k, fallback])
        model = {
            "reuse_edges_s": replay.EDGES,
            "reuse_survival": curves,
            "reuse_prefix_keep": probabilities.tolist(),
        }
        prior = json.loads((old / "model.json").read_text())
        assert model["reuse_survival"] == prior["reuse_survival"]
        (dest / "model.json").write_text(json.dumps(model, indent=2) + "\n")
        (dest / "prefix_fit.json").write_text(
            json.dumps(
                {
                    "events": events.tolist(),
                    "prefix_blocks": kept.tolist(),
                    "total_blocks": total.tolist(),
                    "fallback": fallbacks,
                    "depth_bins": "floor(3 * zero_based_position / input_blocks)",
                    "censored_prefix_pairs": "excluded; prefix fate unknown until return",
                    "pooled_keep": (kept.sum(axis=(0, 1)) / total.sum(axis=(0, 1))).tolist(),
                },
                indent=2,
            )
            + "\n"
        )
        manifest["sources"][name] = {
            **info,
            "parquet_sha256": replay.sha(source / "input.parquet"),
            "model_sha256": replay.sha(dest / "model.json"),
            "evaluation_sha256": replay.sha(dest / "evaluation.parquet"),
        }
        print("FITTED", name, int(events.sum()), "prefix pairs", flush=True)
    manifest["head"] = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    manifest["diff"] = subprocess.check_output(["git", "diff"], text=True)
    (out / "provenance.json").write_text(json.dumps(manifest, indent=2) + "\n")


def options_for(out, dataset, variant):
    v = variants()[variant]
    model = json.loads((out / dataset / "model.json").read_text())
    if variant in ["dump-all", "lru-300"]:
        model.pop("reuse_prefix_keep")
    elif variant == "return-60":
        model["reuse_prefix_keep"] = np.ones_like(model["reuse_prefix_keep"]).tolist()
    return {**model, **v}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["prepare", "run", "worker", "summarize"])
    p.add_argument("--output", type=Path, default=DEFAULT)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--dataset", choices=list(replay.SOURCES))
    p.add_argument("--capacity", type=int)
    p.add_argument("--variant", choices=list(variants()))
    a = p.parse_args()
    if a.mode == "prepare":
        prepare(a.output)
        return
    if a.mode == "worker":
        folder = a.output / a.dataset / f"{a.capacity}-{a.variant}"
        folder.mkdir(exist_ok=True)
        with (folder / "worker.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not (folder / "result.json").exists():
                replay.measure(
                    a.output,
                    a.dataset,
                    a.capacity,
                    a.variant,
                    options=options_for(a.output, a.dataset, a.variant),
                )
    elif a.mode == "summarize":
        replay.summarize(a.output, variant_names=list(variants()))
    else:
        replay.run(a.output, a.workers, configurations=variants(), worker_script=__file__)


if __name__ == "__main__":
    main()
