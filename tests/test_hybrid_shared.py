"""Shared byte capacity, global typed-object ranking and joint recovery."""

import random
from array import array
from dataclasses import asdict, replace

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
from dwpdsim.hybrid_mqsim import convert_hybrid_trace


def configuration(total=64, **overrides):
    return SimulationConfig(
        memory=MemoryConfig(total), slc=StorageTierConfig(128, 1),
        tlc=StorageTierConfig(256, 1), block_size_bytes=8,
        storage_policy=StoragePolicyConfig(kind="infinite_storage"),
        hybrid_attention=HybridAttentionConfig(**{
            "enabled": True, "full_block_bytes": 8, "linear_checkpoint_bytes": 24,
            "full_policy": "lru_k", "linear_policy": "lru_k", "emit_io_trace": False,
            **overrides,
        }),
    )


@pytest.mark.parametrize("component,size,count", [("full", 8, 16), ("linear", 24, 5)])
def test_each_component_can_use_the_entire_shared_budget(tmp_path, component, size, count):
    cfg = configuration(total=128)
    assert cfg.hybrid_attention.pool_allocation == "shared"
    assert cfg.hybrid_attention.full_capacity_bytes == cfg.hybrid_attention.linear_capacity_bytes == 0
    with DWPDSimulator(cfg, tmp_path / component) as sim:
        for i in range(count):
            sim.process_hybrid(HybridEvent(i, i, "publish", [i+1], components=component))
        stats = sim.hybrid_stats()
        assert stats[component]["resident_bytes"] == count * size
        assert stats[component]["evictions"] == 0
        assert stats["pool"]["resident_bytes"] == count * size
        assert "full_target_bytes" not in stats["pool"]
        sim.process_hybrid(HybridEvent(count, count, "publish", [count+1], components=component))
        assert sim.hybrid_stats()[component]["evictions"] == 1
        assert sim.hybrid_stats()["pool"]["peak_resident_bytes"] <= 128


class ReferenceCache:
    """Backward K-distance over one set of variable-sized typed objects."""

    def __init__(self, k, dump):
        self.k, self.dump = k, dump
        self.dram, self.ssd, self.references, self.admitted = set(), set(), {}, {}
        self.admissions = 0

    @staticmethod
    def size(key):
        return 8 if key[0] == "full" else 24

    def admit(self, key, clock):
        if key in self.dram:
            return
        while sum(self.size(item) for item in self.dram) + self.size(key) > 64:
            candidates = [item for item in self.dram if len(self.references.get(item, [])) < self.k]
            if candidates:
                unreferenced = [item for item in candidates if not self.references.get(item)]
                if unreferenced:
                    candidates = unreferenced
                else:
                    oldest = min(self.references[item][-1] for item in candidates)
                    candidates = [item for item in candidates if self.references[item][-1] == oldest]
            else:
                distances = {item: clock-self.references[item][-self.k] for item in self.dram}
                farthest = max(distances.values())
                candidates = [item for item, distance in distances.items() if distance == farthest]
                oldest = min(self.references[item][-1] for item in candidates)
                candidates = [item for item in candidates if self.references[item][-1] == oldest]
            victim = min(candidates, key=self.admitted.__getitem__)
            self.dram.remove(victim)
            del self.admitted[victim]
            if self.dump:
                self.ssd.add(victim)
        self.admissions += 1
        self.admitted[key] = self.admissions
        self.dram.add(key)

    def publish(self, key, clock):
        if key not in self.dram and key not in self.ssd:
            self.admit(key, clock)

    def request(self, keys, clock):
        # Consume and reference all sources before reload can evict another source.
        reads = [key for key in keys if key not in self.dram and key in self.ssd]
        for key in keys:
            self.references.setdefault(key, []).append(clock)
        for key in reads:
            self.admit(key, clock)


@pytest.mark.parametrize("k", [1, 2, 3, 5])
@pytest.mark.parametrize("dump", [False, True])
def test_global_lru_k_matches_independent_variable_byte_reference(tmp_path, k, dump):
    cfg = configuration(lru_k=k, dump_on_eviction=dump)
    reference = ReferenceCache(k, dump)
    rng = random.Random(73)
    events = [("publish", "full", 1), ("publish", "linear", 1)]
    events += [("request", "both", 1)] * 6
    events += [("publish", component, i) for i in range(2, 9) for component in ("full", "linear")]
    for _ in range(120):
        action = "publish" if rng.random() < .45 else "request"
        component = rng.choice(("full", "linear") if action == "publish" else ("full", "linear", "both"))
        events.append((action, component, rng.randrange(1, 13)))
    clock = 0
    with DWPDSimulator(cfg, tmp_path / "reference") as sim:
        for seq, (action, component, node) in enumerate(events):
            keys = [(c, node) for c in (("full", "linear") if component == "both" else (component,))]
            if action == "request":
                clock += 1
                reference.request(keys, clock)
            else:
                reference.publish(keys[0], clock)
            sim.process_hybrid(HybridEvent(seq, seq, action, [node], request_id=seq, components=component))
            for c in ("full", "linear"):
                for identity in range(1, 13):
                    state = sim.hybrid_inspect(identity, c)
                    assert state["in_dram"] == ((c, identity) in reference.dram), (seq, c, identity)
                    assert state["on_storage"] == ((c, identity) in reference.ssd), (seq, c, identity)
            stats = sim.hybrid_stats()
            resident = sum(reference.size(key) for key in reference.dram)
            assert stats["pool"]["resident_bytes"] == resident
            assert stats["full"]["resident_bytes"] + stats["linear"]["resident_bytes"] == resident
            assert resident <= stats["pool"]["peak_resident_bytes"] <= 64
        assert stats["pool"]["cross_component_reclaims"] > 0


