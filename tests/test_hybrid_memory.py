"""Public synchronous replay tests, including an independent checkpoint ledger."""

import csv
import math
import random
from collections import OrderedDict
from dataclasses import replace

import pytest

from dwpdsim import (
    DWPDSimulator,
    HybridAttentionConfig,
    HybridEvent,
    MemoryConfig,
    SimulationConfig,
    StoragePolicyConfig,
    StorageTierConfig,
)


def config(**hybrid):
    h = HybridAttentionConfig(
        enabled=True,
        **{
            "pool_allocation": "fixed",
            "full_capacity_bytes": 256,
            "linear_capacity_bytes": 48,
            "full_block_bytes": 8,
            "linear_checkpoint_bytes": 16,
            **hybrid,
        },
    )
    return SimulationConfig(
        block_size_bytes=8,
        memory=MemoryConfig(h.full_capacity_bytes + h.linear_capacity_bytes),
        slc=StorageTierConfig(8, 1),
        tlc=StorageTierConfig(8, 1),
        storage_policy=StoragePolicyConfig(kind="infinite_storage"),
        hybrid_attention=h,
    )


def event(seq, action, path, **kwargs):
    return HybridEvent(seq, seq, action, path, request_id=seq, **kwargs)


def publish(seq, path, component="linear", **kwargs):
    return event(seq, "publish", path, components=component, **kwargs)


def request(seq, path, component="linear", **kwargs):
    return event(seq, "request", path, components=component, **kwargs)


def test_default_disabled_and_explicit_false_are_legacy_equivalent(tmp_path):
    base = config()
    base = replace(base, hybrid_attention=HybridAttentionConfig())
    outputs = []
    for i, cfg in enumerate([base, replace(base, hybrid_attention=HybridAttentionConfig(enabled=False))]):
        path = tmp_path / f"legacy{i}.csv"
        with DWPDSimulator(cfg, path) as sim:
            for n, hashes in enumerate([[1, 2], [1, 3], [1, 2]]):
                sim.process(n, n, 1, hashes)
        outputs.append((sim.stats(), path.read_text() if path.exists() else None))
    assert outputs[0] == outputs[1]
    assert "hybrid_attention" not in outputs[0][0]
    with DWPDSimulator(base, tmp_path / "disabled") as sim, pytest.raises(RuntimeError, match="enabled"):
        sim.process_hybrid(publish(0, [1]))


@pytest.mark.parametrize("override", [
    {"full_capacity_bytes": 0}, {"alpha": 0}, {"alpha": math.nan},
    {"decay_seconds": math.inf}, {"linear_policy": "unknown"},
    {"linear_read_cost": -1},
])
def test_invalid_enabled_config_rejected(tmp_path, override):
    with pytest.raises(ValueError):
        DWPDSimulator(config(**override), tmp_path / "invalid")


def test_incompatible_modes_are_explicit(tmp_path):
    cfg = config()
    with DWPDSimulator(replace(cfg, storage_policy=StoragePolicyConfig(kind="wear_share_round_robin")), tmp_path / "finite") as sim:
        assert "storage" in sim.hybrid_stats()
    with pytest.raises(ValueError, match="require"):
        DWPDSimulator(replace(cfg, hybrid_attention=HybridAttentionConfig(linear_capacity_bytes=16)),
                      tmp_path / "disabled")
    with DWPDSimulator(cfg, tmp_path / "on") as sim, pytest.raises(RuntimeError, match="hybrid events"):
        sim.process(0, 1, 0, [1])


