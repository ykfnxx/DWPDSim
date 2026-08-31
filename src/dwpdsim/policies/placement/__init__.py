"""Persistent placement policies."""

from typing import Protocol

from dwpdsim.models import Placement, PlacementContext, StorageView
from dwpdsim.policies.placement.drop import DropPlacementPolicy
from dwpdsim.policies.placement.fixed import FixedPlacementPolicy


class PlacementPolicy(Protocol):
    """Choose an SLC/TLC stream or drop a DRAM-evicted block."""

    def choose(
        self,
        context: PlacementContext,
        storage: StorageView,
    ) -> Placement | None:
        """Return a destination, or ``None`` to drop the block."""


__all__ = ["DropPlacementPolicy", "FixedPlacementPolicy", "PlacementPolicy"]
