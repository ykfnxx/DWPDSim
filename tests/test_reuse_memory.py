"""Observable replay behavior for past-trained reuse policy and request diagnostics."""

import csv
from dataclasses import replace

import numpy as np
import pytest
from test_memory_performance import configuration

from dwpdsim import DWPDSimulator, MemoryPolicyConfig, StoragePolicyConfig


def config(memory=2, **options):
    settings = {
        "kind": "reuse_lru",
        "admit_storage_hits": False,
        "alpha": 1,
        "max_eviction_blocks": 1,
        "reuse_edges_s": [0, 60, 3600],
        "reuse_survival": [[1, 1, 1]] * 9,
    }
    settings.update(options)
    return replace(
        configuration(memory, 32),
        memory_policy=MemoryPolicyConfig(**settings),
        storage_policy=StoragePolicyConfig(kind="infinite_storage"),
    )


@pytest.mark.parametrize("probability,dropped", [(0, 0), (0.5, 1)])
def test_conditional_dump_changes_storage_survival(tmp_path, probability, dropped):
    cfg = config(memory=1, reuse_dump_probability=probability)
    with DWPDSimulator(cfg, tmp_path / "unused") as sim:
        sim.process(0, 1, 1, [1])
        sim.process(10**9, 2, 2, [2])
        assert sim.stats()["memory"]["drop_blocks"] == dropped
        sim.process(2 * 10**9, 3, 1, [1])
    assert sim.stats()["accesses"]["global_misses"] == 2 + dropped
    assert sim.stats()["accesses"]["tlc_hits"] == 1 - dropped


@pytest.mark.parametrize("priority,hit", [(False, 0), (True, 1)])
def test_priority_preserves_fast_session_over_colder_prediction(tmp_path, priority, hit):
    curves = [[1, 1, 1] for _ in range(9)]
    curves[3] = [1, 0.1, 0.1]
    cfg = config(reuse_survival=curves, reuse_priority=priority)
    with DWPDSimulator(cfg, tmp_path / "unused") as sim:
        sim.process(0, 0, 1, [1])
        sim.process(10**9, 1, 1, [1])
        sim.process(2 * 10**9, 2, 2, [2])
        sim.process(3 * 10**9, 3, 3, [3])
        before = sim.stats()["accesses"]["memory_hits"]
        sim.process(4 * 10**9, 4, 1, [1])
        assert sim.stats()["accesses"]["memory_hits"] - before == hit


@pytest.mark.parametrize("threshold,expected_reads", [(0.5, 1), (0.95, 2)])
def test_selective_readmission_uses_observed_session_gap(tmp_path, threshold, expected_reads):
    curves = [[1, 0.1, 0.1]] * 9
    cfg = config(
        memory=1, admit_storage_hits=True, reuse_survival=curves, reuse_admit_probability=threshold
    )
    with DWPDSimulator(cfg, tmp_path / "unused") as sim:
        sim.process(0, 0, 1, [1])
        sim.process(10**9, 1, 2, [2])
        sim.process(2 * 10**9, 2, 1, [1])
        sim.process(3 * 10**9, 3, 1, [1])
    assert sim.stats()["storage"]["tlc"]["reads"]["blocks"] == expected_reads