def test_paths_do_not_supply_objects_and_components_are_independent(tmp_path):
    with DWPDSimulator(config(), tmp_path / "objects") as sim:
        sim.process_hybrid(event(0, "observe", [1, 2]))
        missing = sim.process_hybrid(request(1, [1, 2], "both"))
        assert missing["full"] == ["never_published"] * 2
        assert missing["linear"] == "never_published"
        sim.process_hybrid(publish(2, [1], "full"))
        sim.process_hybrid(publish(3, [1, 2], "full"))
        no_state = sim.process_hybrid(request(4, [1, 2], "both"))
        assert no_state["full"] == ["dram"] * 2
        assert not no_state["all_available"]
        sim.process_hybrid(publish(5, [1, 2]))
        ready = sim.process_hybrid(request(6, [1, 2], "both"))
        assert ready["all_dram"] and ready["all_available"]
        assert sim.hybrid_stats()["joint_available"] == 1


def test_single_component_probes_never_report_a_joint_hit(tmp_path):
    with DWPDSimulator(config(), tmp_path / "probe-intersection") as sim:
        sim.process_hybrid(publish(0, [1], "full"))
        sim.process_hybrid(publish(1, [1], "linear"))
        full = sim.process_hybrid(request(2, [1], "full"))
        assert full["full"] == ["dram"] and full["linear"] == "not_requested"
        assert not full["all_available"] and not full["all_dram"]
        linear = sim.process_hybrid(request(3, [1], "linear"))
        assert linear["full"] == [] and linear["linear"] == "dram"
        assert not linear["all_available"] and not linear["all_dram"]
        probes = sim.hybrid_stats()
        assert probes["joint_requests"] == probes["joint_available"] == probes["joint_dram"] == 0
        assert probes["all_available_requests"] == probes["all_dram_requests"] == 0
        assert probes["full"]["dram_hits"] == probes["linear"]["dram_hits"] == 1

        both = sim.process_hybrid(request(4, [1], "both"))
        assert both["full"] == ["dram"] and both["linear"] == "dram"
        assert both["all_available"] and both["all_dram"]
        stats = sim.hybrid_stats()
        assert stats["requests"] == 3
        assert stats["joint_requests"] == stats["joint_available"] == stats["joint_dram"] == 1
        assert stats["all_available_requests"] == stats["all_dram_requests"] == 1
        assert stats["full"]["demands"] == stats["linear"]["demands"] == 2
        assert stats["full"]["dram_hits"] == stats["linear"]["dram_hits"] == 2


def test_full_hole_prevents_joint_hit_and_wrong_endpoint_never_matches(tmp_path):
    with DWPDSimulator(config(), tmp_path / "holes") as sim:
        sim.process_hybrid(publish(0, [1], "full"))
        sim.process_hybrid(publish(1, [1, 2, 3], "full"))
        sim.process_hybrid(publish(2, [1, 2, 3]))
        r = sim.process_hybrid(request(3, [1, 2, 3], "both"))
        assert r["full"] == ["dram", "never_published", "dram"]
        assert r["linear"] == "dram" and not r["all_available"]
        r = sim.process_hybrid(request(4, [1, 2, 3], restore_blocks=2))
        assert r["linear"] == "never_published"
        r = sim.process_hybrid(request(5, [1, 9, 10]))
        assert r["linear"] == "never_published"


def test_internal_checkpoint_eviction_does_not_refresh_ancestors(tmp_path):
    with DWPDSimulator(config(), tmp_path / "internal") as sim:
        for i, path in enumerate([[1], [1, 2], [1, 2, 3]]):
            sim.process_hybrid(publish(i, path))
        sim.process_hybrid(request(3, [1]))
        sim.process_hybrid(request(4, [1, 2, 3]))
        sim.process_hybrid(publish(5, [4]))
        assert sim.hybrid_inspect(1, "linear")["in_dram"]
        assert not sim.hybrid_inspect(2, "linear")["in_dram"]
        assert sim.hybrid_inspect(3, "linear")["in_dram"]
        assert sim.hybrid_inspect(2, "linear")["demands"] == 0
        r = sim.process_hybrid(request(6, [1, 2]))
        assert r["linear"] == "storage"
        assert sim.hybrid_stats()["linear"]["read_bytes"] == 16


