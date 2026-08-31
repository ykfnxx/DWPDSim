"""Analyze DWPDSim statistics and estimate SSD write endurance."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

SECONDS_PER_DAY = 86_400


def analyze(stats: dict, slc_wa: float, tlc_wa: float) -> dict:
    """Calculate logical writes, estimated physical writes, and DWPD."""
    if not slc_wa >= 1:
        raise ValueError("slc_wa must be greater than or equal to 1")
    if not tlc_wa >= 1:
        raise ValueError("tlc_wa must be greater than or equal to 1")

    duration_seconds = stats["time"]["duration_seconds"]
    slc_capacity_bytes = stats["configuration"]["slc_capacity_bytes"]
    tlc_capacity_bytes = stats["configuration"]["tlc_capacity_bytes"]

    if not duration_seconds > 0:
        raise ValueError("duration_seconds must be greater than 0")
    if not slc_capacity_bytes > 0:
        raise ValueError("slc_capacity_bytes must be greater than 0")
    if not tlc_capacity_bytes > 0:
        raise ValueError("tlc_capacity_bytes must be greater than 0")

    dram_to_slc_bytes = stats["writes_from_dram"]["slc"]["bytes"]
    dram_to_tlc_bytes = stats["writes_from_dram"]["tlc"]["bytes"]
    slc_to_tlc_bytes = stats["transfers"]["slc_to_tlc"]["bytes"]
    tlc_to_slc_bytes = stats["transfers"]["tlc_to_slc"]["bytes"]

    days = duration_seconds / SECONDS_PER_DAY
    system_input_write_bytes = dram_to_slc_bytes + dram_to_tlc_bytes
    slc_logical_write_bytes = dram_to_slc_bytes + tlc_to_slc_bytes
    tlc_logical_write_bytes = dram_to_tlc_bytes + slc_to_tlc_bytes
    slc_physical_write_bytes = slc_logical_write_bytes * slc_wa
    tlc_physical_write_bytes = tlc_logical_write_bytes * tlc_wa

    return {
        "duration_seconds": duration_seconds,
        "days": days,
        "capacity_bytes": {
            "slc": slc_capacity_bytes,
            "tlc": tlc_capacity_bytes,
        },
        "wa": {
            "slc": slc_wa,
            "tlc": tlc_wa,
        },
        "input_write_bytes": {
            "dram_to_slc": dram_to_slc_bytes,
            "dram_to_tlc": dram_to_tlc_bytes,
            "slc_to_tlc": slc_to_tlc_bytes,
            "tlc_to_slc": tlc_to_slc_bytes,
        },
        "system_input_write_bytes": system_input_write_bytes,
        "logical_write_bytes": {
            "slc": slc_logical_write_bytes,
            "tlc": tlc_logical_write_bytes,
        },
        "estimated_physical_write_bytes": {
            "slc": slc_physical_write_bytes,
            "tlc": tlc_physical_write_bytes,
        },
        "dwpd": {
            "system_equivalent": system_input_write_bytes
            / (slc_capacity_bytes + tlc_capacity_bytes)
            / days,
            "slc": slc_physical_write_bytes / slc_capacity_bytes / days,
            "tlc": tlc_physical_write_bytes / tlc_capacity_bytes / days,
        },
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Estimate SLC and TLC physical writes and DWPD.")
    parser.add_argument("stats", type=Path, help="path to DWPDSim statistics JSON")
    parser.add_argument("--slc-wa", type=float, required=True)
    parser.add_argument("--tlc-wa", type=float, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    with args.stats.open(encoding="utf-8") as stats_file:
        stats = json.load(stats_file)

    result = analyze(stats, args.slc_wa, args.tlc_wa)
    with args.output.open("w", encoding="utf-8") as output_file:
        json.dump(result, output_file, indent=2)
        output_file.write("\n")


if __name__ == "__main__":
    main()
