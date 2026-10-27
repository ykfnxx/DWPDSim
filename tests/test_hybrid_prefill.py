"""Public prefix recovery and causal fixed-chunk generation checks."""

import csv
import json
import random
import runpy
from array import array
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path

import pytest

from dwpdsim import (
    DWPDSimulator,
    HybridAttentionConfig,
    HybridEvent,
    HybridPrefillRequest,
    MemoryConfig,
    SimulationConfig,
    StoragePolicyConfig,
    StorageTierConfig,
)


def config(**overrides):
    h = HybridAttentionConfig(**{
        "pool_allocation": "fixed",
        "enabled": True, "replay_mode": "prefix_replay", "tokens_per_block": 128,
        "full_capacity_bytes": 8192, "linear_capacity_bytes": 64,
        "full_block_bytes": 8, "linear_checkpoint_bytes": 16, **overrides,
    })
    return SimulationConfig(
        memory=MemoryConfig(h.full_capacity_bytes + h.linear_capacity_bytes),
        slc=StorageTierConfig(8, 1), tlc=StorageTierConfig(8, 1), block_size_bytes=8,
        storage_policy=StoragePolicyConfig(kind="infinite_storage"), hybrid_attention=h,
    )


def prefill(sequence, path, **kwargs):
    return HybridPrefillRequest(sequence, sequence, sequence, path, **kwargs)


def publish(sequence, path, component):
    return HybridEvent(sequence, sequence, "publish", path, components=component)


def test_default_4096_chunks_restore_and_partial_tail(tmp_path):
    path = list(range(1, 79))
    with DWPDSimulator(config(), tmp_path / "chunks") as sim:
        cold = sim.process_hybrid_prefill(prefill(0, path, input_tokens=10000))
        assert cold["restore_tokens"] == cold["matched_prefix_tokens"] == 0
        assert cold["generated_checkpoint_tokens"] == [4096, 8192]
        assert cold["prefill_compute_tokens"] == 10000
        assert not sim.hybrid_inspect(path[-1], "linear")["published"]
        warm = sim.process_hybrid_prefill(prefill(1, path, input_tokens=10000))
        assert warm["restore_tokens"] == 8192
        assert warm["restore_hash_id"] == path[63]
        assert warm["prefill_compute_tokens"] == 1808
        assert warm["replayed_prefix_tokens"] == 1792
        assert warm["new_suffix_tokens"] == 16
        assert warm["generated_checkpoint_tokens"] == []
        assert sim.hybrid_inspect(path[31], "linear")["demands"] == 0
        assert sim.hybrid_inspect(path[63], "linear")["demands"] == 1
        stats = sim.hybrid_stats()
        assert stats["schema_version"] == 2
        assert "joint_available" not in stats
        assert stats["prefill"]["restored_tokens"] == 8192
        assert stats["prefill"]["generated_checkpoints"] == 2


def test_branch_cannot_use_deeper_or_other_branch_checkpoint(tmp_path):
    path = list(range(1, 79))
    branch = path[:54] + list(range(100, 124))
    with DWPDSimulator(config(), tmp_path / "fork") as sim:
        sim.process_hybrid_prefill(prefill(0, path, input_tokens=10000))
        result = sim.process_hybrid_prefill(prefill(1, branch, input_tokens=10000))
        assert result["matched_prefix_tokens"] == 6912
        assert result["restore_tokens"] == 4096
        assert result["prefill_compute_tokens"] == 5904
        assert result["replayed_prefix_tokens"] == 2816
        assert result["generated_checkpoint_tokens"] == [8192]
        assert sim.hybrid_inspect(path[63], "linear")["demands"] == 0
        assert sim.hybrid_inspect(branch[63], "linear")["published"]


