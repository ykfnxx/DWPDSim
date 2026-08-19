"""Small component interfaces used to keep managers loosely coupled."""

from typing import Protocol

from dwpdsim.models import AccessContext, StorageAccessResult, StorageWriteResult


class BlockStorage(Protocol):
    """A lower hierarchy level capable of loading and writing one block."""

    def load_block(self, context: AccessContext) -> StorageAccessResult:
        """Load a block and return all storage-side effects."""

    def write_block(self, context: AccessContext) -> StorageWriteResult:
        """Write an evicted block and return all storage-side effects."""


BlockLoader = BlockStorage
