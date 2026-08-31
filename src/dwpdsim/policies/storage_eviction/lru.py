"""Least-recently-used persistent-storage eviction."""

from dwpdsim.models import BlockId, Medium, StorageBlockView


class LRUStorageEvictionPolicy:
    """Evict the least recently accessed candidate."""

    def choose(
        self,
        medium: Medium,
        stream_id: int,
        incoming_block_id: BlockId,
        candidates: tuple[StorageBlockView, ...],
    ) -> BlockId:
        del medium, stream_id, incoming_block_id
        if not candidates:
            raise ValueError("cannot choose a victim from empty storage candidates")
        return min(
            candidates,
            key=lambda block: (
                block.last_access_order,
                block.insert_order,
                block.block_id,
            ),
        ).block_id