def test_exact_length_repeat_keeps_last_token_and_explicit_limit(tmp_path):
    path = list(range(1, 65))
    with DWPDSimulator(config(), tmp_path / "limit") as sim:
        sim.process_hybrid_prefill(prefill(0, path))
        warm = sim.process_hybrid_prefill(prefill(1, path))
        assert warm["restore_tokens"] == 4096
        assert warm["prefill_compute_tokens"] == 4096
        assert warm["generated_checkpoint_tokens"] == [8192]
        limited = sim.process_hybrid_prefill(prefill(2, path, max_prefix_tokens=4095))
        assert limited["restore_tokens"] == 0
        assert limited["linear"] == "initial"
        assert limited["replayed_prefix_tokens"] == 8192
        assert sim.hybrid_inspect(path[63], "linear")["demands"] == 0


@pytest.mark.parametrize("interval", [256, 512, 4096])
def test_interval_controls_only_executed_complete_boundaries(tmp_path, interval):
    path = list(range(1, 16))
    with DWPDSimulator(config(checkpoint_interval_tokens=interval), tmp_path / "interval") as sim:
        cold = sim.process_hybrid_prefill(prefill(0, path, input_tokens=2000))
        expected = list(range(interval, 2001, interval))
        assert cold["generated_checkpoint_tokens"] == expected
        for index, hash_id in enumerate(path, 1):
            assert sim.hybrid_inspect(hash_id, "linear")["published"] == (index * 128 in expected)


def test_input_shorter_than_hash_block_generates_no_checkpoint(tmp_path):
    with DWPDSimulator(config(), tmp_path / "short") as sim:
        result = sim.process_hybrid_prefill(prefill(0, [], input_tokens=64))
        assert result["input_tokens"] == result["prefill_compute_tokens"] == 64
        assert result["restore_tokens"] == 0
        assert result["generated_checkpoint_tokens"] == result["full"] == []
        assert sim.hybrid_stats()["tree_nodes"] == 0


@pytest.mark.parametrize("dump,expected,source", [(False, 512, "dram"), (True, 768, "storage")])
def test_last_copy_loss_replays_but_dram_eviction_reads_storage(tmp_path, dump, expected, source):
    cfg = config(tokens_per_block=64, checkpoint_interval_tokens=256,
                 linear_capacity_bytes=32, dump_on_eviction=dump)
    path = list(range(1, 17))
    with DWPDSimulator(cfg, tmp_path / "loss") as sim:
        for i in range(16):
            sim.process_hybrid(publish(i, path[:i + 1], "full"))
        sim.process_hybrid(publish(16, path[:8], "linear"))
        sim.process_hybrid(publish(17, path[:12], "linear"))
        # Consume S512 without executing another full chunk, then pressure S768.
        assert sim.process_hybrid_prefill(prefill(18, path[:9]))["restore_tokens"] == 512
        sim.process_hybrid(publish(19, path, "linear"))
        assert not sim.hybrid_inspect(path[11], "linear")["in_dram"]
        assert sim.hybrid_inspect(path[11], "linear")["on_storage"] == dump
        branch = path[:14] + [100, 101]
        result = sim.process_hybrid_prefill(prefill(20, branch))
        assert result["restore_tokens"] == expected
        assert result["linear"] == source
        assert result["replayed_prefix_tokens"] == 896 - expected
        assert result["prefill_compute_tokens"] == 1024 - expected


def test_full_gap_and_observed_ghosts_never_create_a_restore(tmp_path):
    cfg = config(tokens_per_block=64, checkpoint_interval_tokens=256,
                 full_capacity_bytes=24, dump_on_eviction=False)
    path = list(range(1, 9))
    with DWPDSimulator(cfg, tmp_path / "gap") as sim:
        sim.process_hybrid(HybridEvent(0, 0, "observe", path))
        cold = sim.process_hybrid_prefill(prefill(1, path))
        assert cold["matched_prefix_tokens"] == cold["restore_tokens"] == 0
        assert sim.hybrid_inspect(path[3], "linear")["in_dram"]
        result = sim.process_hybrid_prefill(prefill(2, path))
        assert result["matched_prefix_tokens"] == 512
        assert result["full_available_prefix_tokens"] <= 192
        assert result["restore_tokens"] == 0
        assert result["linear"] == "initial"
        assert sim.hybrid_stats()["linear"]["demands"] == 0