def test_duplicate_publish_does_not_touch_heat_or_allocate_again(tmp_path):
    with DWPDSimulator(config(linear_capacity_bytes=16), tmp_path / "duplicate") as sim:
        sim.process_hybrid(publish(0, [1]))
        sim.process_hybrid(publish(1, [2]))
        before = sim.hybrid_stats()["linear"].copy()
        sim.process_hybrid(publish(2, [1]))
        assert not sim.hybrid_inspect(1, "linear")["in_dram"]
        assert sim.hybrid_inspect(1, "linear")["demands"] == 0
        after = sim.hybrid_stats()["linear"]
        for key in ["write_objects", "storage_allocated_bytes", "resident_bytes", "evictions"]:
            assert after[key] == before[key]
        with pytest.raises(ValueError, match="immutable"):
            sim.process_hybrid(publish(3, [1], size_bytes=24))


def test_oversize_objects_have_disjoint_complete_storage_and_exact_lengths(tmp_path):
    path = tmp_path / "sizes"
    with DWPDSimulator(config(full_capacity_bytes=4, linear_capacity_bytes=4), path) as sim:
        sim.process_hybrid(publish(0, [1], "full", size_bytes=9))
        sim.process_hybrid(publish(1, [1], size_bytes=17))
        a = sim.hybrid_inspect(1, "full")
        b = sim.hybrid_inspect(1, "linear")
        assert not a["in_dram"] and not b["in_dram"]
        assert len(a["storage_addresses"]) == 2
        assert len(b["storage_addresses"]) == 3
        assert set(a["storage_addresses"]).isdisjoint(b["storage_addresses"])
        r = sim.process_hybrid(request(2, [1], "both"))
        assert r["all_available"] and not r["all_dram"]
        assert sim.hybrid_stats()["linear"]["read_bytes"] == 17
        assert sim.hybrid_stats()["linear"]["read_allocated_bytes"] == 24
        assert sim.trace_event_count == 4
    with hybrid_trace_path(path).open() as source:
        rows = list(csv.DictReader(source))
    assert [r["operation"] for r in rows] == ["WRITE", "WRITE", "READ", "READ"]
    assert [int(r["payload_bytes"]) for r in rows] == [9, 17, 9, 17]


def hybrid_trace_path(path):
    return path.with_name(path.name + ".hybrid.csv")


def test_drop_preserves_history_but_not_availability(tmp_path):
    with DWPDSimulator(config(linear_capacity_bytes=16, dump_on_eviction=False),
                       tmp_path / "drop") as sim:
        sim.process_hybrid(publish(0, [1]))
        sim.process_hybrid(publish(1, [2]))
        r = sim.process_hybrid(request(2, [1]))
        assert r["linear"] == "lost" and not r["all_available"]
        assert sim.hybrid_stats()["linear"]["write_objects"] == 0
        sim.process_hybrid(publish(3, [1]))
        assert sim.process_hybrid(request(4, [1]))["linear"] == "dram"


@pytest.mark.parametrize("invalid", [
    publish(1, [9, 2]), publish(1, [1, 1]),
    event(1, "write_pending", [1]), request(1, [1], restore_blocks=2),
])
def test_input_identity_and_unsupported_states_are_rejected(tmp_path, invalid):
    with DWPDSimulator(config(), tmp_path / "identity") as sim:
        sim.process_hybrid(publish(0, [1, 2]))
        with pytest.raises(ValueError):
            sim.process_hybrid(invalid)


@pytest.mark.parametrize("invalid,message", [
    (request(1, [1]), "strictly"),
    (replace(request(2, [1]), request_id=1), "unique"),
    (replace(request(2, [1]), timestamp_ns=0), "nondecreasing"),
])
def test_input_order_and_request_identity_are_rejected(tmp_path, invalid, message):
    with DWPDSimulator(config(), tmp_path / "order") as sim:
        sim.process_hybrid(publish(0, [1, 2]))
        sim.process_hybrid(request(1, [1, 2]))
        with pytest.raises(ValueError, match=message):
            sim.process_hybrid(invalid)


