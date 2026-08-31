"""Placement policy that drops evicted blocks."""

from dwpdsim.models import PlacementContext, StorageView


class DropPlacementPolicy:
    """Drop every DRAM-evicted block without persistent storage."""

    def choose(self, context: PlacementContext, storage: StorageView) -> None:
        del context, storage
