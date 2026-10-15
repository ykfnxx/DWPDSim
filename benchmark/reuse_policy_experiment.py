"""Chronological, label-free reuse-policy ablations on two generated serving traces."""

from __future__ import annotations

import argparse
import csv
import fcntl
import functools
import hashlib
import json
import operator
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from dwpdsim import (
    DWPDSimulator,
    InputConfig,
    MemoryConfig,
    MemoryPolicyConfig,
    SimulationConfig,
    StoragePolicyConfig,
    StorageTierConfig,
    parquet_batches,
)

ROOT = Path(__file__).resolve().parents[1]
EDGES = [0, 1, 2, 4, 8, 16, 32, 60, 120, 300, 600, 1800, 3600, 7200, 21600]
SPLIT = 21600 * 10**9
SOURCES = {
    "unscaled": ROOT / "build/context-adaptive-unscaled",
    "tracelab": ROOT / "build/agent-loop-tracelab",
}


def sha(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def group(n, gap):
    return (0 if gap is None else 1 if gap <= 60 else 2) * 3 + (
        0 if n <= 128 else 1 if n <= 512 else 2
    )


def km(observations):
    """Kaplan-Meier; last observed training request is right-censored at split."""
    data = sorted(observations)
    at_risk = len(data)
    survival = 1.0
    times, values = [], []
    i = 0
    while i < len(data):
        t = data[i][0]
        j = i
        events = 0
        while j < len(data) and data[j][0] == t:
            events += data[j][1]
            j += 1
        survival *= 1 - events / at_risk
        times.append(t)
        values.append(survival)
        at_risk -= j - i
        i = j
    # S(0)=1 for interpolation; zero gaps contribute at the first positive edge.
    return [1.0] + [
        values[k] if (k := np.searchsorted(times, t, side="right") - 1) >= 0 else 1.0
        for t in EDGES[1:]
    ]


def prepare(out):
    out.mkdir(parents=True, exist_ok=True)
    provenance = {
        "head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "diff": subprocess.check_output(["git", "diff"], text=True),
        "split_ns": SPLIT,
        "sources": {},
    }
    for name, source in SOURCES.items():
        dest = out / name
        dest.mkdir(exist_ok=True)
        info = json.loads((source / "dataset.json").read_text())
        assert sha(info["source"]) == info["sha256"]
        provenance["sources"][name] = {**info, "parquet_sha256": sha(source / "input.parquet")}
        sessions = {}
        observations = [[] for _ in range(9)]
        seen = np.zeros(info["unique_blocks"] + 1, dtype=bool)
        phase = (
            np.fromfile(source / "request_phase.bin", dtype=np.uint8)
            if name == "tracelab"
            else None
        )
        loops = np.load(source / "loops.npy") if name == "tracelab" else None
        metadata = []
        eval_sessions = set()
        pf = pq.ParquetFile(source / "input.parquet")
        with pq.ParquetWriter(dest / "evaluation.parquet", pf.schema_arrow) as writer:
            for batch in pf.iter_batches(batch_size=512):
                rows = batch.to_pylist()
                evaluation = []
                for r in rows:
                    t, sid, hashes = r["timestamp_ns"], r["affinity_id"], r["hash_ids"]
                    if t < SPLIT:
                        prev = sessions.get(sid)
                        gap = None if prev is None else (t - prev[0]) / 1e9
                        if prev is not None:
                            observations[prev[1]].append((gap, True))
                        sessions[sid] = (t, group(len(hashes), gap))
                    else:
                        rid = r["request_id"]
                        cold = 0
                        for h in hashes:
                            if not seen[h]:
                                cold += 1
                                seen[h] = True
                        p = int(phase[rid]) if phase is not None else 0
                        if p and sid not in eval_sessions:
                            p = 1
                        eval_sessions.add(sid)
                        metadata.append(
                            (
                                rid,
                                t,
                                len(hashes),
                                cold,
                                p,
                                int(loops[rid]) if loops is not None else 0,
                            )
                        )
                        evaluation.append(r)
                if evaluation:
                    writer.write_table(pa.Table.from_pylist(evaluation, schema=pf.schema_arrow))
        for t, g in sessions.values():
            observations[g].append(((SPLIT - t) / 1e9, False))
        pooled = functools.reduce(operator.iadd, observations, [])
        curves = [km(x if len(x) >= 50 else pooled) for x in observations]
        model = {"reuse_edges_s": EDGES, "reuse_survival": curves}
        (dest / "model.json").write_text(json.dumps(model, indent=2) + "\n")
        (dest / "fit.json").write_text(
            json.dumps(
                {
                    "training_observations": [len(x) for x in observations],
                    "censored": [sum(not e for _, e in x) for x in observations],
                    "small_group_fallback": "pooled if fewer than 50 intervals",
                    "evaluation_requests": len(metadata),
                    "evaluation_cold_blocks": int(seen.sum()),
                    "fit_method": "Kaplan-Meier with session terminal gaps censored at 6h; linear interpolation; constant beyond last edge",
                },
                indent=2,
            )
            + "\n"
        )
        np.save(dest / "requests.npy", np.asarray(metadata, dtype=np.uint64))
        print("PREPARED", name, len(metadata), "evaluation requests", flush=True)
    files = [
        p
        for base in ["cpp", "src/dwpdsim", "benchmark", "tests"]
        for p in Path(base).rglob("*")
        if p.suffix in (".py", ".cpp", ".hpp")
    ]
    provenance["source_sha256"] = {str(p): sha(p) for p in files}
    (out / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")


def variants():
    base = {
        "kind": "reuse_lru",
        "admit_storage_hits": False,
        "alpha": 0.1,
        "max_eviction_blocks": 64,
    }
    # All new variants have identical grain and parent reclamation rules.
    return {
        "dump-all": base,
        "retention-300": {**base, "retention_ns": 300 * 10**9},
        "conditional-015": {**base, "reuse_dump_probability": 0.15},
        "conditional-035": {**base, "reuse_dump_probability": 0.35},
        "conditional-060": {**base, "reuse_dump_probability": 0.60},
        "priority-only": {**base, "retention_ns": 300 * 10**9, "reuse_priority": True},
        "admit-all": {**base, "retention_ns": 300 * 10**9, "admit_storage_hits": True},
        "admit-only": {
            **base,
            "retention_ns": 300 * 10**9,
            "admit_storage_hits": True,
            "reuse_admit_probability": 0.5,
        },
        "combined": {
            **base,
            "reuse_dump_probability": 0.35,
            "reuse_priority": True,
            "admit_storage_hits": True,
            "reuse_admit_probability": 0.5,
        },
        "indexed-300": {
            "kind": "indexed_lru",
            "admit_storage_hits": False,
            "retention_ns": 300 * 10**9,
        },
    }


def measure(out, dataset, capacity, name, *, options=None):
    folder = out / dataset
    job = folder / f"{capacity}-{name}"
    job.mkdir(exist_ok=True)
    if options is None:
        options = variants()[name]
        if options["kind"] == "reuse_lru":
            options = {**json.loads((folder / "model.json").read_text()), **options}
    config = SimulationConfig(
        block_size_bytes=8 * 1024**2,
        memory=MemoryConfig(capacity * 8 * 1024**2),
        slc=StorageTierConfig(0, 0),
        tlc=StorageTierConfig(0, 0),
        storage_policy=StoragePolicyConfig(kind="infinite_storage"),
        memory_policy=MemoryPolicyConfig(**options),
    )
    t0 = time.monotonic()
    with DWPDSimulator(config, job / "unused.csv") as sim:
        sim.enable_request_diagnostics(job / "requests.csv")
        count = 0
        for batch in parquet_batches(
            [folder / "evaluation.parquet"], InputConfig(batch_requests=512)
        ):
            sim.process_batch(*batch.buffers)
            count += len(batch.timestamps_ns)
            if count % 10240 == 0:
                print(count, "requests", flush=True)
    result = {
        "dataset": dataset,
        "capacity_blocks": capacity,
        "name": name,
        "config": asdict(config),
        "stats": sim.stats(),
        "work": sim.memory_performance(),
        "elapsed_s": time.monotonic() - t0,
    }
    metadata = np.load(folder / "requests.npy")
    diag = np.loadtxt(job / "requests.csv", delimiter=",", skiprows=1, dtype=np.uint64)
    assert np.array_equal(diag[:, 0], metadata[:, 0])
    assert np.array_equal(diag[:, 1:4].sum(axis=1), metadata[:, 2])
    assert np.all(diag[:, 3] >= metadata[:, 3])
    assert int(diag[:, 2].sum()) == result["stats"]["storage"]["tlc"]["reads"]["blocks"]
    assert len(metadata) == result["stats"]["accesses"]["requests"]
    historical_misses = diag[:, 3] - metadata[:, 3]
    historical_memory_misses = diag[:, 2] + historical_misses
    assert int(diag[:, 4].sum()) == result["stats"]["accesses"]["global_misses"]
    assert int((diag[:, 4] * metadata[:, 2]).sum()) == result["stats"]["accesses"]["compute_cost"]
    result["phases"] = {}
    for phase, mask in [
        ("all", np.ones(len(metadata), dtype=bool)),
        ("user_boundary", metadata[:, 4] == 2),
        ("within_loop", metadata[:, 4] == 3),
    ]:
        d, m = diag[mask], metadata[mask]
        hm, hmm = historical_misses[mask], historical_memory_misses[mask]
        result["phases"][phase] = {
            "requests": int(mask.sum()),
            "blocks": int(m[:, 2].sum()),
            "cold_blocks": int(m[:, 3].sum()),
            "raw_memory_hits": int(d[:, 1].sum()),
            "storage_reads": int(d[:, 2].sum()),
            "historical_misses": int(hm.sum()),
            "requests_with_historical_miss": int((hm > 0).sum()),
            "requests_with_historical_memory_miss": int((hmm > 0).sum()),
            "historical_memory_misses": int(hmm.sum()),
            "loops_with_historical_miss": len(np.unique(m[hm > 0, 5]))
            if phase == "within_loop"
            else None,
        }
    (job / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print("DONE", dataset, capacity, name, result["elapsed_s"], flush=True)


def run(out, workers, *, configurations=None, worker_script=None):
    configurations = variants() if configurations is None else configurations
    jobs = [(d, c, n) for d in SOURCES for c in [16384, 32768] for n in configurations]
    (out / "matrix.json").write_text(
        json.dumps(
            {
                "variants": configurations,
                "capacities": [16384, 32768],
                "datasets": list(SOURCES),
                "jobs": len(jobs),
                "training_hours": [0, 6],
                "evaluation_hours": [6, 24],
                "cold_start_at_evaluation": True,
                "short_horizon_s": 60,
                "long_horizon_s": 3600,
            },
            indent=2,
        )
        + "\n"
    )

    def launch(job):
        d, c, n = job
        folder = out / d / f"{c}-{n}"
        folder.mkdir(exist_ok=True)
        if (folder / "result.json").exists():
            return
        with (folder / "run.log").open("w") as f:
            subprocess.run(
                [
                    sys.executable,
                    str(worker_script or __file__),
                    "worker",
                    "--output",
                    str(out),
                    "--dataset",
                    d,
                    "--capacity",
                    str(c),
                    "--variant",
                    n,
                ],
                stdout=f,
                stderr=subprocess.STDOUT,
                check=True,
            )
        print("FINISHED", d, c, n, flush=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for f in as_completed([pool.submit(launch, j) for j in jobs]):
            f.result()
    summarize(out, variant_names=list(configurations))


def summarize(out, *, variant_names=None):
    variant_names = list(variants()) if variant_names is None else variant_names
    rows = []
    for d in SOURCES:
        for c in [16384, 32768]:
            baseline = json.loads((out / d / f"{c}-dump-all/result.json").read_text())
            bs = baseline["stats"]
            bw = bs["storage"]["tlc"]["writes"]["blocks"]
            bc = bs["accesses"]["compute_cost"]
            for n in variant_names:
                p = out / d / f"{c}-{n}/result.json"
                if not p.exists():
                    continue
                r = json.loads(p.read_text())
                s = r["stats"]
                a = s["accesses"]
                loop = r["phases"]["within_loop"]
                rows.append(
                    {
                        "dataset": d,
                        "memory_gib": c / 128,
                        "variant": n,
                        "hit_pct": 100 * (1 - a["global_misses"] / a["total"]),
                        "memory_hit_pct": 100 * r["phases"]["all"]["raw_memory_hits"] / a["total"],
                        "write_blocks": s["storage"]["tlc"]["writes"]["blocks"],
                        "write_reduction_pct": 100
                        * (1 - s["storage"]["tlc"]["writes"]["blocks"] / bw),
                        "read_blocks": r["phases"]["all"]["storage_reads"],
                        "cost_increase_pct": 100 * (a["compute_cost"] / bc - 1),
                        "historical_misses": r["phases"]["all"]["historical_misses"],
                        "loop_missing_requests": loop["requests_with_historical_miss"],
                        "loop_missing_pct": 100
                        * loop["requests_with_historical_miss"]
                        / loop["requests"]
                        if loop["requests"]
                        else None,
                        "loop_memory_missing_pct": 100
                        * loop["requests_with_historical_memory_miss"]
                        / loop["requests"]
                        if loop["requests"]
                        else None,
                        "loop_reads": loop["storage_reads"],
                        "elapsed_s": r["elapsed_s"],
                    }
                )
    with (out / "results.csv").open("w") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    (out / "results.json").write_text(json.dumps(rows, indent=2) + "\n")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["prepare", "run", "worker", "summarize"])
    p.add_argument("--output", type=Path, default=ROOT / "build/reuse-policy-experiment")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--dataset", choices=list(SOURCES))
    p.add_argument("--capacity", type=int)
    p.add_argument("--variant", choices=list(variants()))
    a = p.parse_args()
    if a.mode == "prepare":
        prepare(a.output)
    elif a.mode == "run":
        run(a.output, a.workers)
    elif a.mode == "summarize":
        summarize(a.output)
    else:
        folder = a.output / a.dataset / f"{a.capacity}-{a.variant}"
        folder.mkdir(exist_ok=True)
        with (folder / "worker.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if not (folder / "result.json").exists():
                measure(a.output, a.dataset, a.capacity, a.variant)


if __name__ == "__main__":
    main()