def test_storage_prefix_is_consumed_before_promotion_evictions(tmp_path):
    cfg = config(tokens_per_block=128, checkpoint_interval_tokens=256,
                 full_capacity_bytes=8, linear_capacity_bytes=16)
    path = tmp_path / "storage"
    with DWPDSimulator(cfg, path) as sim:
        sim.process_hybrid_prefill(prefill(0, list(range(1, 9))))
        before_io = sim.hybrid_stats()["io_events"]
        result = sim.process_hybrid_prefill(prefill(1, list(range(1, 9))))
        assert result["restore_tokens"] == 768
        assert result["full"] == ["storage"] * 6
        assert result["linear"] == "storage"
        assert sim.hybrid_stats()["prefill"]["dram_restored_requests"] == 0
    with Path(str(path) + ".hybrid.csv").open() as source:
        rows = list(csv.DictReader(source))[before_io:]
    assert [row["operation"] for row in rows[:7]] == ["READ"] * 7
    assert [row["hash_id"] for row in rows[:7]] == ["1", "2", "3", "4", "5", "6", "6"]


def test_list_columnar_and_streaming_replay_are_equivalent(tmp_path):
    cfg = config(tokens_per_block=64, checkpoint_interval_tokens=256,
                 linear_capacity_bytes=16, full_capacity_bytes=40)
    requests = [prefill(0, list(range(1, 13))), prefill(1, list(range(1, 10)) + [20, 21, 22]),
                prefill(2, [], input_tokens=7), prefill(3, list(range(1, 13)), max_prefix_tokens=0)]
    outputs = []
    for mode in ("scalar", "list", "columnar", "stream"):
        path = tmp_path / mode
        with DWPDSimulator(cfg, path) as sim:
            if mode == "scalar":
                results = [sim.process_hybrid_prefill(q) for q in requests]
            elif mode == "list":
                results = sim.process_hybrid_prefill_requests(requests)
            elif mode == "stream":
                sim.run_hybrid_prefill(iter(requests), batch_requests=2)
                results = None
            else:
                columns = [[q.timestamp_ns for q in requests], [q.sequence for q in requests],
                           [q.request_id for q in requests], [q.input_tokens or 0 for q in requests],
                           [2**64 - 1 if q.max_prefix_tokens is None else q.max_prefix_tokens for q in requests]]
                offsets, hashes = [0], []
                for q in requests:
                    hashes.extend(q.hash_ids)
                    offsets.append(len(hashes))
                results = sim.process_hybrid_prefill_batch(
                    *[array("Q", c) for c in [*columns, offsets, hashes]],
                )
        outputs.append((results, sim.hybrid_stats(), Path(str(path) + ".hybrid.csv").read_text()))
    assert outputs[0] == outputs[1] == outputs[2]
    assert outputs[3][1:] == outputs[0][1:]


