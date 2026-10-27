"""Compare public component state with the backward K-distance definition."""

import random

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


class ReferenceCache:
    def __init__(self, k, dump):
        self.k = k
        self.dump = dump
        self.dram = set()
        self.ssd = set()
        self.references = {}
        self.admitted = {}
        self.admissions = 0

    def victim(self, clock):
        cold = [node for node in self.dram if len(self.references.get(node, [])) < self.k]
        if cold:
            unreferenced = [node for node in cold if not self.references.get(node)]
            if unreferenced:
                candidates = unreferenced
            else:
                oldest_demand = min(self.references[node][-1] for node in cold)
                candidates = [node for node in cold if self.references[node][-1] == oldest_demand]
        else:
            distances = {node: clock - self.references[node][-self.k] for node in self.dram}
            farthest = max(distances.values())
            candidates = [node for node in self.dram if distances[node] == farthest]
            oldest_demand = min(self.references[node][-1] for node in candidates)
            candidates = [node for node in candidates if self.references[node][-1] == oldest_demand]
        return min(candidates, key=self.admitted.__getitem__)

    def admit(self, node, clock):
        if node in self.dram:
            return
        if len(self.dram) == 4:
            victim = self.victim(clock)
            self.dram.remove(victim)
            del self.admitted[victim]
            if self.dump:
                self.ssd.add(victim)
        self.admissions += 1
        self.admitted[node] = self.admissions
        self.dram.add(node)

    def publish(self, node, clock):
        if node not in self.dram and node not in self.ssd:
            self.admit(node, clock)

    def demand(self, node, clock):
        self.references.setdefault(node, []).append(clock)
        if node in self.ssd:
            self.admit(node, clock)


def events():
    # Repeated demand wraps even a K=5 history; later supplies evict cold objects.
    rows = [("publish", "full", 1), ("publish", "linear", 1)]
    rows.extend([("request", "both", 1)] * 7)
    rows.extend(("publish", component, node)
                for node in range(2, 7) for component in ("full", "linear"))
    rows.append(("request", "both", 2))
    rng = random.Random(4801)
    for _ in range(80):
        action = "publish" if rng.random() < 0.35 else "request"
        component = rng.choice(("full", "linear") if action == "publish"
                               else ("full", "linear", "both"))
        rows.append((action, component, rng.randrange(1, 13)))
    return rows


@pytest.mark.parametrize("k", [1, 2, 3, 5])
@pytest.mark.parametrize("dump", [False, True])
def test_lru_k_matches_reference_after_demands_publication_and_reload(tmp_path, k, dump):
    cfg = SimulationConfig(
        memory=MemoryConfig(64),
        slc=StorageTierConfig(8, 1),
        tlc=StorageTierConfig(8, 1),
        block_size_bytes=8,
        storage_policy=StoragePolicyConfig(kind="infinite_storage"),
        hybrid_attention=HybridAttentionConfig(
            pool_allocation="fixed",
            enabled=True,
            full_policy="lru_k",
            linear_policy="lru_k",
            lru_k=k,
            full_block_bytes=8,
            linear_checkpoint_bytes=8,
            full_capacity_bytes=32,
            linear_capacity_bytes=32,
            emit_io_trace=False,
            dump_on_eviction=dump,
        ),
    )
    caches = {component: ReferenceCache(k, dump) for component in ("full", "linear")}
    request_clock = 0
    with DWPDSimulator(cfg, tmp_path / "reference") as sim:
        for seq, (action, component, node) in enumerate(events()):
            if action == "request":
                request_clock += 1
            components = ("full", "linear") if component == "both" else (component,)
            for requested in components:
                if action == "request":
                    caches[requested].demand(node, request_clock)
                else:
                    caches[requested].publish(node, request_clock)
            sim.process_hybrid(HybridEvent(
                seq, seq, action, [node], request_id=seq, components=component,
            ))
            for requested, cache in caches.items():
                for identity in range(1, 13):
                    state = sim.hybrid_inspect(identity, requested)
                    context = (seq, action, component, node, requested, identity)
                    assert state["in_dram"] == (identity in cache.dram), context
                    assert state["on_storage"] == (identity in cache.ssd), context
        stats = sim.hybrid_stats()
        assert stats["full"]["evictions"] > 0
        assert stats["linear"]["evictions"] > 0
        if dump:
            assert stats["full"]["storage_hits"] > 0
            assert stats["linear"]["storage_hits"] > 0
