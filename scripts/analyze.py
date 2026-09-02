"""Estimate physical writes and DWPD from DWPDSim logical-write metrics."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

SECONDS_PER_DAY = 86_400
TIMESTAMP_SECONDS = {
    "s": 1.0,
    "seconds": 1.0,
    "ms": 1e-3,
    "milliseconds": 1e-3,
    "us": 1e-6,
    "microseconds": 1e-6,
    "ns": 1e-9,
    "nanoseconds": 1e-9,
}


def analyze(stats: dict, slc_wa: float, tlc_wa: float) -> dict:
    if slc_wa < 1 or tlc_wa < 1:
        raise ValueError("write amplification must be at least 1")

    unit = stats["time"]["unit"]
    if unit not in TIMESTAMP_SECONDS:
        raise ValueError(f"unsupported timestamp unit: {unit}")

    duration_seconds = stats["time"]["duration"] * TIMESTAMP_SECONDS[unit]
    if duration_seconds <= 0:
        raise ValueError("simulation duration must be positive")

    slc_capacity = stats["configuration"]["slc_capacity_bytes"]
    tlc_capacity = stats["configuration"]["tlc_capacity_bytes"]
    slc_logical = stats["storage"]["slc"]["writes"]["bytes"]
    tlc_logical = stats["storage"]["tlc"]["writes"]["bytes"]
    slc_physical = slc_logical * slc_wa
    tlc_physical = tlc_logical * tlc_wa
    days = duration_seconds / SECONDS_PER_DAY

    return {
        "duration_seconds": duration_seconds,
        "days": days,
        "wa": {"slc": slc_wa, "tlc": tlc_wa},
        "logical_write_bytes": {"slc": slc_logical, "tlc": tlc_logical},
        "estimated_physical_write_bytes": {
            "slc": slc_physical,
            "tlc": tlc_physical,
        },
        "dwpd": {
            "system_equivalent": (slc_logical + tlc_logical) / (slc_capacity + tlc_capacity) / days,
            "slc": slc_physical / slc_capacity / days,
            "tlc": tlc_physical / tlc_capacity / days,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stats", type=Path)
    parser.add_argument("--slc-wa", type=float, required=True)
    parser.add_argument("--tlc-wa", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    stats = json.loads(args.stats.read_text(encoding="utf-8"))
    result = analyze(stats, args.slc_wa, args.tlc_wa)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
