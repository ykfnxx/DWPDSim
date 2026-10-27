"""Prepare and replay the complete pinned WEKA 062126 corpus as Kimi-K3 inputs.

All root traces start together; nested request times already use the root clock.
The hash stream stays on disk so 338 million references need not become Python
objects at once. The simulator retains the observed prefix topology and state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import sys
import time
from collections import Counter
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import numpy as np

import dwpdsim
from dwpdsim import (
    DWPDSimulator,
    HybridPrefillRequest,
    MemoryConfig,
    SimulationConfig,
    StoragePolicyConfig,
    StorageTierConfig,
    _core,
    kimi_k3_profile,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SHA = "29b6a19e751ff5230771519aab755f80a0f43a4ba9cf96b72d3a6a437ec99276"
REVISION = "23f152f6f0f9399a85901b89a6458def0ef16729"


def api_requests(entries):
    for entry in entries:
        if entry["type"] == "subagent":
            yield from api_requests(entry["requests"])
        else:
            assert entry["type"] in ("n", "s")
            yield entry


def prepare(args):
    args.prepared.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    last_progress = started
    source_digest = hashlib.sha256()
    binary_digest = hashlib.sha256()
    rows = []
    offset = 0
    models = Counter()
    trace_ids = []
    with args.source.open("rb") as source, (args.prepared / "hashes.u64").open("wb") as binary:
        for trace_index, line in enumerate(source):
            source_digest.update(line)
            trace = json.loads(line, parse_float=Decimal)
            assert trace["block_size"] == 64 and trace["hash_id_scope"] == "local"
            trace_ids.append(trace["id"])
            namespace = np.uint64((trace_index + 1) << 32)
            for position, request in enumerate(api_requests(trace["requests"])):
                ids = np.asarray(request["hash_ids"], dtype="<u8")
                assert ids.size and int(ids.max()) < (1 << 32)
                assert request["in"] == len(ids) * 64
                ids |= namespace
                raw = ids.tobytes()
                binary.write(raw)
                binary_digest.update(raw)
                timestamp_ns = int(Decimal(str(request["t"])) * 1_000_000_000)
                assert timestamp_ns >= 0
                rows.append({
                    "timestamp_ns": timestamp_ns,
                    "trace_index": trace_index,
                    "position": position,
                    "offset": offset,
                    "blocks": len(ids),
                    "input_tokens": request["in"],
                    "source_model": request["model"],
                })
                offset += len(ids)
                models[request["model"]] += 1
            if time.perf_counter() - last_progress >= 5:
                print(json.dumps({"phase": "prepare", "traces": trace_index + 1,
                                  "requests": len(rows), "blocks": offset}), flush=True)
                last_progress = time.perf_counter()
    assert source_digest.hexdigest() == SOURCE_SHA
    assert len(trace_ids) == 393 and len(set(trace_ids)) == 393
    assert len(rows) == 98_827 and offset == 338_052_834
    rows.sort(key=lambda row: (row["timestamp_ns"], row["trace_index"], row["position"]))
    with (args.prepared / "requests.jsonl").open("w") as output:
        for row in rows:
            output.write(json.dumps(row) + "\n")
    manifest = {
        "dataset": "semianalysisai/cc-traces-weka-062126",
        "revision": REVISION,
        "source_path": str(args.source.resolve()),
        "source_bytes": args.source.stat().st_size,
        "source_sha256": source_digest.hexdigest(),
        "hashes_sha256": binary_digest.hexdigest(),
        "index_sha256": hashlib.sha256((args.prepared / "requests.jsonl").read_bytes()).hexdigest(),
        "hash_dtype": "little-endian uint64",
        "namespace": "((trace_index + 1) << 32) | raw_hash_id; descendants share root namespace",
        "timeline": "all root starts at zero; nested t already root-relative; stable sort by (t, trace_index, recursive position)",
        "target_model": "all source model calls mapped to one Kimi-K3 logical cache",
        "tokens_per_block": 64,
        "traces": len(trace_ids),
        "requests": len(rows),
        "input_token_blocks": offset,
        "input_tokens": sum(row["input_tokens"] for row in rows),
        "start_ns": rows[0]["timestamp_ns"],
        "end_ns": rows[-1]["timestamp_ns"],
        "source_model_requests": dict(models),
        "trace_ids": trace_ids,
        "prepare_seconds": time.perf_counter() - started,
        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
    }
    (args.prepared / "dataset.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({key: value for key, value in manifest.items() if key != "trace_ids"}, indent=2))


def run(args):
    native_sha = hashlib.sha256(Path(_core.__file__).read_bytes()).hexdigest()
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((args.prepared / "dataset.json").read_text())
    hashes = np.memmap(args.prepared / "hashes.u64", mode="r", dtype="<u8")
    assert len(hashes) == manifest["input_token_blocks"]
    index = [json.loads(line) for line in (args.prepared / "requests.jsonl").open()]
    if args.requests:
        index = index[:args.requests]
    input_tokens = sum(row["input_tokens"] for row in index)
    profile = kimi_k3_profile(tokens_per_block=64)
    unit = 1 << 20
    slc_capacity = (args.capacity_tb * 10**12 // (3 * unit)) * unit
    infinite = args.storage_policy == "infinite_storage"
    cfg = SimulationConfig(
        memory=MemoryConfig(args.memory_bytes),
        slc=StorageTierConfig(unit if infinite else slc_capacity, 1),
        tlc=StorageTierConfig(unit if infinite else 2*slc_capacity, 1),
        block_size_bytes=unit,
        storage_policy=StoragePolicyConfig(
            kind=args.storage_policy, slc_host_share=3/17,
            slc_erase_budget=3, tlc_erase_budget=7,
        ),
        hybrid_attention=profile.hybrid_config(
            pool_allocation=args.pool_allocation,
            full_capacity_bytes=args.full_bytes, linear_capacity_bytes=args.linear_bytes,
            lru_k=args.k, emit_io_trace=args.emit_io_trace,
        ),
    )
    (args.output / "config.json").write_text(json.dumps(asdict(cfg), indent=2) + "\n")
    core_seconds = 0.0
    started = time.perf_counter()
    last_progress = started
    with DWPDSimulator(cfg, args.output / "io") as sim:
        for start in range(0, len(index), args.batch_size):
            batch = []
            for sequence, row in enumerate(index[start:start + args.batch_size], start):
                first = row["offset"]
                request = HybridPrefillRequest(
                    timestamp_ns=row["timestamp_ns"], sequence=sequence, request_id=sequence,
                    input_tokens=row["input_tokens"],
                    hash_ids=hashes[first:first + row["blocks"]].tolist(),
                    affinity_id=row["trace_index"] + 1,
                )
                batch.append(sim._prefill_request(request))
            core_start = time.perf_counter()
            sim._core.run_hybrid_prefill_requests(batch)
            core_seconds += time.perf_counter() - core_start
            if time.perf_counter() - last_progress >= 10:
                stats = sim.hybrid_stats()
                print(json.dumps({"phase": "replay", "requests": stats["requests"],
                                  "total": len(index), "elapsed_seconds": time.perf_counter() - started,
                                  "joint_token_hit_rate": stats["prefill"]["joint_token_hit_rate"],
                                  "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024}), flush=True)
                last_progress = time.perf_counter()
        elapsed = time.perf_counter() - started
        stats = sim.hybrid_stats()
    assert (args.output / "io.hybrid.csv").exists() == args.emit_io_trace
    assert stats["requests"] == len(index)
    prefill = stats["prefill"]
    assert prefill["input_tokens"] == input_tokens
    assert prefill["restored_tokens"] + prefill["prefill_compute_tokens"] == prefill["input_tokens"]
    assert prefill["restored_tokens"] + prefill["replayed_prefix_tokens"] == prefill["matched_prefix_tokens"]
    assert prefill["matched_prefix_tokens"] + prefill["new_suffix_tokens"] == prefill["input_tokens"]
    assert prefill["generated_checkpoints"] == len(index)
    assert stats["full"]["demands"] * 64 == prefill["restored_tokens"]
    assert stats["linear"]["demands"] == prefill["restored_requests"]
    for name, capacity in (("full", args.full_bytes), ("linear", args.linear_bytes)):
        if args.pool_allocation != "fixed":
            capacity = args.memory_bytes
        assert stats[name]["peak_resident_bytes"] <= capacity
    if args.pool_allocation != "fixed":
        assert stats["pool"]["peak_resident_bytes"] <= args.memory_bytes
    report = {
        "dataset_manifest": str((args.prepared / "dataset.json").resolve()),
        "source_sha256": manifest["source_sha256"],
        "replayed_requests": len(index),
        "full_corpus": len(index) == manifest["requests"],
        "last_request_timestamp_ns": index[-1]["timestamp_ns"],
        "trace_path": str((args.output / "io.hybrid.csv").resolve()) if args.emit_io_trace else None,
        "model": "Kimi-K3",
        "model_config_revision": profile.config_revision,
        "cache_implementation_revision": profile.cache_implementation_revision,
        "package_path": str(Path(dwpdsim.__file__).resolve()),
        "native_extension_path": str(Path(_core.__file__).resolve()),
        "native_extension_sha256": native_sha,
        "python_version": sys.version,
        "scope": "prefill-only logical whole-model payload; synchronous requests; no decode-only hashes, SSD timing or physical WAF",
        "cache_start": "empty",
        "timeline": manifest["timeline"],
        "config": asdict(cfg),
        "replay_seconds": elapsed,
        "replay_timing_scope": "simulator construction, mmap input reads, Python adaptation, binding construction and native replay; excludes dataset preparation, index loading, simulator close and final serialization",
        "native_aggregate_seconds": core_seconds,
        "native_timing_scope": "aggregate pybind call including batch copy and native replay",
        "requests_per_second": len(index) / elapsed,
        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        "rss_scope": "simulator metadata, request index, mapped hash pages and batch buffers; no KV tensors allocated",
        "stats": stats,
    }
    (args.output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preparation = commands.add_parser("prepare")
    preparation.add_argument("--source", type=Path, required=True)
    preparation.add_argument("--prepared", type=Path, required=True)
    replay = commands.add_parser("run")
    replay.add_argument("--prepared", type=Path, required=True)
    replay.add_argument("--output", type=Path, required=True)
    replay.add_argument("--memory-bytes", type=int, default=100_000_000_000)
    replay.add_argument("--pool-allocation", choices=("shared", "fixed", "adaptive"), default="shared")
    replay.add_argument("--full-bytes", type=int, default=0, help="component cap for explicit fixed allocation only")
    replay.add_argument("--linear-bytes", type=int, default=0, help="component cap for explicit fixed allocation only")
    replay.add_argument("--k", type=int, default=2)
    replay.add_argument("--batch-size", type=int, default=64)
    replay.add_argument("--requests", type=int, default=0, help="0 replays the complete corpus")
    replay.add_argument("--capacity-tb", type=int, choices=(1, 4), default=1)
    replay.add_argument("--storage-policy", choices=("infinite_storage", "wear_share_round_robin", "adaptive_endurance"), default="infinite_storage")
    replay.add_argument("--emit-io-trace", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args)
    else:
        run(args)


if __name__ == "__main__":
    main()