@pytest.mark.parametrize("dump", [False, True])
@pytest.mark.parametrize("seed", range(3))
def test_prefix_recovery_matches_independent_full_path_ledger(tmp_path, dump, seed):
    rng = random.Random(seed)
    block_tokens, interval, capacity = 64, 256, 2
    cfg = config(tokens_per_block=block_tokens, checkpoint_interval_tokens=interval,
                 linear_capacity_bytes=capacity * 16, full_capacity_bytes=100000,
                 dump_on_eviction=dump)
    full, generated, stored = set(), set(), set()
    dram = OrderedDict()
    sessions, next_hash = [[], [], []], 1

    def admit(key):
        while len(dram) >= capacity:
            victim, _ = dram.popitem(last=False)
            if dump:
                stored.add(victim)
        dram[key] = None

    with DWPDSimulator(cfg, tmp_path / "ledger") as sim:
        for seq in range(100):
            sid = rng.randrange(len(sessions))
            path = sessions[sid][:rng.randrange(len(sessions[sid]) + 1)]
            for _ in range(rng.randrange(1, 17)):
                path.append(next_hash)
                next_hash += 1
            sessions[sid] = path
            tokens = len(path) * block_tokens + rng.randrange(block_tokens)
            cap = rng.randrange(tokens + 1) if rng.random() < 0.2 else tokens - 1
            prefixes = [tuple(path[:end]) for end in range(1, len(path) + 1)]
            matched = 0
            for key in prefixes:
                if key not in full:
                    break
                matched = len(key) * block_tokens
            candidates = [key for key in generated if key in prefixes
                          and len(key) * block_tokens <= min(matched, cap, tokens - 1)
                          and (key in dram or key in stored)
                          and all(key[:i] in full for i in range(1, len(key) + 1))]
            chosen = max(candidates, key=len, default=())
            restored = len(chosen) * block_tokens
            source = "initial"
            if chosen:
                source = "dram" if chosen in dram else "storage"
                if chosen in dram:
                    dram.move_to_end(chosen)
                else:
                    admit(chosen)
            checkpoints = []
            for key in prefixes[len(chosen):]:
                full.add(key)
                if len(key) * block_tokens % interval == 0:
                    checkpoints.append(len(key) * block_tokens)
                    generated.add(key)
                    if key not in dram and key not in stored:
                        admit(key)
            result = sim.process_hybrid_prefill(prefill(seq, path, input_tokens=tokens, max_prefix_tokens=cap))
            assert result["restore_tokens"] == restored
            assert result["restore_hash_id"] == (chosen[-1] if chosen else None)
            assert result["linear"] == source
            assert result["full"] == ["dram"] * len(chosen)
            assert result["matched_prefix_tokens"] == matched
            assert result["generated_checkpoint_tokens"] == checkpoints
            assert result["prefill_compute_tokens"] == tokens - restored
            assert result["replayed_prefix_tokens"] == matched - restored
            assert result["new_suffix_tokens"] == tokens - matched
            for key in generated:
                actual = sim.hybrid_inspect(key[-1], "linear")
                assert actual["in_dram"] == (key in dram)
                assert actual["on_storage"] == (key in stored)


@pytest.mark.parametrize("invalid", [
    prefill(1, [9, 2]), prefill(1, [1, 1]),
    prefill(1, [1, 2], input_tokens=128), prefill(1, [1, 2], input_tokens=0),
    prefill(0, [1, 2]), replace(prefill(1, [1, 2]), request_id=0),
])
def test_invalid_prefill_requests_are_rejected(tmp_path, invalid):
    with DWPDSimulator(config(), tmp_path / "validation") as sim:
        sim.process_hybrid_prefill(prefill(0, [1, 2]))
        with pytest.raises((ValueError, TypeError)):
            sim.process_hybrid_prefill(invalid)


def test_prefix_mode_rejects_the_exact_object_request_api(tmp_path):
    with (DWPDSimulator(config(), tmp_path / "prefix-api") as sim,
          pytest.raises(ValueError, match="process_hybrid_prefill")):
        sim.process_hybrid(HybridEvent(1, 1, "request", [1, 2], request_id=1))


def test_exact_object_mode_rejects_the_prefill_request_api(tmp_path):
    exact = replace(config().hybrid_attention, replay_mode="exact_objects", tokens_per_block=0)
    with (DWPDSimulator(replace(config(), hybrid_attention=exact), tmp_path / "exact") as sim,
          pytest.raises(ValueError, match="prefix_replay")):
        sim.process_hybrid_prefill(prefill(0, [1, 2]))


@pytest.mark.parametrize("component,size", [("full", 9), ("linear", 17)])
def test_prefix_publish_rejects_conflicting_model_size(tmp_path, component, size):
    with (DWPDSimulator(config(), tmp_path / component) as sim,
          pytest.raises(ValueError, match="immutable size")):
        sim.process_hybrid(HybridEvent(
            0, 0, "publish", [1], components=component, size_bytes=size,
        ))


