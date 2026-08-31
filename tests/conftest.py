import pytest

from dwpdsim import SimulationConfig, SSDConfig


@pytest.fixture
def config_factory():
    def make(
        *,
        dram_blocks: int = 2,
        chunks: int = 4,
        blocks_per_chunk: int = 2,
        streams: int = 1,
    ) -> SimulationConfig:
        block_size = 4
        ssd = SSDConfig(
            capacity_bytes=chunks * blocks_per_chunk * block_size,
            chunk_size_bytes=blocks_per_chunk * block_size,
            stream_count=streams,
            gc_reserve_chunks=1,
        )
        return SimulationConfig(
            block_size_bytes=block_size,
            dram_capacity_bytes=dram_blocks * block_size,
            slc=ssd,
            tlc=ssd,
        )

    return make
