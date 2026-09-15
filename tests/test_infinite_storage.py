"""Infinite Storage preserves Memory decisions and logical I/O of a non-evicting TLC."""

from dataclasses import replace

import pytest
from test_memory_performance import configuration, workload

from dwpdsim import DWPDSimulator, MemoryPolicyConfig, StoragePolicyConfig, StorageTierConfig


@pytest.mark.parametrize("kind,retention", [("baseline_lru", None)] + [
    (kind, retention) for kind in ("indexed_lru", "context_lru") for retention in (None, 0, 5)
])
@pytest.mark.parametrize("admit", [False, True])
def test_infinite_matches_large_storage(tmp_path, kind, admit, retention):
    cfg = replace(
        configuration(7, 4096),
        slc=StorageTierConfig(512, 1),
        tlc=StorageTierConfig(4096 * 512, 1),
        memory_policy=MemoryPolicyConfig(
            kind=kind, alpha=0.5, admit_storage_hits=admit, retention_ns=retention,
        ),
    )
    requests = list(workload(count=300))
    results = []
    for mode in ["baseline_fixed_lru", "infinite_storage"]:
        selected = replace(cfg, storage_policy=StoragePolicyConfig(kind=mode))
        if mode == "infinite_storage":
            selected = replace(selected, slc=StorageTierConfig(0, 0), tlc=StorageTierConfig(0, 0))
        trace = tmp_path / f"{mode}.csv"
        with DWPDSimulator(selected, trace) as sim:
            for request in requests:
                sim.process(*request)
        stats = sim.stats()
        if mode == "infinite_storage":
            assert not trace.exists()
            assert stats["trace"]["events"] == 0
            assert stats["configuration"]["storage_mode"] == "infinite_storage"
            assert stats["storage"]["tlc"]["capacity_bytes"] is None
            assert all(value == 0 for value in sim.storage_performance().values())
        del stats["configuration"]
        del stats["trace"]
        for tier in ("slc", "tlc"):
            del stats["storage"][tier]["capacity_bytes"]
        results.append(stats)
    assert results[0] == results[1]
