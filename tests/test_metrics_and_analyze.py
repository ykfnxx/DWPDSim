import json

import pytest

from dwpdsim import DWPDSimulator, Medium, Query
from dwpdsim.policies import FixedPlacementPolicy
from scripts.analyze import analyze


def test_raw_stats_and_wa_analysis_are_separate(config_factory, tmp_path):
    config = config_factory(dram_blocks=1)
    simulator = DWPDSimulator.from_config(
        config,
        placement_policy=FixedPlacementPolicy(Medium.SLC, 0),
    )
    simulator.process_query(Query(0, (1,)))
    simulator.process_query(Query(86_400, (2,)))
    simulator.storage.transfer(1, Medium.TLC, 0)

    stats_path = tmp_path / "simulation_stats.json"
    simulator.write_stats(stats_path)
    stats = json.loads(stats_path.read_text())

    assert stats["time"]["duration_seconds"] == 86_400
    assert stats["accesses"]["global_misses"] == 2
    assert stats["created"] == {"blocks": 2, "bytes": 8}
    assert stats["writes_from_dram"]["slc"]["bytes"] == 4
    assert stats["transfers"]["slc_to_tlc"]["bytes"] == 4
    assert stats["stream_writes"]["slc"]["0"]["blocks"] == 1
    assert stats["stream_writes"]["tlc"]["0"]["blocks"] == 1

    result = analyze(stats, slc_wa=2, tlc_wa=3)
    assert result["system_input_write_bytes"] == 4
    assert result["logical_write_bytes"] == {"slc": 4, "tlc": 4}
    assert result["estimated_physical_write_bytes"] == {"slc": 8, "tlc": 12}
    assert result["dwpd"] == {
        "system_equivalent": 4 / 64,
        "slc": 8 / 32,
        "tlc": 12 / 32,
    }
    assert analyze(stats, 1, 1)["estimated_physical_write_bytes"] == {
        "slc": 4,
        "tlc": 4,
    }


@pytest.mark.parametrize(
    "slc_wa,tlc_wa,duration,slc_capacity,tlc_capacity",
    [
        (0.9, 1, 1, 1, 1),
        (1, 0.9, 1, 1, 1),
        (1, 1, 0, 1, 1),
        (1, 1, 1, 0, 1),
        (1, 1, 1, 1, 0),
    ],
)
def test_analysis_rejects_invalid_parameters(
    slc_wa,
    tlc_wa,
    duration,
    slc_capacity,
    tlc_capacity,
):
    stats = {
        "time": {"duration_seconds": duration},
        "configuration": {
            "slc_capacity_bytes": slc_capacity,
            "tlc_capacity_bytes": tlc_capacity,
        },
        "writes_from_dram": {"slc": {"bytes": 0}, "tlc": {"bytes": 0}},
        "transfers": {
            "slc_to_tlc": {"bytes": 0},
            "tlc_to_slc": {"bytes": 0},
        },
    }
    with pytest.raises(ValueError):
        analyze(stats, slc_wa, tlc_wa)
