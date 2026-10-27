"""Prepare pinned Qwen Bailian Trace A and replay shared-DRAM Kimi-K3.

Raw hashes name token blocks, not globally unique prefix nodes. Complete blocks
are interned by (parent prefix, raw block hash); the partial tail remains compute.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import sys
import time
from array import array
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

SOURCE_SHA256 = "07cedc9ed8aff301994ac68ed4aede8123b7603673575eeba9dd677de663db17"
SOURCE_REVISION = "5f7439c51ec248a0c585f7d90a41a6f57773b912"
TOKENS_PER_BLOCK = 16


def prepare(args):
    assert sys.byteorder == "little"
    args.prepared.mkdir(parents=True, exist_ok=False)
    chat_parents = {}
    with args.source.open() as source:
        for line in source:
            row = json.loads(line)
            assert row["chat_id"] not in chat_parents
            chat_parents[row["chat_id"]] = row["parent_chat_id"]
    roots = {}
    for chat in chat_parents:
        path = []
        ancestor = chat
        while ancestor not in roots:
            path.append(ancestor)
            parent = chat_parents[ancestor]
            if parent == -1:
                roots[ancestor] = ancestor
                break
            assert parent in chat_parents and parent not in path
            ancestor = parent
        for node in path:
            roots[node] = roots[ancestor]

    prefix_ids, first_raw_parents = {}, {}
    changed_parent_hashes = set()
    endpoints = set()
    source_digest, hash_digest = hashlib.sha256(), hashlib.sha256()
    counts = Counter()
    first_ns = last_ns = None
    with args.source.open("rb") as source, (args.prepared / "hashes.u64").open("wb") as hashes, \
            (args.prepared / "requests.jsonl").open("w") as index:
        for sequence, line in enumerate(source):
            source_digest.update(line)
            row = json.loads(line, parse_float=Decimal)
            n = row["input_length"]
            assert n > 0 and len(row["hash_ids"]) == (n + 15) // 16
            timestamp = row["timestamp"]
            exact_ns = Decimal(str(timestamp)) * 1_000_000_000
            now = int(exact_ns)
            assert now == exact_ns and now >= 0
            assert last_ns is None or now >= last_ns
            if first_ns is None:
                first_ns = now
            last_ns = now
            parent = 0
            before = len(prefix_ids)
            path = []
            matched = restored = 0
            for i, raw_hash in enumerate(row["hash_ids"][:n // 16]):
                assert isinstance(raw_hash, int) and raw_hash >= 0
                key = (parent, raw_hash)
                identity = prefix_ids.get(key)
                if identity is None:
                    identity = len(prefix_ids) + 1
                    prefix_ids[key] = identity
                if raw_hash in first_raw_parents:
                    if first_raw_parents[raw_hash] != parent:
                        counts["raw_parent_conflict_occurrences"] += 1
                        changed_parent_hashes.add(raw_hash)
                else:
                    first_raw_parents[raw_hash] = parent
                path.append(identity)
                boundary = (i + 1) * 16
                if identity <= before:
                    matched = boundary
                if boundary < n and identity in endpoints:
                    restored = boundary
                parent = identity
            # This oracle retains all historical FULL data and request endpoints.
            counts["permanent_matched_prefix_tokens"] += matched
            counts["permanent_restored_tokens"] += restored
            counts["permanent_restored_requests"] += restored > 0
            if path:
                endpoints.add(path[-1])
                counts["checkpoint_publications"] += 1
            raw = array("Q", path).tobytes()
            hashes.write(raw)
            hash_digest.update(raw)
            index.write(json.dumps({
                "timestamp_ns": now, "sequence": sequence,
                "request_id": row["chat_id"], "affinity_id": roots[row["chat_id"]] + 1,
                "input_tokens": n, "offset": counts["input_token_blocks"],
                "blocks": len(path),
            }) + "\n")
            counts["requests"] += 1
            counts["input_tokens"] += n
            counts["output_tokens"] += row["output_length"]
            counts["input_token_blocks"] += len(path)
            counts["partial_tail_requests"] += n % 16 != 0
            counts["partial_tail_tokens"] += n % 16
    assert source_digest.hexdigest() == SOURCE_SHA256
    manifest = {
        "source_path": str(args.source.resolve()), "source_sha256": SOURCE_SHA256,
        "source_revision": SOURCE_REVISION,
        "source_url": f"https://github.com/alibaba-edu/qwen-bailian-usagetraces-anon/blob/{SOURCE_REVISION}/qwen_traceA_blksz_16.jsonl",
        "tokens_per_block": 16, "hashes_sha256": hash_digest.hexdigest(),
        "unique_prefix_nodes": len(prefix_ids), "unique_raw_full_hashes": len(first_raw_parents),
        "raw_hashes_with_multiple_parents": len(changed_parent_hashes),
        "root_sessions": len(set(roots.values())), "unique_endpoints": len(endpoints),
        "first_timestamp_ns": first_ns, "last_timestamp_ns": last_ns,
        "timeline": "original file order and original trace-relative seconds converted exactly to nanoseconds; no sorting or scaling",
        "identity": "intern (canonical parent prefix ID, original block hash); IDs start at 1",
        "tail": "ignore only the final partial-block hash; preserve all input tokens as demand/compute",
        "supply": "request-end checkpoint at the final complete block; no decode hashes inferred from output_length",
        **counts,
    }
    manifest["permanent_joint_token_hit_rate"] = counts["permanent_restored_tokens"] / counts["input_tokens"]
    (args.prepared / "dataset.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2), flush=True)


def run(args):
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = json.loads((args.prepared / "dataset.json").read_text())
    assert manifest["tokens_per_block"] == TOKENS_PER_BLOCK
    native_sha = hashlib.sha256(Path(_core.__file__).read_bytes()).hexdigest()
    unit = 1 << 20
    slc = args.capacity_tb * 10**12 // (3 * unit) * unit
    infinite = args.storage_policy == "infinite_storage"
    profile = kimi_k3_profile(tokens_per_block=TOKENS_PER_BLOCK)
    cfg = SimulationConfig(
        memory=MemoryConfig(100_000_000_000), block_size_bytes=unit,
        slc=StorageTierConfig(unit if infinite else slc, 1),
        tlc=StorageTierConfig(unit if infinite else 2 * slc, 1),
        storage_policy=StoragePolicyConfig(kind=args.storage_policy,
                                          slc_host_share=3/17, slc_erase_budget=3, tlc_erase_budget=7),
        hybrid_attention=profile.hybrid_config(lru_k=2, emit_io_trace=args.emit_io_trace),
    )
    (args.output / "config.json").write_text(json.dumps(asdict(cfg), indent=2) + "\n")
    hashes = np.memmap(args.prepared / "hashes.u64", mode="r", dtype="<u8")
    assert len(hashes) == manifest["input_token_blocks"]
    started = last_log = time.perf_counter()
    native_seconds = 0.0
    count = input_tokens = checkpoints = 0
    with DWPDSimulator(cfg, args.output / "io") as sim, (args.prepared / "requests.jsonl").open() as index:
        batch = []
        for line in index:
            row = json.loads(line)
            lo, blocks = row["offset"], row["blocks"]
            batch.append(HybridPrefillRequest(
                row["timestamp_ns"], row["sequence"], row["request_id"],
                hashes[lo:lo+blocks].tolist(), input_tokens=row["input_tokens"],
                affinity_id=row["affinity_id"],
            ))
            count += 1
            input_tokens += row["input_tokens"]
            checkpoints += blocks > 0
            if len(batch) == args.batch_size:
                before = time.perf_counter()
                sim.run_hybrid_prefill(batch, batch_requests=args.batch_size)
                native_seconds += time.perf_counter() - before
                batch.clear()
                if time.perf_counter() - last_log >= 10:
                    stats = sim.hybrid_stats()
                    print(json.dumps({"phase": "replay", "requests": count,
                                      "total": manifest["requests"],
                                      "elapsed_seconds": time.perf_counter() - started,
                                      "joint_token_hit_rate": stats["prefill"]["joint_token_hit_rate"],
                                      "tree_nodes": stats["tree_nodes"],
                                      "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024}), flush=True)
                    last_log = time.perf_counter()
        if batch:
            before = time.perf_counter()
            sim.run_hybrid_prefill(batch, batch_requests=args.batch_size)
            native_seconds += time.perf_counter() - before
        sim.finish()
        stats = sim.hybrid_stats()
    elapsed = time.perf_counter() - started
    assert count == manifest["requests"] == stats["requests"]
    assert (args.output / "io.hybrid.csv").exists() == args.emit_io_trace
    prefill = stats["prefill"]
    assert input_tokens == manifest["input_tokens"] == prefill["input_tokens"]
    assert checkpoints == manifest["checkpoint_publications"] == prefill["generated_checkpoints"]
    assert prefill["restored_tokens"] + prefill["prefill_compute_tokens"] == input_tokens
    assert prefill["restored_tokens"] + prefill["replayed_prefix_tokens"] == prefill["matched_prefix_tokens"]
    assert prefill["matched_prefix_tokens"] + prefill["new_suffix_tokens"] == input_tokens
    assert prefill["matched_prefix_tokens"] == manifest["permanent_matched_prefix_tokens"]
    assert prefill["restored_tokens"] <= manifest["permanent_restored_tokens"]
    assert stats["full"]["demands"] * 16 == prefill["restored_tokens"]
    assert stats["linear"]["demands"] == prefill["restored_requests"]
    pool = stats["pool"]
    assert pool["allocation"] == "shared" and "full_target_bytes" not in pool
    assert pool["resident_bytes"] == sum(stats[c]["resident_bytes"] for c in ("full", "linear"))
    assert pool["resident_bytes"] <= pool["peak_resident_bytes"] <= cfg.memory.capacity_bytes
    if not infinite:
        for operation in ("read", "write"):
            assert sum(stats[c][operation+"_allocated_bytes"] for c in ("full", "linear")) == sum(
                stats["storage"][p][operation+"_bytes"] for p in ("slc", "tlc"))
    result = {
        "dataset_manifest": str((args.prepared / "dataset.json").resolve()),
        "source_sha256": SOURCE_SHA256, "source_revision": SOURCE_REVISION,
        "replayed_requests": count, "full_corpus": True,
        "last_request_timestamp_ns": manifest["last_timestamp_ns"],
        "trace_path": str((args.output / "io.hybrid.csv").resolve()) if args.emit_io_trace else None,
        "model": "Kimi-K3", "model_config_revision": profile.config_revision,
        "cache_implementation_revision": profile.cache_implementation_revision,
        "package_path": str(Path(dwpdsim.__file__).resolve()),
        "native_extension_path": str(Path(_core.__file__).resolve()),
        "native_extension_sha256": native_sha, "config": asdict(cfg),
        "cache_start": "empty", "timeline": manifest["timeline"],
        "scope": "complete Bailian Trace A request inputs; Kimi-K3 hybrid payload, prefill only, no decode-only hashes or physical SSD timing/WAF",
        "replay_seconds": elapsed, "aggregate_call_seconds": native_seconds,
        "timing_scope": "replay includes input mmap reads, adaptation, simulator construction, finish and close; aggregate_call_seconds includes Python public batching and native replay",
        "peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        "stats": stats,
    }
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)


def audit(args):
    """Check every source/reference with dense node labels, not the interning map."""
    manifest = json.loads((args.prepared / "dataset.json").read_text())
    source_path = Path(manifest["source_path"])
    hashes_path = args.prepared / "hashes.u64"
    assert hashlib.sha256(source_path.read_bytes()).hexdigest() == SOURCE_SHA256
    assert hashlib.sha256(hashes_path.read_bytes()).hexdigest() == manifest["hashes_sha256"]
    session_parents = {}
    with source_path.open() as source:
        for line in source:
            row = json.loads(line)
            session_parents[row["chat_id"]] = row["parent_chat_id"]
    hashes = np.memmap(hashes_path, mode="r", dtype="<u8")
    size = manifest["unique_prefix_nodes"] + 1
    labels = np.zeros(size, dtype=np.uint64)
    parents = np.zeros(size, dtype=np.uint64)
    seen = np.zeros(size, dtype=np.bool_)
    endpoints = np.zeros(size, dtype=np.bool_)
    counts = Counter()
    offset = 0
    with source_path.open() as source, (args.prepared / "requests.jsonl").open() as index:
        for sequence, (line, prepared) in enumerate(zip(source, index, strict=True)):
            row = json.loads(line, parse_float=Decimal)
            q = json.loads(prepared)
            n = row["input_length"]
            assert q["sequence"] == sequence and q["request_id"] == row["chat_id"]
            root = row["chat_id"]
            while session_parents[root] != -1:
                root = session_parents[root]
            assert q["affinity_id"] == root + 1
            assert q["input_tokens"] == n and q["blocks"] == n // 16 and q["offset"] == offset
            assert q["timestamp_ns"] == Decimal(str(row["timestamp"])) * 1_000_000_000
            path = hashes[offset:offset+q["blocks"]].tolist()
            assert len(path) == q["blocks"]
            parent = 0
            matched = restored = 0
            continuous = True
            for i, (identity, raw_hash) in enumerate(zip(path, row["hash_ids"][:n // 16], strict=True)):
                assert 0 < identity < size
                if seen[identity]:
                    assert int(parents[identity]) == parent and int(labels[identity]) == raw_hash
                else:
                    parents[identity], labels[identity] = parent, raw_hash
                continuous = continuous and bool(seen[identity])
                boundary = (i + 1) * 16
                if continuous:
                    matched = boundary
                    if boundary < n and endpoints[identity]:
                        restored = boundary
                parent = identity
            seen[path] = True
            if path:
                endpoints[path[-1]] = True
            counts["requests"] += 1
            counts["input_tokens"] += n
            counts["permanent_matched_prefix_tokens"] += matched
            counts["permanent_restored_tokens"] += restored
            counts["permanent_restored_requests"] += restored > 0
            offset += len(path)
    assert offset == len(hashes) and seen[1:].all()
    assert np.all(parents[1:] < np.arange(1, size, dtype=np.uint64))
    # Equal original blocks under an equal canonical parent must have one identity.
    order = np.lexsort((labels[1:], parents[1:])) + 1
    assert not np.any((parents[order[1:]] == parents[order[:-1]]) &
                      (labels[order[1:]] == labels[order[:-1]]))
    for key, count in counts.items():
        assert count == manifest[key], (key, count, manifest[key])
    result = {"status": "passed", "source_sha256": SOURCE_SHA256,
              "checked_complete_block_references": offset,
              "unique_prefix_nodes": size - 1, "all_prefix_identities_verified": True,
              "all_request_session_affinities_verified": True,
              "permanent_oracle": dict(counts)}
    (args.prepared / "audit.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--source", type=Path, required=True)
    prep.add_argument("--prepared", type=Path, required=True)
    check = commands.add_parser("audit")
    check.add_argument("--prepared", type=Path, required=True)
    replay = commands.add_parser("run")
    replay.add_argument("--prepared", type=Path, required=True)
    replay.add_argument("--output", type=Path, required=True)
    replay.add_argument("--storage-policy", choices=("infinite_storage", "wear_share_round_robin", "adaptive_endurance"), required=True)
    replay.add_argument("--capacity-tb", type=int, choices=(1, 4), default=1)
    replay.add_argument("--batch-size", type=int, default=16)
    replay.add_argument("--emit-io-trace", action="store_true")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args)
    elif args.command == "audit":
        audit(args)
    else:
        if args.batch_size <= 0:
            parser.error("--batch-size must be positive")
        run(args)


if __name__ == "__main__":
    main()
