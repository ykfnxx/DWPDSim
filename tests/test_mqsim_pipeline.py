import csv
import json
import xml.etree.ElementTree as ET

import pytest

from dwpdsim.mqsim import (
    CHANNEL_INTEGER_ATTRIBUTES,
    FTL_INTEGER_ATTRIBUTES,
    MAX_NVME_SECTORS,
    POOL_INTEGER_ATTRIBUTES,
    SECTOR_SIZE_BYTES,
    convert_trace,
    read_mqsim_results,
)

TRACE_FIELDS = [
    "sequence",
    "timestamp_ns",
    "request_id",
    "access_sequence",
    "operation",
    "storage_tier",
    "stream_id",
    "offset_bytes",
    "length_bytes",
    "node_id",
    "hash_id",
    "reason",
    "move_id",
    "depends_on_sequence",
]
DAY_NS = 86_400 * 1_000_000_000


def ssd_configuration(capacity, *, start_ns=0, end_ns=DAY_NS):
    sectors = capacity // SECTOR_SIZE_BYTES
    return f"""\
<Execution_Parameter_Set>
  <Device_Parameter_Set>
    <Enabled_Preconditioning>false</Enabled_Preconditioning>
    <Memory_Type>FLASH</Memory_Type>
    <HostInterface_Type>NVME</HostInterface_Type>
    <Address_Mapping>PAGE_LEVEL</Address_Mapping>
    <Measurement_Start_Time_Ns>{start_ns}</Measurement_Start_Time_Ns>
    <Measurement_End_Time_Ns>{end_ns}</Measurement_End_Time_Ns>
    <Flash_Media_Profile>
      <Media_Profile_ID>slc_profile</Media_Profile_ID><Flash_Technology>SLC</Flash_Technology>
    </Flash_Media_Profile>
    <Flash_Media_Profile>
      <Media_Profile_ID>tlc_profile</Media_Profile_ID><Flash_Technology>TLC</Flash_Technology>
    </Flash_Media_Profile>
    <Flash_Pool_Parameter_Set>
      <Pool_ID>slc</Pool_ID><Channel_IDs>0</Channel_IDs>
      <Logical_Capacity_In_Sectors>{sectors}</Logical_Capacity_In_Sectors>
      <Media_Profile_ID>slc_profile</Media_Profile_ID>
    </Flash_Pool_Parameter_Set>
    <Flash_Pool_Parameter_Set>
      <Pool_ID>tlc</Pool_ID><Channel_IDs>1</Channel_IDs>
      <Logical_Capacity_In_Sectors>{sectors}</Logical_Capacity_In_Sectors>
      <Media_Profile_ID>tlc_profile</Media_Profile_ID>
    </Flash_Pool_Parameter_Set>
  </Device_Parameter_Set>
</Execution_Parameter_Set>
"""


def write_inputs(
    tmp_path,
    rows,
    *,
    slc_streams=2,
    tlc_streams=2,
    capacity_bytes=None,
    declared_events=None,
):
    capacity = 128 * 1024 * 1024 if capacity_bytes is None else capacity_bytes
    trace_path = tmp_path / "canonical.csv"
    metrics_path = tmp_path / "metrics.json"
    config_path = tmp_path / "ssd.xml"
    with trace_path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output)
        writer.writerow(TRACE_FIELDS)
        writer.writerows(rows)
    metrics_path.write_text(
        json.dumps(
            {
                "time": {"unit": "ns"},
                "configuration": {
                    "block_size_bytes": 4096,
                    "slc_capacity_bytes": capacity,
                    "tlc_capacity_bytes": capacity,
                    "slc_stream_count": slc_streams,
                    "tlc_stream_count": tlc_streams,
                },
                "storage": {
                    "slc": {"host_write_bytes": 4096},
                    "tlc": {"host_write_bytes": 0},
                },
                "trace": {
                    "schema_version": 4,
                    "events": len(rows) if declared_events is None else declared_events,
                },
            }
        ),
        encoding="utf-8",
    )
    config_path.write_text(ssd_configuration(capacity), encoding="utf-8")
    return trace_path, metrics_path, config_path


