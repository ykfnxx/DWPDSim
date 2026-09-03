import csv
import json
import xml.etree.ElementTree as ET
from pathlib import Path

from dwpdsim.mqsim import convert_trace

MIB = 1024 * 1024
TRACE_FIELDS = [
    "sequence",
    "timestamp",
    "request_sequence",
    "operation",
    "storage_tier",
    "stream_id",
    "offset_bytes",
    "length_bytes",
    "node_id",
    "hash_id",
    "reason",
]


def test_conversion_generates_one_mqsim_flow_per_storage_tier_stream(tmp_path):
    trace_path = tmp_path / "dwpdsim.csv"
    metrics_path = tmp_path / "metrics.json"
    output_path = tmp_path / "mqsim"
    rows = [
        [0, 100, 0, "WRITE", "SLC", 3, 0, 8 * MIB, 10, 1000, "MEMORY_EVICTION"],
        [1, 101, 1, "READ", "SLC", 3, 0, 8 * MIB, 10, 1000, "STORAGE_HIT"],
        [2, 102, 2, "WRITE", "TLC", 7, 0, 8 * MIB, 20, 2000, "MEMORY_EVICTION"],
        [3, 103, 3, "WRITE", "SLC", 5, 0, 8 * MIB, 11, 1100, "MEMORY_EVICTION"],
        [4, 104, 4, "TRIM", "SLC", 3, 0, 8 * MIB, 10, 1000, "STORAGE_EVICTION"],
        [5, 105, 5, "TRIM", "TLC", 7, 0, 8 * MIB, 20, 2000, "STORAGE_EVICTION"],
        [6, 106, 6, "WRITE", "SLC", 3, 0, 8 * MIB, 12, 1200, "MEMORY_EVICTION"],
    ]
    with trace_path.open("w", newline="", encoding="utf-8") as trace_file:
        writer = csv.writer(trace_file)
        writer.writerow(TRACE_FIELDS)
        writer.writerows(rows)

    metrics_path.write_text(
        json.dumps(
            {
                "time": {"unit": "us"},
                "configuration": {"block_size_bytes": 8 * MIB},
            }
        ),
        encoding="utf-8",
    )
    repository = Path(__file__).parents[1]
    manifest = convert_trace(
        trace_path,
        metrics_path,
        output_path,
        {
            "slc": repository / "example/mqsim/ssdconfig-slc.xml",
            "tlc": repository / "example/mqsim/ssdconfig-tlc.xml",
        },
    )

    assert manifest["events"] == 7
    assert manifest["tiers"]["slc"]["timestamp_origin"] == 100
    assert [stream["dwpdsim_stream_id"] for stream in manifest["tiers"]["slc"]["streams"]] == [
        3,
        5,
    ]
    assert (output_path / "slc/stream-3.trace").read_text(encoding="utf-8").splitlines() == [
        "0 0 0 16384 0",
        "1000 0 0 16384 1",
        "4000 0 0 16384 2",
        "6000 0 0 16384 0",
    ]
    assert (output_path / "slc/stream-5.trace").read_text(encoding="utf-8").strip() == (
        "3000 0 0 16384 0"
    )
    assert (output_path / "tlc/stream-7.trace").read_text(encoding="utf-8").splitlines() == [
        "0 0 0 16384 0",
        "3000 0 0 16384 2",
    ]

    workload = ET.parse(output_path / "slc/workload.xml").getroot()
    flows = workload.findall("IO_Scenario/IO_Flow_Parameter_Set_Trace_Based")
    assert len(flows) == 2
    assert [flow.findtext("Time_Unit") for flow in flows] == ["NANOSECOND", "NANOSECOND"]
    assert [flow.findtext("Device_Level_Data_Caching_Mode") for flow in flows] == [
        "TURNED_OFF",
        "TURNED_OFF",
    ]
