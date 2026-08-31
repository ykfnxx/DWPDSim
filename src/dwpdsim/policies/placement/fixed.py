"""Fixed-medium placement policy."""

from dataclasses import dataclass

from dwpdsim.models import Medium, Placement, PlacementContext, StorageView


@dataclass(frozen=True, slots=True)
class FixedPlacementPolicy:
    """Always choose the configured medium and stream."""

    medium: Medium
    stream_id: int

    def choose(self, context: PlacementContext, storage: StorageView) -> Placement:
        del context, storage
        return Placement(medium=self.medium, stream_id=self.stream_id)