def test_finished_simulator_rejects_further_input(tmp_path):
    with DWPDSimulator(config(), tmp_path / "finished") as sim:
        sim.process_hybrid(publish(0, [1]))
    with pytest.raises(RuntimeError, match="finish"):
        sim.process_hybrid(request(3, [1]))


@pytest.mark.parametrize("policy", ["checkpoint_lru", "frequency", "benefit"])
def test_batch_and_serial_replay_are_identical(tmp_path, policy):
    events = [publish(0, [1], "full"), publish(1, [1]), request(2, [1], "both"),
              event(3, "observe", [1, 2]), publish(4, [1, 2]), request(5, [1]),
              publish(6, [3]), request(7, [1, 2], "both")]
    outputs = []
    for batch in [False, True]:
        path = tmp_path / str(batch)
        with DWPDSimulator(config(linear_policy=policy, linear_capacity_bytes=16), path) as sim:
            if batch:
                results = sim.process_hybrid_events(events[:3]) + sim.process_hybrid_events(events[3:])
            else:
                results = [r for e in events if (r := sim.process_hybrid(e)) is not None]
        outputs.append((results, sim.stats(), hybrid_trace_path(path).read_text()))
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("drop", [False, True])
@pytest.mark.parametrize("seed", range(5))
def test_random_checkpoint_lru_against_independent_ledger(tmp_path, drop, seed):
    # No simulator lookup/score helper is used to predict hits. Explicit publish,
    # LRU eviction, completed dump and demand are tracked in separate Python sets.
    rng = random.Random(seed)
    dram = OrderedDict()
    stored, published = set(), set()
    counts = {"dram": 0, "storage": 0, "never_published": 0, "lost": 0}

    def insert(key):
        if len(dram) == 3:
            victim, _ = dram.popitem(last=False)
            if not drop:
                stored.add(victim)
        dram[key] = None

    with DWPDSimulator(config(dump_on_eviction=not drop), tmp_path / "oracle") as sim:
        for i in range(600):
            key = rng.randrange(1, 12)
            if rng.random() < 0.4:
                sim.process_hybrid(publish(i, [key]))
                published.add(key)
                if key not in dram and key not in stored:
                    insert(key)
            else:
                expected = ("dram" if key in dram else "storage" if key in stored
                            else "lost" if key in published else "never_published")
                actual = sim.process_hybrid(request(i, [key]))
                assert actual["linear"] == expected, (seed, i, key)
                assert not actual["all_available"] and not actual["all_dram"]
                counts[expected] += 1
                if key in dram:
                    dram.move_to_end(key)
                elif key in stored:
                    insert(key)
            for candidate in range(1, 12):
                state = sim.hybrid_inspect(candidate, "linear")
                assert state["in_dram"] == (candidate in dram)
                assert state["on_storage"] == (candidate in stored)
            assert sim.hybrid_stats()["linear"]["resident_bytes"] == 16 * len(dram)
        stats = sim.hybrid_stats()["linear"]
        assert [stats[k] for k in ["dram_hits", "storage_hits", "never_published_misses", "lost_misses"]] == [
            counts[k] for k in ["dram", "storage", "never_published", "lost"]
        ]


def test_uniform_cost_benefit_degenerates_to_frequency(tmp_path):
    rng = random.Random(932)
    events = [publish(i, [i + 1]) for i in range(8)]
    events += [request(i, [rng.randrange(1, 9)]) if i % 3 else publish(i, [100 + i])
               for i in range(8, 200)]
    outcomes = []
    for policy in ["frequency", "benefit"]:
        path = tmp_path / policy
        with DWPDSimulator(config(linear_policy=policy, alpha=1), path) as sim:
            outcomes.append(sim.process_hybrid_events(events))
        outcomes.append(hybrid_trace_path(path).read_text())
    assert outcomes[0] == outcomes[2]
    assert outcomes[1] == outcomes[3]