@pytest.mark.parametrize("full_policy", ["segment_lru", "indexed_segment_lru"])
def test_global_lru_can_reclaim_the_other_component(tmp_path, full_policy):
    cfg = configuration(full_policy=full_policy, linear_policy="checkpoint_lru")
    rows = [("publish", "linear", 1), ("publish", "full", 2),
            ("publish", "linear", 3), ("publish", "full", 4),
            ("request", "linear", 1), ("request", "full", 2),
            ("publish", "full", 5)]
    with DWPDSimulator(cfg, tmp_path / full_policy) as sim:
        for i, (action, component, node) in enumerate(rows):
            sim.process_hybrid(HybridEvent(i, i, action, [node], request_id=i, components=component))
        assert sim.hybrid_inspect(3, "linear")["on_storage"]
        assert not sim.hybrid_inspect(3, "linear")["in_dram"]
        assert sim.hybrid_inspect(4, "full")["in_dram"]
        assert sim.hybrid_stats()["pool"]["cross_component_reclaims"] == 1


@pytest.mark.parametrize("first,missing", [("full", "full"), ("linear", "linear")])
def test_shared_last_copy_eviction_cannot_create_joint_hit(tmp_path, first, missing):
    cfg = configuration(total=32, dump_on_eviction=False)
    second = "linear" if first == "full" else "full"
    with DWPDSimulator(cfg, tmp_path / first) as sim:
        for i, component in enumerate((first, second)):
            sim.process_hybrid(HybridEvent(i, i, "publish", [1], components=component))
        sim.process_hybrid(HybridEvent(2, 2, "publish", [2], components="full"))
        result = sim.process_hybrid(HybridEvent(3, 3, "request", [1], components="both"))
        assert not result["all_available"]
        assert not sim.hybrid_inspect(1, missing)["in_dram"]
        assert not sim.hybrid_inspect(1, missing)["on_storage"]
        assert sim.hybrid_stats()["joint_available"] == 0


def test_oversized_checkpoint_bypasses_shared_dram_without_evicting_full(tmp_path):
    cfg = configuration(total=16)
    with DWPDSimulator(cfg, tmp_path / "large") as sim:
        sim.process_hybrid(HybridEvent(0, 0, "publish", [1], components="full"))
        sim.process_hybrid(HybridEvent(1, 1, "publish", [1], components="linear"))
        result = sim.process_hybrid(HybridEvent(2, 2, "request", [1], components="both"))
        assert result["all_available"] and not result["all_dram"]
        assert sim.hybrid_inspect(1, "full")["in_dram"]
        assert sim.hybrid_inspect(1, "linear")["on_storage"]
        assert sim.hybrid_stats()["linear"]["resident_bytes"] == 0


@pytest.mark.parametrize("policy", ["infinite_storage", "wear_share_round_robin", "adaptive_endurance"])
def test_shared_prefix_scalar_buffer_and_trace_equivalence(tmp_path, policy):
    cfg = configuration(replay_mode="prefix_replay", tokens_per_block=64,
                        checkpoint_interval_tokens=64, checkpoint_generation="request_end")
    cfg = replace(cfg, memory=MemoryConfig(4096), slc=StorageTierConfig(8192, 1),
                  tlc=StorageTierConfig(16384, 1), block_size_bytes=512,
                  storage_policy=StoragePolicyConfig(kind=policy),
                  hybrid_attention=replace(cfg.hybrid_attention,
                                           full_block_bytes=512, linear_checkpoint_bytes=1536))
    paths = [[(i % 3)*100+j for j in range(1, 4+i//9)] for i in range(45)]
    requests = [HybridPrefillRequest(i*10**9, i, i, p, input_tokens=len(p)*64+1)
                for i, p in enumerate(paths)]
    outputs = []
    for batch, trace in ((False, False), (True, True)):
        path = tmp_path / str(batch)
        run_cfg = replace(cfg, hybrid_attention=replace(cfg.hybrid_attention, emit_io_trace=trace))
        with DWPDSimulator(run_cfg, path) as sim:
            if batch:
                offsets = [0]
                for p in paths:
                    offsets.append(offsets[-1]+len(p))
                results = sim.process_hybrid_prefill_batch(
                    array('Q', [q.timestamp_ns for q in requests]), array('Q', range(45)),
                    array('Q', range(45)), array('Q', [q.input_tokens for q in requests]),
                    array('Q', [2**64-1]*45), array('Q', offsets),
                    array('Q', [node for p in paths for node in p]),
                )
            else:
                results = [sim.process_hybrid_prefill(q) for q in requests]
            stats = sim.hybrid_stats()
        outputs.append((results, stats))
        if trace and policy != "infinite_storage":
            manifest = convert_hybrid_trace(path.with_name(path.name+".hybrid.csv"),
                                            asdict(run_cfg), tmp_path / "device")
            for pool in ("slc", "tlc"):
                for operation in ("read", "write", "trim"):
                    assert manifest["pools"][pool].get(operation.upper()+"_bytes", 0) == stats["storage"][pool][operation+"_bytes"]
        assert stats["pool"]["peak_resident_bytes"] <= 4096
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("overrides", [
    {"full_capacity_bytes": 8}, {"linear_capacity_bytes": 24},
    {"full_policy": "lru_k", "linear_policy": "checkpoint_lru"},
])
def test_shared_configuration_cannot_silently_apply_type_caps_or_mixed_rankings(tmp_path, overrides):
    with pytest.raises(ValueError, match="shared allocation"):
        DWPDSimulator(configuration(**overrides), tmp_path / "invalid")
