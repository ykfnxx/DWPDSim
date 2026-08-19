"""Small component interfaces used to keep managers loosely coupled."""

from typing import Protocol

from dwpdsim.models import (
    AccessContext,
    MemAdmissionResult,
    Query,
    StorageAccessResult,
    StorageWriteResult,
)


class BlockStorage(Protocol):
    """A lower hierarchy level capable of loading and writing one block."""

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
        storage_result: StorageAccessResult,
        admission_result: MemAdmissionResult | None,
    ) -> None:
        """Record one DRAM miss and its lower-level side effects."""


BlockLoader = BlockStorage
