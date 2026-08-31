"""Persistent-storage eviction policies."""

from typing import Protocol

from dwpdsim.models import BlockId, Medium, StorageBlockView
from dwpdsim.policies.storage_eviction.lru import LRUStorageEvictionPolicy


class StorageEvictionPolicy(Protocol):
    """Choose one persistent block to delete."""

    def choose(
        self,
        medium: Medium,
        stream_id: int,
        incoming_block_id: BlockId,
        candidates: tuple[StorageBlockView, ...],
    ) -> BlockId:
        """Return one block id from the supplied candidates."""


__all__ = ["LRUStorageEvictionPolicy", "StorageEvictionPolicy"]
