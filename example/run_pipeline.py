"""Run the complete DWPDSim to MQSim example configured by example/.env."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

from dotenv import load_dotenv

from dwpdsim import (
    DWPDSimulator,
    MemoryConfig,
    MemoryPolicyConfig,
    Request,
    SimulationConfig,
    StoragePolicyConfig,
    StorageTierConfig,
)
from dwpdsim.mqsim import convert_trace, run_mqsim

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / "example" / ".env"


def required(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        raise ValueError(f"{name} must be set in {ENV_PATH}")
    return value


def optional_int(name: str) -> int | None:
    value = os.environ.get(name, "")
    return None if value == "" else int(value)


def optional_float(name: str) -> float | None:
    value = os.environ.get(name, "")
    return None if value == "" else float(value)


def boolean(name: str) -> bool:
    value = required(name).lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def configured_path(name: str) -> Path:
    path = Path(required(name)).expanduser()
    return path if path.is_absolute() else ROOT / path


def read_requests(path: Path) -> Iterator[Request]:
    with path.open(encoding="utf-8") as source:
        for line in source:
            if not line.strip():
                continue
            record = json.loads(line)
            yield Request(
                timestamp_ns=int(record["timestamp_ns"]),
                request_id=int(record["request_id"]),
                affinity_id=int(record["affinity_id"]),
                hash_ids=[int(value) for value in record["hash_ids"]],
            )


def simulation_config() -> SimulationConfig:
    return SimulationConfig(
        block_size_bytes=int(required("DWPDSIM_BLOCK_SIZE_BYTES")),
        memory=MemoryConfig(capacity_bytes=int(required("DWPDSIM_MEMORY_CAPACITY_BYTES"))),
        slc=StorageTierConfig(
            capacity_bytes=int(required("DWPDSIM_SLC_CAPACITY_BYTES")),
            stream_count=int(required("DWPDSIM_SLC_STREAM_COUNT")),
        ),
        tlc=StorageTierConfig(
            capacity_bytes=int(required("DWPDSIM_TLC_CAPACITY_BYTES")),
            stream_count=int(required("DWPDSIM_TLC_STREAM_COUNT")),
        ),
        memory_policy=MemoryPolicyConfig(
            kind=required("DWPDSIM_MEMORY_POLICY"),
            admit_storage_hits=boolean("DWPDSIM_ADMIT_STORAGE_HITS"),
        ),
        storage_policy=StoragePolicyConfig(
            kind=required("DWPDSIM_STORAGE_POLICY"),
            fixed_tier=required("DWPDSIM_FIXED_TIER"),
            fixed_stream_id=int(required("DWPDSIM_FIXED_STREAM_ID")),
            slc_write_ratio=float(required("DWPDSIM_SLC_WRITE_RATIO")),
            slc_host_share=optional_float("DWPDSIM_SLC_HOST_SHARE"),
            idle_multiplier=float(required("DWPDSIM_IDLE_MULTIPLIER")),
            promotion_seconds=float(required("DWPDSIM_PROMOTION_SECONDS")),
            adaptation_gain=float(required("DWPDSIM_ADAPTATION_GAIN")),
            direct_gain=float(required("DWPDSIM_DIRECT_GAIN")),
            slc_soft_utilization=float(required("DWPDSIM_SLC_SOFT_UTILIZATION")),
            occupancy_decay=float(required("DWPDSIM_OCCUPANCY_DECAY")),
            logical_fill_fraction=float(required("DWPDSIM_LOGICAL_FILL_FRACTION")),
            slc_erase_budget=float(required("DWPDSIM_SLC_ERASE_BUDGET")),
            tlc_erase_budget=float(required("DWPDSIM_TLC_ERASE_BUDGET")),
            background_period_ns=int(required("DWPDSIM_BACKGROUND_PERIOD_NS")),
        ),
        simulation_end_ns=optional_int("DWPDSIM_SIMULATION_END_NS"),
        progress_interval_requests=int(required("DWPDSIM_PROGRESS_INTERVAL_REQUESTS")),
    )


def main() -> None:
    if not ENV_PATH.is_file():
        raise FileNotFoundError(
            f"missing {ENV_PATH}; copy example/.env.example to example/.env first"
        )
    load_dotenv(ENV_PATH)

    output_dir = configured_path("DWPDSIM_OUTPUT_DIR")
    output_dir.mkdir(parents=True, exist_ok=True)
    trace_path = output_dir / "simulation_trace.csv"
    metrics_path = output_dir / "simulation_metrics.json"
    mqsim_output_dir = output_dir / "mqsim"

    simulator = DWPDSimulator(simulation_config(), trace_path)
    with simulator:
        simulator.run(read_requests(configured_path("DWPDSIM_REQUESTS_PATH")))
    simulator.write_stats(metrics_path)

    manifest = convert_trace(
        trace_path,
        metrics_path,
        mqsim_output_dir,
        configured_path("DWPDSIM_SSD_CONFIG"),
        event_limit=optional_int("DWPDSIM_EVENT_LIMIT"),
    )
    run_mqsim(
        configured_path("DWPDSIM_MQSIM_BINARY"),
        mqsim_output_dir,
        manifest,
    )

    print(
        json.dumps(
            {
                "trace": str(trace_path),
                "metrics": str(metrics_path),
                "manifest": str(mqsim_output_dir / "manifest.json"),
                "summary": str(mqsim_output_dir / "summary.json"),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
