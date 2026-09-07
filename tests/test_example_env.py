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
