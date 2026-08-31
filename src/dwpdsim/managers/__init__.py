"""State-owning managers."""

from dwpdsim.managers.dram import DRAMManager
from dwpdsim.managers.ssd import BlockLocation, SSDManager
from dwpdsim.managers.storage import StorageManager

__all__ = ["BlockLocation", "DRAMManager", "SSDManager", "StorageManager"]