def test_columnar_events_match_serial_and_reject_invalid_buffers(tmp_path):
    import numpy as np

    events = [publish(0, [1], "full"), publish(1, [1]), request(2, [1], "both"),
              publish(3, [1, 2]), request(4, [1, 2], "both")]
    columns = [np.array(a, dtype=np.uint64) for a in [
        [e.timestamp_ns for e in events], [e.sequence for e in events], [1, 1, 2, 1, 2],
        [0, 1, 2, 1, 2], [e.request_id for e in events], [0] * 5, [0] * 5,
        [0, 1, 2, 3, 5, 7], [1, 1, 1, 1, 2, 1, 2],
    ]]
    output = []
    for batch in [False, True]:
        with DWPDSimulator(config(), tmp_path / str(batch)) as sim:
            result = (sim.process_hybrid_batch(*columns) if batch
                      else [r for e in events if (r := sim.process_hybrid(e)) is not None])
            output.append((result, sim.stats()))
    assert output[0] == output[1]
    with DWPDSimulator(config(), tmp_path / "invalid-buffers") as sim:
        bad = columns.copy()
        bad[2] = np.array([0, 9, 2, 1, 2], dtype=np.uint64)
        with pytest.raises(ValueError, match="event code"):
            sim.process_hybrid_batch(*bad)
        assert sim.node_count == 0
        bad = columns.copy()
        bad[-1] = bad[-1].astype(np.int64)
        with pytest.raises(ValueError, match="uint64"):
            sim.process_hybrid_batch(*bad)


def test_full_segment_eviction_uses_full_residency_only(tmp_path):
    with DWPDSimulator(config(full_capacity_bytes=16), tmp_path / "full") as sim:
        sim.process_hybrid(publish(0, [1], "full"))
        sim.process_hybrid(publish(1, [1, 2], "full"))
        sim.process_hybrid(publish(2, [1, 2, 3]))
        sim.process_hybrid(publish(3, [1, 2, 3], "full"))
        assert sim.hybrid_inspect(3, "linear")["in_dram"]
        assert sim.hybrid_inspect(3, "full")["in_dram"]
        assert not sim.hybrid_inspect(1, "full")["in_dram"]
        assert sim.hybrid_inspect(1, "full")["on_storage"]
        r = sim.process_hybrid(request(4, [1, 2, 3], "both"))
        assert r["full"] == ["storage", "storage", "dram"]
        assert r["linear"] == "dram" and r["all_available"]
        assert sim.hybrid_stats()["full"]["resident_bytes"] <= 16


def test_benefit_per_byte_can_choose_different_size_checkpoint(tmp_path):
    with DWPDSimulator(config(linear_capacity_bytes=40, linear_policy="benefit", alpha=1),
                       tmp_path / "weighted") as sim:
        events = [publish(0, [1], size_bytes=8), publish(1, [2], size_bytes=24),
                  request(2, [1]), request(3, [2]), publish(4, [3], size_bytes=16)]
        # Same demand time makes frequency identical. Different sizes determine priority.
        events = [replace(e, timestamp_ns=0) for e in events]
        sim.process_hybrid_events(events)
        assert sim.hybrid_inspect(1, "linear")["in_dram"]
        assert not sim.hybrid_inspect(2, "linear")["in_dram"]
        assert sim.hybrid_inspect(3, "linear")["in_dram"]
        assert sim.hybrid_stats()["linear"]["resident_bytes"] == 24


def test_storage_hit_bypass_does_not_create_dram_copy(tmp_path):
    with DWPDSimulator(config(linear_capacity_bytes=16, admit_storage_hits=False),
                       tmp_path / "no-promote") as sim:
        sim.process_hybrid(publish(0, [1]))
        sim.process_hybrid(publish(1, [2]))
        for seq in [2, 3]:
            assert sim.process_hybrid(request(seq, [1]))["linear"] == "storage"
        assert not sim.hybrid_inspect(1, "linear")["in_dram"]
        assert sim.hybrid_inspect(2, "linear")["in_dram"]
        assert sim.hybrid_stats()["linear"]["read_objects"] == 2


