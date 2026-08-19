"""Built-in TLC/QLC placement policies."""

from collections import Counter

from dwpdsim.models import BlockId, StorageTier
from dwpdsim.policies.base import PlacementContext, PlacementDecision


class AlwaysTLCPolicy:
    """Request TLC placement for every accessed block."""

    def decide(self, context: PlacementContext) -> PlacementDecision:
        del context
        return PlacementDecision(target_tier=StorageTier.TLC, reason="always_tlc")

    def reset(self) -> None:
        return None


class AlwaysQLCPolicy:
    """Request QLC placement for every accessed block."""

    def decide(self, context: PlacementContext) -> PlacementDecision:
        del context
        return PlacementDecision(target_tier=StorageTier.QLC, reason="always_qlc")

    def reset(self) -> None:
        return None


class FrequencyPlacementPolicy:
    """Place blocks in TLC once their access count reaches a threshold."""

    def __init__(self, tlc_threshold: int) -> None:
        if tlc_threshold <= 0:
            raise ValueError("tlc_threshold must be positive")
        self._tlc_threshold = tlc_threshold
        self._frequencies: Counter[BlockId] = Counter()

    def decide(self, context: PlacementContext) -> PlacementDecision:
        block_id = context.access.block_id
        self._frequencies[block_id] += 1
        frequency = self._frequencies[block_id]
        target_tier = StorageTier.TLC if frequency >= self._tlc_threshold else StorageTier.QLC
        return PlacementDecision(
            target_tier=target_tier,
            reason=f"frequency={frequency}",
        )

    def reset(self) -> None:
        self._frequencies.clear()