@pytest.mark.parametrize("overrides", [
    {"tokens_per_block": 0}, {"checkpoint_interval_tokens": 0},
    {"tokens_per_block": 100}, {"replay_mode": "unknown"},
])
def test_invalid_generation_config_is_rejected(tmp_path, overrides):
    with pytest.raises(ValueError):
        DWPDSimulator(config(**overrides), tmp_path / "config")


def test_env_config_runs_prefill_pipeline(monkeypatch, tmp_path):
    from dotenv import dotenv_values

    root = Path(__file__).resolve().parents[1]
    for key, value in dotenv_values(root / "example/.env.example").items():
        if value is not None:
            monkeypatch.setenv(key, value)
    requests = tmp_path / "prefill.jsonl"
    requests.write_text("\n".join(json.dumps({
        "timestamp_ns": i, "sequence": i, "request_id": i, "hash_ids": list(range(1, 66)),
    }) for i in range(2)))
    for key, value in {
        "HYBRID_ENABLED": "true", "HYBRID_REPLAY_MODE": "prefix_replay",
        "HYBRID_TOKENS_PER_BLOCK": "128", "HYBRID_CHECKPOINT_INTERVAL_TOKENS": "4096",
        "HYBRID_FULL_CAPACITY_BYTES": "0", "HYBRID_LINEAR_CAPACITY_BYTES": "0",
        "HYBRID_FULL_BLOCK_BYTES": "8", "HYBRID_LINEAR_CHECKPOINT_BYTES": "16",
        "BLOCK_SIZE_BYTES": "8", "MEMORY_CAPACITY_BYTES": "1088", "STORAGE_POLICY": "infinite_storage",
        "REQUESTS_PATH": str(requests), "OUTPUT_DIR": str(tmp_path / "output"),
    }.items():
        monkeypatch.setenv("DWPDSIM_" + key, value)
    namespace = runpy.run_path(str(root / "example/run_pipeline.py"))
    monkeypatch.setitem(namespace["main"].__globals__, "ENV_PATH", tmp_path / ".env")
    (tmp_path / ".env").write_text("")
    namespace["main"]()
    stats = json.loads((tmp_path / "output/simulation_metrics.json").read_text())["hybrid_attention"]
    assert stats["prefill"]["generated_checkpoints"] == 2
    assert stats["prefill"]["restored_tokens"] == 8192


@pytest.mark.parametrize("seed", range(3))
@pytest.mark.parametrize("dump", [False, True])
def test_indexed_full_segments_match_scan_results_and_io(tmp_path, seed, dump):
    rng = random.Random(seed)
    paths, requests, next_hash = [[], [], []], [], 1
    for seq in range(100):
        sid = rng.randrange(3)
        path = paths[sid][:rng.randrange(len(paths[sid]) + 1)]
        count = rng.randrange(1, 24)
        path += list(range(next_hash, next_hash + count))
        next_hash += count
        paths[sid] = path
        requests.append(prefill(seq, path))
    results = []
    for policy in ["segment_lru", "indexed_segment_lru"]:
        target = tmp_path / policy
        cfg = config(tokens_per_block=64, checkpoint_interval_tokens=256, full_capacity_bytes=80,
                     linear_capacity_bytes=32, full_policy=policy, dump_on_eviction=dump)
        with DWPDSimulator(cfg, target) as sim:
            rows = sim.process_hybrid_prefill_requests(requests)
        stats = sim.hybrid_stats()
        stats["full"].pop("candidates_examined")
        results.append((rows, stats, Path(str(target) + ".hybrid.csv").read_text()))
    assert results[0] == results[1]


@pytest.mark.parametrize("fraction", [0.0, 0.25, 0.5, 0.75, 1.0])
@pytest.mark.parametrize("phase", [0, 0.5])
def test_last_copy_drop_quota_and_independent_copy_counters(tmp_path, fraction, phase):
    cfg = config(linear_capacity_bytes=32, linear_drop_fraction=fraction, drop_phase=phase)
    with DWPDSimulator(cfg, tmp_path / "quota") as sim:
        for seq in range(9):
            sim.process_hybrid(publish(seq, [seq + 1], "linear"))
        stats = sim.hybrid_stats()["linear"]
        assert stats["eligible_evictions"] == stats["evictions"] == 7
        assert stats["drop_objects"] == int(7 * fraction + phase)
        assert stats["dump_objects"] + stats["drop_objects"] == 7
        assert stats["write_objects"] == stats["dump_objects"]
        assert sum(sim.hybrid_inspect(key, "linear")["on_storage"] for key in range(1, 10)) == 7 - int(7 * fraction + phase)