def test_example_environment_forwards_explicit_feature_flag(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "example/run_pipeline.py"
    spec = importlib.util.spec_from_file_location("hybrid_example_config", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from dotenv import dotenv_values

    for key, value in dotenv_values(path.parent / ".env.example").items():
        if value is not None:
            monkeypatch.setenv(key, value)
    assert not module.simulation_config().hybrid_attention.enabled
    for key, value in {
        "DWPDSIM_HYBRID_ENABLED": "true", "DWPDSIM_HYBRID_FULL_CAPACITY_BYTES": "128",
        "DWPDSIM_HYBRID_LINEAR_CAPACITY_BYTES": "48", "DWPDSIM_HYBRID_FULL_BLOCK_BYTES": "8",
        "DWPDSIM_HYBRID_LINEAR_CHECKPOINT_BYTES": "16", "DWPDSIM_HYBRID_LINEAR_POLICY": "benefit",
        "DWPDSIM_STORAGE_POLICY": "infinite_storage", "DWPDSIM_MEMORY_POLICY": "baseline_lru",
    }.items():
        monkeypatch.setenv(key, value)
    cfg = module.simulation_config()
    assert cfg.hybrid_attention.enabled
    assert cfg.hybrid_attention.linear_policy == "benefit"
    assert cfg.hybrid_attention.linear_capacity_bytes == 48


@pytest.mark.parametrize("policy", ["frequency", "benefit"])
@pytest.mark.parametrize("seed", range(5))
def test_scored_eviction_matches_independent_full_scan_reference(tmp_path, policy, seed):
    # Independent object dictionary + stable sort; no C++ policy/index helper.
    rng = random.Random(seed)
    data, dram, stored = {}, OrderedDict(), set()
    capacity, alpha, decay = 64, 0.5, 60

    def admit(key, now):
        size = data[key]["size"]
        while sum(data[k]["size"] for k in dram) + size > capacity:
            candidates, covered = [], 0
            for k in dram:
                candidates.append(k)
                covered += data[k]["size"]
                if covered >= alpha * capacity:
                    break

            def score(k):
                entry = data[k]
                freq = entry["frequency"] * math.exp(-(now - entry["time"]) / decay)
                return freq if policy == "frequency" else freq / entry["size"]

            victim = min(candidates, key=score)
            del dram[victim]
            stored.add(victim)
        dram[key] = None

    with DWPDSimulator(config(linear_capacity_bytes=capacity, linear_policy=policy, alpha=alpha),
                       tmp_path / "scored-oracle") as sim:
        for seq in range(400):
            now, key = seq * 0.5, rng.randrange(1, 13)
            if key not in data:
                data[key] = {"size": 8 * (1 + key % 3), "frequency": 0, "time": 0,
                             "published": False}
            c = data[key]
            if rng.random() < 0.35:
                sim.process_hybrid(replace(publish(seq, [key], size_bytes=c["size"]),
                                           timestamp_ns=int(now * 1e9)))
                c["published"] = True
                if key not in dram and key not in stored:
                    admit(key, now)
            else:
                expected = ("dram" if key in dram else "storage" if key in stored
                            else "lost" if c["published"] else "never_published")
                actual = sim.process_hybrid(replace(request(seq, [key]), timestamp_ns=int(now * 1e9)))
                assert actual["linear"] == expected
                c["frequency"] = c["frequency"] * math.exp(-(now - c["time"]) / decay) + 1
                c["time"] = now
                if key in dram:
                    dram.move_to_end(key)
                elif key in stored:
                    admit(key, now)
            for k in data:
                actual = sim.hybrid_inspect(k, "linear")
                assert actual["in_dram"] == (k in dram), (seed, seq, k)
                assert actual["on_storage"] == (k in stored)


@pytest.mark.parametrize("dump", [False, True])
@pytest.mark.parametrize("admit", [False, True])
@pytest.mark.parametrize("seed", range(5))
def test_random_joint_hits_against_two_independent_component_ledgers(tmp_path, dump, admit, seed):
    # Independent immutable object registries and two OrderedDict caches. Flat
    # prefix trees make FULL segment LRU a single-object LRU, so both pools can
    # be checked without relying on the C++ lookup or topology implementation.
    rng = random.Random(seed)
    dram = [OrderedDict(), OrderedDict()]
    stored = [set(), set()]
    published = [set(), set()]
    joint_count = joint_available = joint_dram = 0

    def insert(kind, key):
        if len(dram[kind]) == 2:
            victim, _ = dram[kind].popitem(last=False)
            if dump:
                stored[kind].add(victim)
        dram[kind][key] = None

    cfg = config(full_capacity_bytes=16, linear_capacity_bytes=32,
                 dump_on_eviction=dump, admit_storage_hits=admit)
    with DWPDSimulator(cfg, tmp_path / "joint-oracle") as sim:
        for seq in range(400):
            key = rng.choice([0, 1, 2, 3, 4, 2**64 - 1])
            if rng.random() < 0.4:
                kind = rng.randrange(2)
                sim.process_hybrid(publish(seq, [key], ["full", "linear"][kind]))
                published[kind].add(key)
                if key not in dram[kind] and key not in stored[kind]:
                    insert(kind, key)
            else:
                kinds = rng.choice([(0,), (1,), (0, 1)])
                name = "both" if len(kinds) == 2 else ["full", "linear"][kinds[0]]
                # Snapshot both authoritative ledgers before simulating promotion.
                expected = {
                    kind: ("dram" if key in dram[kind] else "storage" if key in stored[kind]
                           else "lost" if key in published[kind] else "never_published")
                    for kind in kinds
                }
                actual = sim.process_hybrid(request(seq, [key], name))
                if 0 in kinds:
                    assert actual["full"] == [expected[0]]
                else:
                    assert actual["full"] == []
                assert actual["linear"] == expected.get(1, "not_requested")
                available = name == "both" and all(
                    value in {"dram", "storage"} for value in expected.values()
                )
                all_dram = name == "both" and all(value == "dram" for value in expected.values())
                assert actual["all_available"] == available, (seq, expected)
                assert actual["all_dram"] == all_dram
                if len(kinds) == 2:
                    joint_count += 1
                    joint_available += available
                    joint_dram += all_dram
                for kind in kinds:
                    if key in dram[kind]:
                        dram[kind].move_to_end(key)
                    elif key in stored[kind] and admit:
                        insert(kind, key)
            for kind in range(2):
                stats = sim.hybrid_stats()[["full", "linear"][kind]]
                assert stats["resident_bytes"] == len(dram[kind]) * [8, 16][kind]
                for candidate in published[kind]:
                    state = sim.hybrid_inspect(candidate, ["full", "linear"][kind])
                    assert state["in_dram"] == (candidate in dram[kind])
                    assert state["on_storage"] == (candidate in stored[kind])
        stats = sim.hybrid_stats()
        assert (stats["joint_requests"], stats["joint_available"], stats["joint_dram"]) == (
            joint_count, joint_available, joint_dram)


def test_joint_availability_cannot_accumulate_from_past_component_hits(tmp_path):
    with DWPDSimulator(config(full_capacity_bytes=8, linear_capacity_bytes=16,
                             dump_on_eviction=False), tmp_path / "no-history-union") as sim:
        sim.process_hybrid(publish(0, [1], "full"))
        first = sim.process_hybrid(request(1, [1], "both"))
        assert first["full"] == ["dram"] and not first["all_available"]
        sim.process_hybrid(publish(2, [2], "full"))  # Last FULL(1) copy is gone.
        sim.process_hybrid(publish(3, [1], "linear"))
        second = sim.process_hybrid(request(4, [1], "both"))
        assert second["full"] == ["lost"] and second["linear"] == "dram"
        assert not second["all_available"]
        assert sim.hybrid_stats()["joint_available"] == 0
