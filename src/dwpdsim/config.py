"""Configuration objects for DWPDSim."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TierConfig:
    """Capacity configuration for one hierarchy tier."""

    capacity_blocks: int

    def __post_init__(self) -> None:
        if self.capacity_blocks <= 0:
            raise ValueError("capacity_blocks must be positive")


@dataclass(frozen=True, slots=True)
class SimulationConfig:
    """Top-level capacity and block-size configuration."""

    block_size_bytes: int
    dram: TierConfig
    tlc: TierConfig
    qlc: TierConfig

    def __post_init__(self) -> None:
        if self.block_size_bytes <= 0:
            raise ValueError("block_size_bytes must be positive")
