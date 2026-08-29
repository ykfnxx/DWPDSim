"""Small component interfaces used to keep managers loosely coupled."""

from typing import Protocol

from dwpdsim.models import (
    AccessContext,
    BlockId,
    MemAdmissionResult,
    Query,
    StorageAccessResult,
    StorageWriteResult,
)


class BlockStorage(Protocol):
    """A lower hierarchy level capable of loading and writing one block."""

    def contains_block(self, block_id: BlockId) -> bool:
        """Return whether a block exists in persistent storage."""

    def load_block(self, context: AccessContext) -> StorageAccessResult:
        """Load a block and return all storage-side effects."""

    def write_block(self, context: AccessContext) -> StorageWriteResult:
        """Write an evicted block and return all storage-side effects."""


class AggregateMetricsSink(Protocol):
    """Receives query outcomes without retaining detailed result trees."""

    def record_query_start(self, query: Query) -> None:
        """Record the start of one query."""

    def record_memory_hit(self) -> None:
        """Record one DRAM hit."""

    def record_memory_miss(
        self,
        storage_result: StorageAccessResult | None,
        admission_result: MemAdmissionResult | None,
        *,
        inserted_on_storage_miss: bool = False,
    ) -> None:
        """Record one DRAM miss, including a storage-miss insertion."""


BlockLoader = BlockStorage