def convert(tmp_path, rows, **kwargs):
    trace, metrics, config = write_inputs(tmp_path, rows, **kwargs)
    return convert_trace(trace, metrics, tmp_path / "mqsim", config)


def read_command_manifest(manifest):
    with open(manifest["command_manifest"], newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


def test_conversion_uses_fixed_all_stream_flow_mapping_and_absolute_time(tmp_path):
    manifest = convert(
        tmp_path,
        [
            [0, 100, 1, 0, "READ", "SLC", 1, 512, 1024, 10, 10, "STORAGE_HIT", 1, ""],
            [1, 100, 1, 0, "WRITE", "TLC", 0, 0, 1024, 10, 10, "ACCESS_MIGRATION", 1, 0],
            [2, 100, 1, 0, "TRIM", "SLC", 1, 512, 1024, 10, 10, "ACCESS_MIGRATION", 1, 1],
        ],
    )

    assert manifest["commands"] == 3
    assert manifest["max_flows"] == 8
    assert manifest["time"] == {
        "unit": "ns",
        "preserved_absolute_timestamps": True,
        "global_time_origin_ns": 0,
        "measurement_start_ns": 0,
        "measurement_end_ns": DAY_NS,
    }
    assert [
        (flow["flow_id"], flow["pool_id"], flow["dwpdsim_tier_local_stream_id"])
        for flow in manifest["flows"]
    ] == [
        (0, "slc", 0),
        (1, "slc", 1),
        (2, "tlc", 0),
        (3, "tlc", 1),
    ]
    assert (tmp_path / "mqsim/flow-0.trace").read_text(encoding="utf-8") == ""
    assert (tmp_path / "mqsim/flow-1.trace").read_text(encoding="utf-8").splitlines() == [
        "100 0 1 2 1 0 -1",
        "100 0 1 2 2 2 0,1",
    ]
    assert (tmp_path / "mqsim/flow-2.trace").read_text(encoding="utf-8").strip() == (
        "100 0 0 2 0 1 0"
    )

    assert manifest["pools"]["slc"]["stream_count"] == 2
    assert manifest["pools"]["slc"]["channel_ids"] == [0]
    assert manifest["pools"]["slc"]["dwpdsim_dump_host_write_bytes"] == 4096
    workload = ET.parse(manifest["workload"]).getroot()
    flows = workload.findall("IO_Scenario/IO_Flow_Parameter_Set_Trace_Based")
    assert len(flows) == 4
    assert [flow.findtext("Pool_ID") for flow in flows] == ["slc", "slc", "tlc", "tlc"]
    assert {flow.findtext("Trace_Format") for flow in flows} == {
        "DWPDSIM_DEPENDENCY_V1"
    }


def test_large_relocation_uses_unique_command_ids_and_serial_chunks(tmp_path):
    sectors = MAX_NVME_SECTORS + 10
    length = sectors * SECTOR_SIZE_BYTES
    manifest = convert(
        tmp_path,
        [
            [10, 5, "", "", "READ", "SLC", 0, 0, length, 1, 1, "BACKGROUND_MIGRATION", 7, ""],
            [11, 5, "", "", "WRITE", "TLC", 0, 0, length, 1, 1, "BACKGROUND_MIGRATION", 7, 10],
            [12, 5, "", "", "TRIM", "SLC", 0, 0, length, 1, 1, "BACKGROUND_MIGRATION", 7, 11],
        ],
        capacity_bytes=length,
    )

    commands = read_command_manifest(manifest)
    assert [row["command_request_id"] for row in commands] == [
        "0",
        "1",
        "2",
        "3",
        "4",
        "5",
    ]
    assert [
        (row["source_sequence"], row["chunk_index"], row["chunk_count"])
        for row in commands
    ] == [
        ("10", "0", "2"),
        ("10", "1", "2"),
        ("11", "0", "2"),
        ("11", "1", "2"),
        ("12", "0", "2"),
        ("12", "1", "2"),
    ]
    assert [row["depends_on_request_ids"] for row in commands] == [
        "-1",
        "0",
        "1",
        "2",
        "0,3",
        "1,4",
    ]


def test_lba_hazards_are_stream_local(tmp_path):
    manifest = convert(
        tmp_path,
        [
            [0, 10, 1, 0, "WRITE", "SLC", 0, 0, 4096, 1, 1, "MEMORY_DUMP", "", ""],
            [1, 20, "", "", "TRIM", "SLC", 0, 0, 4096, 1, 1, "CAPACITY_EVICTION", "", ""],
            [2, 20, 2, 1, "WRITE", "SLC", 0, 0, 4096, 2, 2, "MEMORY_DUMP", "", ""],
            [3, 20, 3, 2, "WRITE", "SLC", 1, 0, 4096, 3, 3, "MEMORY_DUMP", "", ""],
        ],
    )
    commands = read_command_manifest(manifest)
    assert [row["depends_on_request_ids"] for row in commands] == ["-1", "0", "1", "-1"]


def test_reads_are_parallel_and_next_mutation_waits_for_all_readers(tmp_path):
    manifest = convert(
        tmp_path,
        [
            [0, 10, 1, 0, "WRITE", "SLC", 0, 0, 4096, 1, 1, "MEMORY_DUMP", "", ""],
            [1, 20, 2, 1, "READ", "SLC", 0, 0, 4096, 1, 1, "STORAGE_HIT", "", ""],
            [2, 20, 3, 2, "READ", "SLC", 0, 0, 4096, 1, 1, "STORAGE_HIT", "", ""],
            [3, 20, "", "", "TRIM", "SLC", 0, 0, 4096, 1, 1, "CAPACITY_EVICTION", "", ""],
        ],
    )
    commands = read_command_manifest(manifest)
    assert [row["depends_on_request_ids"] for row in commands] == [
        "-1",
        "0",
        "0",
        "0,1,2",
    ]


def test_relocation_destination_reuse_has_source_and_address_predecessors(tmp_path):
    manifest = convert(
        tmp_path,
        [
            [0, 0, 1, 0, "WRITE", "TLC", 1, 0, 4096, 8, 8, "MEMORY_DUMP", "", ""],
            [1, 10, "", "", "TRIM", "TLC", 1, 0, 4096, 8, 8, "CAPACITY_EVICTION", "", ""],
            [2, 10, "", "", "READ", "SLC", 0, 0, 4096, 9, 9, "BACKGROUND_MIGRATION", 7, ""],
            [3, 10, "", "", "WRITE", "TLC", 1, 0, 4096, 9, 9, "BACKGROUND_MIGRATION", 7, 2],
            [4, 10, "", "", "TRIM", "SLC", 0, 0, 4096, 9, 9, "BACKGROUND_MIGRATION", 7, 3],
        ],
    )
    commands = read_command_manifest(manifest)
    assert commands[3]["depends_on_request_ids"] == "1,2"
    assert commands[4]["depends_on_request_ids"] == "2,3"


def test_total_configured_flow_count_is_limited_by_nvme_queues(tmp_path):
    trace, metrics, config = write_inputs(
        tmp_path,
        [],
        slc_streams=5,
        tlc_streams=4,
    )
    with pytest.raises(ValueError, match=r"requires 9 MQSim flows; the V1 limit is 8"):
        convert_trace(trace, metrics, tmp_path / "mqsim", config)


def test_canonical_header_is_exact_even_for_empty_trace(tmp_path):
    trace, metrics, config = write_inputs(tmp_path, [])
    trace.write_text("sequence,timestamp_ns\n", encoding="utf-8")
    with pytest.raises(ValueError, match="header does not match schema version 4"):
        convert_trace(trace, metrics, tmp_path / "mqsim", config)


def test_canonical_trace_rejects_negative_addresses(tmp_path):
    trace, metrics, config = write_inputs(
        tmp_path,
        [[0, 0, 1, 0, "WRITE", "SLC", 0, -512, 4096, 1, 1, "MEMORY_DUMP", "", ""]],
    )
    with pytest.raises(ValueError, match="offset_bytes must be nonnegative"):
        convert_trace(trace, metrics, tmp_path / "mqsim", config)


def test_canonical_row_count_must_match_metrics(tmp_path):
    trace, metrics, config = write_inputs(tmp_path, [], declared_events=1)
    with pytest.raises(ValueError, match="row count differs"):
        convert_trace(trace, metrics, tmp_path / "mqsim", config)


def test_event_limit_records_selected_declared_and_actual_counts(tmp_path):
    rows = [
        [0, 0, 1, 0, "WRITE", "SLC", 0, 0, 4096, 1, 1, "MEMORY_DUMP", "", ""],
        [1, 1, 2, 1, "WRITE", "SLC", 0, 4096, 4096, 2, 2, "MEMORY_DUMP", "", ""],
    ]
    trace, metrics, config = write_inputs(tmp_path, rows, declared_events=100)
    manifest = convert_trace(trace, metrics, tmp_path / "mqsim", config, event_limit=1)
    assert manifest["source_semantic_events"] == 1
    assert manifest["source_trace_events_declared"] == 100
    assert manifest["source_trace_events_actual"] == 2


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_tlc", "exactly the slc and tlc pools"),
        ("uppercase_slc", "exactly the slc and tlc pools"),
        ("capacity", "logical capacity differs"),
        ("window", "start_ns < end_ns"),
        ("profile", "unknown media profile"),
        ("preconditioning", "Enabled_Preconditioning must be false"),
        ("host_interface", "HostInterface_Type must be NVME"),
        ("memory_type", "Memory_Type must be FLASH"),
        ("mapping", "Address_Mapping must be PAGE_LEVEL"),
        ("technology", "media profile must use SLC technology"),
    ],
)
def test_ssd_configuration_contract(tmp_path, mutation, message):
    trace, metrics, config = write_inputs(tmp_path, [])
    root = ET.parse(config).getroot()
    pools = root.findall(".//Flash_Pool_Parameter_Set")
    if mutation == "missing_tlc":
        root.find(".//Device_Parameter_Set").remove(pools[1])
    elif mutation == "uppercase_slc":
        pools[0].find("Pool_ID").text = "SLC"
    elif mutation == "capacity":
        pools[0].find("Logical_Capacity_In_Sectors").text = "1"
    elif mutation == "window":
        root.find(".//Measurement_End_Time_Ns").text = "0"
    elif mutation == "profile":
        pools[0].find("Media_Profile_ID").text = "missing"
    elif mutation == "preconditioning":
        root.find(".//Enabled_Preconditioning").text = "true"
    elif mutation == "host_interface":
        root.find(".//HostInterface_Type").text = "SATA"
    elif mutation == "memory_type":
        root.find(".//Memory_Type").text = "DRAM"
    elif mutation == "mapping":
        root.find(".//Address_Mapping").text = "BLOCK_LEVEL"
    else:
        root.findall(".//Flash_Media_Profile")[0].find("Flash_Technology").text = "TLC"
    ET.ElementTree(root).write(config, encoding="unicode")
    with pytest.raises(ValueError, match=message):
        convert_trace(trace, metrics, tmp_path / "mqsim", config)


