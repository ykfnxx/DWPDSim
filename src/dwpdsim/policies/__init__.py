"""Built-in replaceable policies."""

from dwpdsim.policies.base import (
    AdmissionPolicy,
    MemCachePolicy,
    PlacementContext,
    PlacementDecision,
    PlacementPolicy,
    StorageCachePolicy,
)
from dwpdsim.policies.cache import FIFOPolicy, LRUPolicy
from dwpdsim.policies.memory import AlwaysAdmitPolicy
from dwpdsim.policies.storage import (
    AlwaysQLCPolicy,
    AlwaysTLCPolicy,
    FrequencyPlacementPolicy,
)

__all__ = [
    "AdmissionPolicy",
    "AlwaysAdmitPolicy",
    "AlwaysQLCPolicy",
    "AlwaysTLCPolicy",
    "FIFOPolicy",
    "FrequencyPlacementPolicy",
    "LRUPolicy",
    "MemCachePolicy",
    "PlacementContext",
    "PlacementDecision",
    "PlacementPolicy",
    "StorageCachePolicy",
]
