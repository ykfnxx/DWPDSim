"""Exercise the example dotenv -> config -> native replay path."""

import os
import runpy
from pathlib import Path

import pytest
from dotenv import load_dotenv

from dwpdsim import DWPDSimulator

ROOT = Path(__file__).resolve().parents[1]


def configure(monkeypatch, tmp_path, retention, *, override=None):
    for name in list(os.environ):
        if name.startswith("DWPDSIM_"):
            monkeypatch.delenv(name)
    text = (
        (ROOT / "example/.env.example")
        .read_text()
        .replace("DWPDSIM_MEMORY_POLICY=baseline_lru", "DWPDSIM_MEMORY_POLICY=indexed_lru")
    )
    line = "DWPDSIM_MEMORY_RETENTION_NS="
    text = text.replace(line + "\n", "" if retention is None else line + retention + "\n")
    path = tmp_path / ".env"
    path.write_text(text)
    # Register dotenv's additions with monkeypatch so the caller's environment is restored.
    for row in text.splitlines():
        if row.startswith("DWPDSIM_"):
            name = row.split("=", 1)[0]
            monkeypatch.setenv(name, "")
            monkeypatch.delenv(name)
    if override is not None:
        monkeypatch.setenv("DWPDSIM_MEMORY_RETENTION_NS", override)
    load_dotenv(path)
    namespace = runpy.run_path(str(ROOT / "example/run_pipeline.py"))
    return namespace["simulation_config"]()


@pytest.mark.parametrize(
    "retention,evict_ns,dropped",
    [
        (None, 11, False),
        ("", 11, False),
        ("10", 10, False),
        ("10", 11, True),
        ("0", 0, False),
        ("0", 1, True),
    ],
)
def test_env_retention_reaches_native_eviction(monkeypatch, tmp_path, retention, evict_ns, dropped):
    config = configure(monkeypatch, tmp_path, retention)
    with DWPDSimulator(config, tmp_path / "trace.csv") as sim:
        sim.process(0, 0, 0, [1, 2])
        sim.process(evict_ns, 1, 0, [3])
    stats = sim.stats()
    assert stats["memory"]["drop_blocks"] == 2 * int(dropped)
    assert stats["trace"]["events"] == 2 * int(not dropped)


def test_shell_environment_overrides_dotenv_retention(monkeypatch, tmp_path):
    config = configure(monkeypatch, tmp_path, "10", override="0")
    with DWPDSimulator(config, tmp_path / "override.csv") as sim:
        sim.process(0, 0, 0, [1, 2])
        sim.process(1, 1, 0, [3])
    assert sim.stats()["memory"]["drop_blocks"] == 2


@pytest.mark.parametrize("retention", ["-1", str(2**64)])
def test_env_rejects_out_of_range_retention(monkeypatch, tmp_path, retention):
    with pytest.raises(ValueError, match="DWPDSIM_MEMORY_RETENTION_NS"):
        configure(monkeypatch, tmp_path, retention)


@pytest.mark.parametrize(
    "mode,counts", [("scan", "false"), ("fused", "false"), ("indexed", "true")]
)
def test_env_rr_options_reach_native_reclaim(monkeypatch, tmp_path, mode, counts):
    configure(monkeypatch, tmp_path, "")
    monkeypatch.setenv("DWPDSIM_STORAGE_POLICY", "wear_share_round_robin")
    monkeypatch.setenv("DWPDSIM_SLC_CAPACITY_BYTES", "16384")
    monkeypatch.setenv("DWPDSIM_TLC_CAPACITY_BYTES", "16384")
    monkeypatch.setenv("DWPDSIM_RR_VICTIM_SEARCH", mode)
    monkeypatch.setenv("DWPDSIM_RR_SUBTREE_COUNTS", counts)
    monkeypatch.setenv("DWPDSIM_RR_VERIFY_VICTIMS", "true")
    monkeypatch.setenv("DWPDSIM_RR_PROFILE", "true")
    namespace = runpy.run_path(str(ROOT / "example/run_pipeline.py"))
    with DWPDSimulator(namespace["simulation_config"](), tmp_path / "rr.csv") as sim:
        for i in range(40):
            sim.process(i, i, 0, [i * 2, i * 2 + 1])
    work = sim.storage_performance()
    assert work["decisions"] > 0
    assert work["decisions"] == work["verified_decisions"]
    assert work["decision_ns"] > 0
    assert bool(work["ancestor_updates"]) == (counts == "true")


@pytest.mark.parametrize("alpha,evicted", [("0.01", 3), ("1", 1)])
def test_env_context_alpha_reaches_native_eviction(monkeypatch, tmp_path, alpha, evicted):
    configure(monkeypatch, tmp_path, "")
    monkeypatch.setenv("DWPDSIM_MEMORY_POLICY", "context_lru")
    monkeypatch.setenv("DWPDSIM_MEMORY_ALPHA", alpha)
    monkeypatch.setenv("DWPDSIM_MEMORY_CAPACITY_BYTES", str(4 * 4096))
    namespace = runpy.run_path(str(ROOT / "example/run_pipeline.py"))
    config = namespace["simulation_config"]()
    with DWPDSimulator(config, tmp_path / "context-env.csv") as sim:
        sim.process(0, 0, 0, [1, 2, 3])
        sim.process(1, 1, 0, [4])
        sim.process(2, 2, 0, [5])
        assert sim.stats()["memory"]["evicted_blocks"] == evicted


def test_env_infinite_storage_skips_mqsim(monkeypatch, tmp_path):
    import json

    configure(monkeypatch, tmp_path, "")
    monkeypatch.setenv("DWPDSIM_STORAGE_POLICY", "infinite_storage")
    monkeypatch.setenv("DWPDSIM_OUTPUT_DIR", str(tmp_path / "output"))
    requests = tmp_path / "requests.jsonl"
    requests.write_text("\n".join(json.dumps({
        "timestamp_ns": i, "request_id": i, "affinity_id": 1, "hash_ids": path,
    }) for i, path in enumerate([[1, 2, 3], [4], [1, 2, 3]])))
    monkeypatch.setenv("DWPDSIM_REQUESTS_PATH", str(requests))
    namespace = runpy.run_path(str(ROOT / "example/run_pipeline.py"))
    globals_ = namespace["main"].__globals__
    monkeypatch.setitem(globals_, "ENV_PATH", tmp_path / ".env")

    def unexpected(*args, **kwargs):
        pytest.fail("infinite_storage must not run MQSim conversion or replay")

    monkeypatch.setitem(globals_, "convert_trace", unexpected)
    monkeypatch.setitem(globals_, "run_mqsim", unexpected)
    namespace["main"]()
    stats = json.loads((tmp_path / "output/simulation_metrics.json").read_text())
    assert stats["accesses"]["tlc_hits"] > 0
    assert stats["trace"]["events"] == 0
    assert not (tmp_path / "output/simulation_trace.csv").exists()