def test_request_diagnostics_and_batch_preserve_replay(tmp_path):
    cfg = config(
        reuse_dump_probability=0.5,
        reuse_priority=True,
        reuse_survival=[[1, 0.5, 0.1]] * 9,
        reuse_prefix_keep=[[[1, 0.8, 0.4]] * 2] * 9,
        reuse_cost_weighted=True,
    )
    states = []
    for diagnostic, batch in [(False, False), (True, False), (True, True)]:
        with DWPDSimulator(cfg, tmp_path / "unused") as sim:
            if diagnostic:
                sim.enable_request_diagnostics(tmp_path / "requests.csv")
            if batch:
                sim.process_batch(
                    np.array([0, 10**9, 2 * 10**9], dtype=np.uint64),
                    np.array([0, 1, 2], dtype=np.uint64),
                    np.array([1, 2, 1], dtype=np.uint64),
                    np.array([0, 2, 3, 5], dtype=np.uint64),
                    np.array([1, 2, 3, 1, 2], dtype=np.uint64),
                )
            else:
                for i, (sid, hashes) in enumerate([(1, [1, 2]), (2, [3]), (1, [1, 2])]):
                    sim.process(i * 10**9, i, sid, hashes)
        states.append(sim.stats())
    assert states[0] == states[1] == states[2]
    with (tmp_path / "requests.csv").open() as f:
        rows = list(csv.DictReader(f))
    assert sum(int(r["prefix_misses"]) for r in rows) == states[0]["accesses"]["global_misses"]
    assert (
        sum(sum(int(r[k]) for k in ["memory_hits", "storage_hits", "raw_misses"]) for r in rows)
        == 5
    )


def test_invalid_survival_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="nonincreasing"):
        DWPDSimulator(config(reuse_survival=[[1, 0.1, 0.8]] * 9), tmp_path / "unused")


@pytest.mark.parametrize("prefix,hit", [(False, 0), (True, 1)])
def test_prefix_retention_changes_evicted_tail(tmp_path, prefix, hit):
    keep = [[[1, 0 if prefix else 1, 0 if prefix else 1]] * 2] * 9
    cfg = config(
        memory=3, reuse_survival=[[1, 0.1, 0.1]] * 9, reuse_priority=True, reuse_prefix_keep=keep
    )
    with DWPDSimulator(cfg, tmp_path / "unused") as sim:
        sim.process(0, 0, 2, [3])
        sim.process(0, 1, 1, [1, 2])
        sim.process(0, 2, 3, [4])
        before = sim.stats()["accesses"]["memory_hits"]
        sim.process(0, 3, 2, [3])
        assert sim.stats()["accesses"]["memory_hits"] - before == hit
    work = sim.memory_performance()
    assert sum(work["evicted_residency_bins"]) == sim.stats()["memory"]["evicted_blocks"]


@pytest.mark.parametrize("weighted,hits", [(False, 1), (True, 2)])
def test_cost_protects_context_when_drop_would_require_recompute(tmp_path, weighted, hits):
    cfg = config(
        memory=3,
        reuse_survival=[[1, 0.1, 0.1]] * 9,
        reuse_priority=True,
        reuse_prefix_keep=[[[1, 1, 1]] * 2] * 9,
        reuse_cost_weighted=weighted,
        retention_ns=0,
    )
    with DWPDSimulator(cfg, tmp_path / "unused") as sim:
        sim.process(0, 0, 1, [1, 2])
        sim.process(0, 1, 2, [3])
        sim.process(10**9, 2, 3, [4])
        before = sim.stats()["accesses"]["memory_hits"]
        sim.process(10**9, 3, 1, [1, 2])
        assert sim.stats()["accesses"]["memory_hits"] - before == hits


@pytest.mark.parametrize("wait,hits", [(1, 1), (120, 0)])
def test_residency_value_changes_after_waiting_without_hint(tmp_path, wait, hits):
    curves = [[1, 1, 0.1] for _ in range(9)]
    curves[3] = [1, 0.1, 0.1]
    cfg = config(
        reuse_priority=True, reuse_survival=curves, reuse_prefix_keep=[[[1, 1, 1]] * 2] * 9
    )
    with DWPDSimulator(cfg, tmp_path / "unused") as sim:
        sim.process(0, 0, 1, [1])
        sim.process(0, 1, 1, [1])
        sim.process(0, 2, 2, [2])
        sim.process(wait * 10**9, 3, 3, [3])
        before = sim.stats()["accesses"]["memory_hits"]
        sim.process(wait * 10**9, 4, 1, [1])
        assert sim.stats()["accesses"]["memory_hits"] - before == hits
