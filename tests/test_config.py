import pytest

from dwpdsim import Query, SimulationConfig, SSDConfig


def test_config_and_query_use_explicit_bytes_and_ordered_hashes():
    ssd = SSDConfig(
        capacity_bytes=64,
        chunk_size_bytes=16,
        stream_count=2,
        gc_reserve_chunks=1,
    )
    config = SimulationConfig(
        block_size_bytes=4,
        dram_capacity_bytes=12,
        slc=ssd,
        tlc=ssd,
    )
    query = Query(timestamp=1.5, hash_ids=[7, 7, 8], other_info={"oracle": True})

    assert config.dram_capacity_blocks == 3
    assert ssd.chunk_count == 4
    assert query.hash_ids == (7, 7, 8)


@pytest.mark.parametrize(
    "build",
    [
        lambda: SSDConfig(0, 8, 1),
        lambda: SSDConfig(16, 6, 1),
        lambda: SSDConfig(16, 8, 0),
        lambda: SSDConfig(16, 8, 1, 0),
        lambda: SSDConfig(16, 8, 2, 1),
        lambda: SimulationConfig(4, 6, SSDConfig(24, 8, 1), SSDConfig(24, 8, 1)),
        lambda: SimulationConfig(4, 8, SSDConfig(24, 6, 1), SSDConfig(24, 6, 1)),
    ],
)
def test_config_rejects_invalid_layouts(build):
    with pytest.raises(ValueError):
        build()
