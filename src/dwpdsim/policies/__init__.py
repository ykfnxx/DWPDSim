"""Built-in replaceable policies."""

from dwpdsim.policies.dram import DramPolicy, LRUDramPolicy
from dwpdsim.policies.gc import GCPolicy, NaiveGCPolicy
from dwpdsim.policies.placement import (
    DropPlacementPolicy,
    FixedPlacementPolicy,
    PlacementPolicy,
)
from dwpdsim.policies.storage_eviction import (
    LRUStorageEvictionPolicy,
    StorageEvictionPolicy,
)

__all__ = [
    "DramPolicy",
    "DropPlacementPolicy",
    "FixedPlacementPolicy",
    "GCPolicy",
    "LRUDramPolicy",
    "LRUStorageEvictionPolicy",
    "NaiveGCPolicy",
    "PlacementPolicy",
    "StorageEvictionPolicy",
]
