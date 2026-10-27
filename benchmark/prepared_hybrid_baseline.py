"""Replay prepared uint64 request inputs through the shared Kimi-K3 baseline.

The input directory contains dataset.json, requests.jsonl and hashes.u64. Each
run uses the original timestamps and affinity IDs, starts empty, and finishes
at a frozen observation endpoint. No decode identities are inferred.
"""

import argparse
import hashlib
import json
import mmap
import resource
import sys
import time
from dataclasses import asdict
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

HOUR_NS = 3600 * 10**9


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def configuration(manifest, args):
    unit = 1 << 20
    slc = args.capacity_tb * 10**12 // (3 * unit) * unit
    infinite = args.storage_policy == "infinite_storage"
    profile = kimi_k3_profile(tokens_per_block=manifest["tokens_per_block"])
    return SimulationConfig(
        memory=MemoryConfig(args.memory_bytes), block_size_bytes=unit,
        slc=StorageTierConfig(unit if infinite else slc, 1),
        tlc=StorageTierConfig(unit if infinite else 2 * slc, 1),
        storage_policy=StoragePolicyConfig(
            kind=args.storage_policy, slc_host_share=3 / 17,
            slc_erase_budget=3, tlc_erase_budget=7,
        ),
        hybrid_attention=profile.hybrid_config(lru_k=args.k, emit_io_trace=args.emit_io_trace),
        simulation_end_ns=round(args.end_seconds * 10**9),
    )


def audit_stats(stats, cfg, count, tokens, checkpoints):
    assert stats["requests"] == count
    p = stats["prefill"]
    assert p["input_tokens"] == tokens
    assert p["generated_checkpoints"] == checkpoints
    assert p["restored_tokens"] + p["prefill_compute_tokens"] == tokens
    assert p["restored_tokens"] + p["replayed_prefix_tokens"] == p["matched_prefix_tokens"]
    assert p["matched_prefix_tokens"] + p["new_suffix_tokens"] == tokens
    assert stats["full"]["demands"] * cfg.hybrid_attention.tokens_per_block == p["restored_tokens"]
    assert stats["linear"]["demands"] == p["restored_requests"]
    assert 0 <= p["dram_restored_requests"] <= p["restored_requests"] <= count
    pool = stats["pool"]
    assert pool["allocation"] == "shared" and "full_target_bytes" not in pool
    assert pool["resident_bytes"] == sum(stats[c]["resident_bytes"] for c in ("full", "linear"))
    assert pool["resident_bytes"] <= pool["peak_resident_bytes"] <= cfg.memory.capacity_bytes
    for c in ("full", "linear"):
        component = stats[c]
        assert component["drop_objects"] == 0
        assert component["dram_hits"] + component["storage_hits"] == component["demands"]
    if cfg.storage_policy.kind != "infinite_storage":
        for operation in ("read", "write"):
            assert sum(stats[c][operation + "_allocated_bytes"] for c in ("full", "linear")) == sum(
                stats["storage"][p][operation + "_bytes"] for p in ("slc", "tlc"))
        assert sum(stats[c]["storage_allocated_bytes"] for c in ("full", "linear")) == sum(
            stats["storage"][p]["used_bytes"] for p in ("slc", "tlc"))
        for name in ("slc", "tlc"):
            tier = stats["storage"][name]
            assert tier["used_bytes"] <= tier["peak_used_bytes"] <= getattr(cfg, name).capacity_bytes
            assert tier["write_bytes"] - tier["trim_bytes"] == tier["used_bytes"]