def test_suppressing_trace_preserves_logical_io_and_hits(tmp_path):
    outputs = []
    for emit in [True, False]:
        path = tmp_path / str(emit)
        cfg = config(tokens_per_block=128, checkpoint_interval_tokens=256,
                     full_capacity_bytes=8, linear_capacity_bytes=16, emit_io_trace=emit)
        with DWPDSimulator(cfg, path) as sim:
            rows = sim.process_hybrid_prefill_requests([prefill(0, list(range(1, 9))),
                                                       prefill(1, list(range(1, 9)))])
        outputs.append((rows, sim.hybrid_stats()))
        assert Path(str(path) + ".hybrid.csv").exists() == emit
    assert outputs[0] == outputs[1]
    assert outputs[0][1]["io_events"] > 0


@pytest.mark.parametrize("policy", ["prefix_frequency", "replay_value", "copy_aware", "target_frequency", "target_value", "frontier_lru", "frontier_guard"])
@pytest.mark.parametrize("fraction", [0.5, 1.0])
def test_prefix_value_policies_match_complete_path_reference(tmp_path, policy, fraction):
    # Independent full-path maps, literal opportunity counts and recomputed fallback chains.
    # All requests share a timestamp so exact integer weights make ties reproducible.
    rng = random.Random(7)
    capacity, interval, block = 4, 256, 64
    cfg = config(tokens_per_block=block, checkpoint_interval_tokens=interval, full_capacity_bytes=100000,
                 full_policy="indexed_segment_lru", linear_capacity_bytes=capacity * 16,
                 linear_policy=policy, alpha=0.5, linear_drop_fraction=fraction, recompute_weight=4)
    full, stored, metadata = set(), set(), {}
    resident = OrderedDict()
    eligible = dropped = 0

    def score(key):
        count = metadata[key]["target"] if policy.startswith("target_") else metadata[key]["count"]
        if policy in {"prefix_frequency", "target_frequency"}:
            return count
        prior = metadata[key]["parent"]
        while prior and prior not in resident and prior not in stored:
            prior = metadata[prior]["parent"]
        gap = (len(key) - len(prior)) * block / interval
        if policy == "replay_value":
            return count * gap / 16
        return count * (1 + (4 * gap if key not in stored else 0)) / 16

    def admit(key):
        nonlocal eligible, dropped
        while len(resident) >= capacity:
            cold = list(resident)[:2]
            if policy == "frontier_guard" and dropped >= int((eligible + 1) * fraction):
                victim = cold[0]
            elif policy in {"frontier_lru", "frontier_guard"}:
                descendants = [*resident, key]
                covered = [c for c in cold if any(len(d) > len(c) and d[:len(c)] == c for d in descendants)]
                victim = covered[0] if covered else cold[0]
            else:
                victim = min(cold, key=score)
            if victim not in stored:
                eligible += 1
                if dropped < int(eligible * fraction):
                    dropped += 1
                else:
                    stored.add(victim)
            resident.pop(victim)
        resident[key] = None

    sessions, next_hash = [[], [], []], 1
    with DWPDSimulator(cfg, tmp_path / policy) as sim:
        for seq in range(100):
            sid = rng.randrange(3)
            path = sessions[sid][:rng.randrange(len(sessions[sid]) + 1)]
            count = rng.randrange(1, 20)
            path += list(range(next_hash, next_hash + count))
            next_hash += count
            sessions[sid] = path
            prefixes = [tuple(path[:i]) for i in range(1, len(path) + 1)]
            matched = 0
            for key in prefixes:
                if key not in full:
                    break
                matched = len(key) * block
            for key in prefixes:
                if key in metadata and len(key) * block <= matched:
                    metadata[key]["count"] += 1
            targets = [key for key in metadata if key in prefixes and len(key) * block <= matched]
            if targets:
                metadata[max(targets, key=len)]["target"] += 1
            candidates = [key for key in metadata if key in prefixes
                          and (key in resident or key in stored)]
            chosen = max(candidates, key=len, default=())
            source = "initial"
            if chosen:
                source = "dram" if chosen in resident else "storage"
                if chosen in resident:
                    resident.move_to_end(chosen)
                else:
                    admit(chosen)
            previous, checkpoints = chosen, []
            for key in prefixes[len(chosen):]:
                full.add(key)
                if len(key) * block % interval == 0:
                    if key not in metadata:
                        frontier = len(key) * block == (len(path) * block + 1) // interval * interval
                        metadata[key] = {"count": 1, "target": int(frontier), "parent": previous}
                    metadata[key]["parent"] = previous
                    checkpoints.append(len(key) * block)
                    if key not in resident and key not in stored:
                        admit(key)
                if key in metadata:
                    previous = key
            result = sim.process_hybrid_prefill(HybridPrefillRequest(
                0, seq, seq, path, input_tokens=len(path) * block + 1,
            ))
            assert result["restore_tokens"] == len(chosen) * block
            assert result["linear"] == source
            assert result["generated_checkpoint_tokens"] == checkpoints
            for key in metadata:
                actual = sim.hybrid_inspect(key[-1], "linear")
                assert actual["in_dram"] == (key in resident)
                assert actual["on_storage"] == (key in stored)
        assert sim.hybrid_stats()["linear"]["drop_objects"] == dropped
        assert sim.hybrid_stats()["linear"]["eligible_evictions"] == eligible


