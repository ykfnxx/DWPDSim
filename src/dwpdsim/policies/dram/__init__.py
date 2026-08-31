"""DRAM admission and eviction policies."""

from typing import Protocol

from dwpdsim.models import AccessContext, BlockId, DramView
from dwpdsim.policies.dram.lru import LRUDramPolicy


class DramPolicy(Protocol):
    """Choose DRAM admission and eviction without mutating manager state."""

    def should_admit(self, context: AccessContext, dram: DramView) -> bool:
        """Return whether a storage hit should enter DRAM."""

    def choose_victim(self, context: AccessContext, dram: DramView) -> BlockId:
        """Choose one resident block to evict."""


__all__ = ["DramPolicy", "LRUDramPolicy"]