def run(args):
    manifest_path = args.prepared / "dataset.json"
    manifest = json.loads(manifest_path.read_text())
    paths = {key: args.prepared / manifest[key + "_file"] for key in ("hashes", "requests")}
    assert digest(paths["hashes"]) == manifest["hashes_sha256"]
    assert digest(paths["requests"]) == manifest["index_sha256"]
    assert paths["hashes"].stat().st_size == 8 * manifest["input_token_blocks"]
    cfg = configuration(manifest, args)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "config.json").write_text(json.dumps(asdict(cfg), indent=2) + "\n")
    print(json.dumps({"phase": "start", "policy": args.storage_policy,
                      "capacity_tb": None if args.storage_policy == "infinite_storage" else args.capacity_tb,
                      "expected_requests": args.requests or manifest["requests"],
                      "end_ns": cfg.simulation_end_ns}), flush=True)
    started = last_log = time.perf_counter()
    call_seconds = 0.0
    count = tokens = checkpoints = 0
    last_timestamp = last_sequence = -1
    hourly = []
    hour = None
    with paths["hashes"].open("rb") as source, mmap.mmap(
        source.fileno(), 0, access=mmap.ACCESS_READ,
    ) as mapped, DWPDSimulator(cfg, args.output / "io") as sim:
        batch = []

        def flush():
            nonlocal call_seconds
            if batch:
                before = time.perf_counter()
                sim.run_hybrid_prefill(batch, batch_requests=args.batch_size)
                call_seconds += time.perf_counter() - before
                batch.clear()
                # Each contiguous input slice is consumed once. Release mapped
                # pages after adaptation instead of retaining the entire input.
                mapped.madvise(mmap.MADV_DONTNEED)

        with paths["requests"].open() as index:
            for line in index:
                if args.requests and count >= args.requests:
                    break
                row = json.loads(line)
                assert last_timestamp <= row["timestamp_ns"] <= cfg.simulation_end_ns
                assert row["sequence"] > last_sequence
                current_hour = row["timestamp_ns"] // HOUR_NS
                if hour is not None and current_hour != hour:
                    flush()
                    hourly.append({"hour": hour, "last_request_timestamp_ns": last_timestamp,
                                   "stats": sim.hybrid_stats()})
                hour = current_hour
                block_ids = np.frombuffer(mapped, dtype="<u8", count=row["blocks"],
                                          offset=row["offset"] * 8).tolist()
                batch.append(HybridPrefillRequest(
                    row["timestamp_ns"], row["sequence"], row["request_id"], block_ids,
                    input_tokens=row["input_tokens"], affinity_id=row["affinity_id"],
                ))
                count += 1
                tokens += row["input_tokens"]
                checkpoints += row["blocks"] > 0
                last_timestamp, last_sequence = row["timestamp_ns"], row["sequence"]
                if len(batch) == args.batch_size:
                    flush()
                    if time.perf_counter() - last_log >= 15:
                        stats = sim.hybrid_stats()
                        print(json.dumps({"phase": "replay", "requests": count,
                                          "total": args.requests or manifest["requests"],
                                          "virtual_hour": last_timestamp / HOUR_NS,
                                          "elapsed_seconds": time.perf_counter() - started,
                                          "joint_token_hit_rate": stats["prefill"]["joint_token_hit_rate"],
                                          "tree_nodes": stats["tree_nodes"],
                                          "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024}), flush=True)
                        last_log = time.perf_counter()
        flush()
        sim.finish()
        stats = sim.hybrid_stats()
        hourly.append({"hour": hour, "last_request_timestamp_ns": last_timestamp, "stats": stats})
    elapsed = time.perf_counter() - started
    audit_stats(stats, cfg, count, tokens, checkpoints)
    full = count == manifest["requests"]
    if full:
        assert tokens == manifest["input_tokens"]
        source_audit = args.prepared.parent / "audit.json"
        if source_audit.exists():
            oracle = json.loads(source_audit.read_text())["input_only_oracle"]
            assert stats["prefill"]["matched_prefix_tokens"] == oracle["matched_prefix_tokens"]
            assert stats["prefill"]["restored_tokens"] <= oracle["restored_tokens"]
            if args.storage_policy == "infinite_storage":
                assert stats["prefill"]["restored_tokens"] == oracle["restored_tokens"]
    assert (args.output / "io.hybrid.csv").exists() == args.emit_io_trace
    profile = kimi_k3_profile(tokens_per_block=manifest["tokens_per_block"])
    report = {
        "status": "passed", "dataset_manifest": str(manifest_path.resolve()),
        "dataset_manifest_sha256": digest(manifest_path),
        "hashes_sha256": manifest["hashes_sha256"], "index_sha256": manifest["index_sha256"],
        "replayed_requests": count, "full_corpus": full,
        "last_request_timestamp_ns": last_timestamp,
        "trace_path": str((args.output / "io.hybrid.csv").resolve()) if args.emit_io_trace else None,
        "model": "Kimi-K3", "model_config_revision": profile.config_revision,
        "cache_implementation_revision": profile.cache_implementation_revision,
        "package_path": str(Path(dwpdsim.__file__).resolve()),
        "native_extension_path": str(Path(_core.__file__).resolve()),
        "native_extension_sha256": digest(Path(_core.__file__)),
        "runner_sha256": digest(Path(__file__)), "python_version": sys.version,
        "config": asdict(cfg), "cache_start": "empty", "timeline": manifest["timeline"],
        "scope": "complete input-only hybrid logical replay; original branch identities and timing; no inferred decode supply or physical SSD timing/WAF",
        "replay_seconds": elapsed, "aggregate_call_seconds": call_seconds,
        "timing_scope": "replay includes input mmap reads, adaptation, construction, finish and close; excludes digest verification and serialization",
        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        "stats": stats,
    }
    (args.output / "hourly.json").write_text(json.dumps(hourly, indent=2) + "\n")
    (args.output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"phase": "complete", "requests": count, "seconds": elapsed,
                      "joint_token_hit_rate": stats["prefill"]["joint_token_hit_rate"],
                      "result": str((args.output / "result.json").resolve())}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--storage-policy", choices=("infinite_storage", "wear_share_round_robin", "adaptive_endurance"), required=True)
    parser.add_argument("--capacity-tb", type=int, choices=(1, 4), default=4)
    parser.add_argument("--memory-bytes", type=int, default=100_000_000_000)
    parser.add_argument("--k", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--end-seconds", type=float, default=86400)
    parser.add_argument("--requests", type=int, default=0, help="0 replays every request")
    parser.add_argument("--emit-io-trace", action="store_true")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
