"""SSD garbage-collection policies."""

from typing import Protocol

from dwpdsim.models import BlockId, ChunkView
from dwpdsim.policies.gc.naive import NaiveGCPolicy


class GCPolicy(Protocol):
    """Choose the order for relocating live blocks in one chunk."""

    def relocation_order(self, chunk: ChunkView) -> tuple[BlockId, ...]:
        """Return live block ids in relocation order."""


__all__ = ["GCPolicy", "NaiveGCPolicy"]
