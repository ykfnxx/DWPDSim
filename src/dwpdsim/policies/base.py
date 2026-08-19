"""Protocols and immutable context objects for replaceable policies."""

from dataclasses import dataclass
from typing import Protocol

from dwpdsim.models import (
    AccessContext,
    CapacitySnapshot,
    StorageAccessResult,
    StorageRequestType,
    StorageTier,
)


class MemCachePolicy(Protocol):
    """Controls DRAM cache metadata, eviction, and downward write-back."""

    def on_hit(self, context: AccessContext) -> None:
        """Observe a cache hit."""

    def on_insert(self, context: AccessContext) -> None:
        """Observe a block insertion."""

    def on_remove(self, block_id: int, context: AccessContext) -> bool:
        """Remove policy metadata and return whether to write the block downward."""

    def choose_victim(self, context: AccessContext) -> int:
        """Return the block ID that should be evicted."""

    def reset(self) -> None:
        """Clear policy history."""


class StorageCachePolicy(Protocol):
    """Controls which TLC block is overwritten when capacity is full."""

    def on_hit(self, context: AccessContext) -> None:
        """Observe a TLC hit."""

    def on_insert(self, context: AccessContext) -> None:
        """Observe a block insertion into TLC."""

    def on_remove(self, block_id: int, context: AccessContext) -> bool:
        """Remove metadata and return whether to write the block to QLC."""

    def choose_overwrite(self, context: AccessContext) -> int:
        """Return the TLC block whose storage position should be overwritten."""

    def reset(self) -> None:
        """Clear policy history."""


class AdmissionPolicy(Protocol):
    """Decides whether a storage-loaded block enters DRAM."""

    def should_admit(
        self,
        context: AccessContext,
        storage_result: StorageAccessResult,
    ) -> bool:
        """Return whether the block should be admitted."""

    def reset(self) -> None:
        """Clear policy history."""


@dataclass(frozen=True, slots=True)
class PlacementContext:
    """Storage state visible to a placement policy."""

    access: AccessContext
    request_type: StorageRequestType
    current_tier: StorageTier | None
    tlc: CapacitySnapshot
    qlc: CapacitySnapshot


@dataclass(frozen=True, slots=True)
class PlacementDecision:
    """Requested destination from a storage placement policy."""

    target_tier: StorageTier
    reason: str


class PlacementPolicy(Protocol):
    """Decides whether an accessed block should reside in TLC or QLC."""

    def decide(self, context: PlacementContext) -> PlacementDecision:
        """Choose the post-access storage tier."""

    def reset(self) -> None:
        """Clear policy history."""
