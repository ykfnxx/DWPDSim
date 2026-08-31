"""Naive in-order garbage collection."""

from dwpdsim.models import BlockId, ChunkView


class NaiveGCPolicy:
    """Relocate live blocks in their original slot order."""

    def relocation_order(self, chunk: ChunkView) -> tuple[BlockId, ...]:
        return tuple(block_id for block_id in chunk.slots if block_id is not None)