def test_frontier_tradeoff_on_a_shorter_branch_does_not_fake_a_hit(tmp_path):
    base = list(range(1, 13))
    branch = base[:4] + list(range(100, 112))
    restored = {}
    for policy in ["checkpoint_lru", "frontier_lru"]:
        cfg = config(tokens_per_block=64, checkpoint_interval_tokens=256, linear_capacity_bytes=32,
                     full_policy="indexed_segment_lru", linear_policy=policy, alpha=1, linear_drop_fraction=1)
        with DWPDSimulator(cfg, tmp_path / policy) as sim:
            sim.process_hybrid_prefill(prefill(0, base, input_tokens=769))
            sim.process_hybrid_prefill(prefill(1, branch, input_tokens=1025))
            result = sim.process_hybrid_prefill(prefill(2, branch[:12], input_tokens=769))
            restored[policy] = result['restore_tokens']
            assert result['prefill_compute_tokens'] == 769 - result['restore_tokens']
    assert restored == {'checkpoint_lru': 768, 'frontier_lru': 0}


@pytest.mark.parametrize("fraction,reference", [(0, "checkpoint_lru"), (1, "frontier_lru")])
def test_frontier_guard_has_exact_boundary_behaviour(tmp_path, fraction, reference):
    outputs = []
    paths = [list(range(1, 17)), list(range(1, 17)) + [20, 21, 22, 23],
             list(range(1, 9)) + list(range(100, 112)), list(range(1, 17))]
    for policy in [reference, 'frontier_guard']:
        path = tmp_path / policy
        cfg = config(tokens_per_block=64, checkpoint_interval_tokens=256, full_capacity_bytes=80,
                     full_policy='indexed_segment_lru', linear_capacity_bytes=32,
                     linear_policy=policy, alpha=1, linear_drop_fraction=fraction)
        with DWPDSimulator(cfg, path) as sim:
            rows = sim.process_hybrid_prefill_requests([prefill(i, p) for i, p in enumerate(paths)])
        outputs.append((rows, sim.hybrid_stats(), Path(str(path) + '.hybrid.csv').read_text()))
    assert outputs[0] == outputs[1]