def test_relocation_groups_are_validated(tmp_path):
    incomplete = [
        [0, 0, "", "", "READ", "SLC", 0, 0, 4096, 1, 1, "BACKGROUND_MIGRATION", 1, ""],
        [1, 0, "", "", "WRITE", "TLC", 0, 0, 4096, 1, 1, "BACKGROUND_MIGRATION", 1, ""],
    ]
    trace, metrics, config = write_inputs(tmp_path, incomplete)
    with pytest.raises(ValueError, match="must contain READ, WRITE, TRIM"):
        convert_trace(trace, metrics, tmp_path / "mqsim", config)

    missing_access_read = [
        [0, 0, 1, 0, "READ", "SLC", 0, 0, 4096, 1, 1, "ACCESS_MIGRATION", 1, ""],
        [1, 0, 1, 0, "WRITE", "TLC", 0, 0, 4096, 1, 1, "ACCESS_MIGRATION", 1, 0],
        [2, 0, 1, 0, "TRIM", "SLC", 0, 0, 4096, 1, 1, "ACCESS_MIGRATION", 1, 1],
    ]
    trace, metrics, config = write_inputs(tmp_path, missing_access_read)
    with pytest.raises(ValueError, match="reuse exactly one storage-hit READ"):
        convert_trace(trace, metrics, tmp_path / "mqsim-2", config)

    wrong_source_location = [
        [0, 0, "", "", "READ", "SLC", 0, 0, 4096, 1, 1, "BACKGROUND_MIGRATION", 1, ""],
        [1, 0, "", "", "WRITE", "TLC", 0, 0, 4096, 1, 1, "BACKGROUND_MIGRATION", 1, 0],
        [2, 0, "", "", "TRIM", "SLC", 1, 0, 4096, 1, 1, "BACKGROUND_MIGRATION", 1, 1],
    ]
    trace, metrics, config = write_inputs(tmp_path, wrong_source_location)
    with pytest.raises(ValueError, match="inconsistent locations"):
        convert_trace(trace, metrics, tmp_path / "mqsim-3", config)


