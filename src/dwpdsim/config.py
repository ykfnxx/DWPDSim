"""Simulation configuration and validation."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SSDConfig:
    """Capacity and write-layout configuration for one SSD medium."""

    capacity_bytes: int
    chunk_size_bytes: int
    stream_count: int
    gc_reserve_chunks: int = 1

    def __post_init__(self) -> None:
        if self.capacity_bytes <= 0:
            raise ValueError("capacity_bytes must be positive")
        if self.chunk_size_bytes <= 0:
            raise ValueError("chunk_size_bytes must be positive")
        if self.stream_count <= 0:
            raise ValueError("stream_count must be positive")
        if self.gc_reserve_chunks < 1:
            raise ValueError("gc_reserve_chunks must be at least 1")
        if self.capacity_bytes % self.chunk_size_bytes != 0:
            raise ValueError("capacity_bytes must be divisible by chunk_size_bytes")
        if self.chunk_count < self.stream_count + self.gc_reserve_chunks:
            raise ValueError("chunk_count must be at least stream_count + gc_reserve_chunks")

    @property
    def chunk_count(self) -> int:
        """Return the number of chunks in this SSD."""

        return self.capacity_bytes // self.chunk_size_bytes


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    """Top-level DRAM, SLC, and TLC simulation configuration."""

    block_size_bytes: int
    dram_capacity_bytes: int
    slc: SSDConfig
    tlc: SSDConfig

    def __post_init__(self) -> None:
        if self.block_size_bytes <= 0:
            raise ValueError("block_size_bytes must be positive")
        if self.dram_capacity_bytes <= 0:
            raise ValueError("dram_capacity_bytes must be positive")
        if self.dram_capacity_bytes % self.block_size_bytes != 0:
            raise ValueError("dram_capacity_bytes must be divisible by block_size_bytes")

        for name, ssd in (("slc", self.slc), ("tlc", self.tlc)):
            if ssd.chunk_size_bytes % self.block_size_bytes != 0:
                raise ValueError(f"{name}.chunk_size_bytes must be divisible by block_size_bytes")

    @property
    def dram_capacity_blocks(self) -> int:
        """Return DRAM capacity measured in blocks."""

        return self.dram_capacity_bytes // self.block_size_bytes