def write_mqsim_result(path, manifest, *, decimal_flow_bytes=False):
    root = ET.Element("MQSim_Results")
    host = ET.SubElement(root, "Host")
    for flow in reversed(manifest["flows"]):
        element = ET.SubElement(host, "Host.IO_Flow")
        operations = flow["operations"]
        byte_counts = flow["bytes"]
        values = {
            "Flow_ID": flow["flow_id"],
            "Pool_ID": flow["pool_id"],
            "Time_Unit": "nanosecond",
            "Generated_Request_Count": flow["commands"],
            "Completed_Request_Count": flow["commands"],
            "Read_Request_Count": operations["READ"],
            "Write_Request_Count": operations["WRITE"],
            "Trim_Request_Count": operations["TRIM"],
            "Bytes_Transferred_Read": byte_counts["READ"],
            "Bytes_Transferred_Write": byte_counts["WRITE"],
            "Bytes_Trimmed_Requested": byte_counts["TRIM"],
            "Measurement_Host_Write_Bytes": byte_counts["WRITE"],
            "Dependency_Wait_Time_Total": 0,
            "Dependency_Wait_Time_Max": 0,
        }
        for name, value in values.items():
            text = str(value)
            if decimal_flow_bytes and name == "Bytes_Transferred_Write":
                text += ".000000"
            ET.SubElement(element, name).text = text

    device = ET.SubElement(root, "SSDDevice")
    config = manifest["ssd_config"]
    time = manifest["time"]
    ET.SubElement(
        device,
        "SSDDevice.Configuration",
        {
            "Simulator_Version": "MQSim-DWPDSim-vNext-1",
            "Statistics_ABI_Version": "1",
            "Configuration_Hash": str(config["mqsim_configuration_hash"]),
            "Configuration_Hash_Algorithm": config[
                "mqsim_configuration_hash_algorithm"
            ],
            "Time_Unit": "nanosecond",
            "Measurement_Start_Time_Ns": str(time["measurement_start_ns"]),
            "Measurement_End_Time_Ns": str(time["measurement_end_ns"]),
        },
    )
    ftl_statistics = {name: 0 for name in FTL_INTEGER_ATTRIBUTES}
    ftl_statistics["Received_Trim_Command_Count"] = sum(
        pool["operations"]["TRIM"] for pool in manifest["pools"].values()
    )
    ftl_statistics["Requested_Trim_Sector_Count"] = sum(
        pool["bytes"]["TRIM"] // 512 for pool in manifest["pools"].values()
    )
    ET.SubElement(
        device,
        "SSDDevice.FTL",
        {name: str(value) for name, value in ftl_statistics.items()},
    )
    for pool_id in ("tlc", "slc"):
        pool = manifest["pools"][pool_id]
        statistics = {name: 0 for name in POOL_INTEGER_ATTRIBUTES}
        statistics.update(
            {
                "Host_Read_Bytes": pool["bytes"]["READ"],
                "Host_Write_Bytes": pool["bytes"]["WRITE"],
                "Requested_Trim_Bytes": pool["bytes"]["TRIM"],
                "Received_Trim_Command_Count": pool["operations"]["TRIM"],
                "Requested_Trim_Sector_Count": pool["bytes"]["TRIM"] // 512,
                "Measurement_Host_Write_Bytes": pool["bytes"]["WRITE"],
                "Measurement_Flash_Programmed_Bytes": pool["bytes"]["WRITE"] * 2,
                "Measurement_Max_Block_Erase_Count": 2,
                "Logical_Capacity_Bytes": pool["logical_capacity_bytes"],
                "Physical_Capacity_Bytes": pool["logical_capacity_bytes"] * 2,
                "PE_Cycle_Limit": 100,
            }
        )
        ET.SubElement(
            device,
            "SSDDevice.Pool",
            {
                "ID": pool_id,
                "Media_Profile_ID": pool["media_profile_id"],
                "Channel_IDs": ",".join(str(value) for value in pool["channel_ids"]),
                **{name: str(value) for name, value in statistics.items()},
            },
        )
    for pool_id, pool in manifest["pools"].items():
        for channel_id in pool["channel_ids"]:
            statistics = {name: 0 for name in CHANNEL_INTEGER_ATTRIBUTES}
            statistics.update(
                {
                    "Host_Read_Bytes": pool["bytes"]["READ"],
                    "Host_Write_Bytes": pool["bytes"]["WRITE"],
                    "Requested_Trim_Bytes": pool["bytes"]["TRIM"],
                    "Requested_Trim_Sector_Count": pool["bytes"]["TRIM"] // 512,
                    "Measurement_Max_Block_Erase_Count": 2,
                    "Measurement_Flash_Programmed_Bytes": pool["bytes"]["WRITE"] * 2,
                    "Logical_Capacity_Bytes": pool["logical_capacity_bytes"],
                    "Physical_Capacity_Bytes": pool["logical_capacity_bytes"] * 2,
                    "PE_Cycle_Limit": 100,
                }
            )
            ET.SubElement(
                device,
                "SSDDevice.Channel",
                {
                    "ID": str(channel_id),
                    "Pool_ID": pool_id,
                    "Media_Profile_ID": pool["media_profile_id"],
                    **{name: str(value) for name, value in statistics.items()},
                },
            )
    ET.ElementTree(root).write(path, encoding="unicode")


def test_result_parser_joins_ids_and_computes_measurement_metrics(tmp_path):
    manifest = convert(
        tmp_path,
        [[0, 0, 1, 0, "WRITE", "SLC", 0, 0, 4096, 1, 1, "MEMORY_DUMP", "", ""]],
    )
    result_path = tmp_path / "result.xml"
    write_mqsim_result(result_path, manifest)
    result = read_mqsim_results(result_path, manifest)

    assert [flow["flow_id"] for flow in result["flows"]] == [0, 1, 2, 3]
    slc = result["pools"][0]
    assert slc["pool_id"] == "slc"
    assert slc["dwpdsim_dump_host_write_bytes"] == 4096
    assert slc["mqsim_host_write_bytes_all_commands"] == 4096
    assert slc["derived"] == {
        "measurement_days": 1.0,
        "host_dwpd": 4096 / (128 * 1024 * 1024),
        "nand_dwpd": 8192 / (128 * 1024 * 1024),
        "write_amplification": 2.0,
        "max_block_pe_per_day": 2.0,
    }
    assert result["pools"][1]["derived"]["write_amplification"] is None
    assert [channel["channel_id"] for channel in result["channels"]] == [0, 1]


def test_result_parser_requires_decimal_integer_fields(tmp_path):
    manifest = convert(
        tmp_path,
        [[0, 0, 1, 0, "WRITE", "SLC", 0, 0, 4096, 1, 1, "MEMORY_DUMP", "", ""]],
    )
    result_path = tmp_path / "result.xml"
    write_mqsim_result(result_path, manifest, decimal_flow_bytes=True)
    with pytest.raises(RuntimeError, match="Bytes_Transferred_Write is not an integer"):
        read_mqsim_results(result_path, manifest)


def test_result_parser_reconciles_trim_counts_and_sectors(tmp_path):
    manifest = convert(
        tmp_path,
        [
            [0, 0, 1, 0, "WRITE", "SLC", 0, 0, 4096, 1, 1, "MEMORY_DUMP", "", ""],
            [1, 1, "", "", "TRIM", "SLC", 0, 0, 4096, 1, 1, "CAPACITY_EVICTION", "", ""],
        ],
    )
    result_path = tmp_path / "result.xml"
    write_mqsim_result(result_path, manifest)
    result = read_mqsim_results(result_path, manifest)
    assert result["ftl"]["Received_Trim_Command_Count"] == 1
    assert result["ftl"]["Requested_Trim_Sector_Count"] == 8
