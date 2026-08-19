"""Built-in DRAM admission policies."""

from dwpdsim.models import AccessContext, StorageAccessResult


class AlwaysAdmitPolicy:
    """Admit every block loaded from lower storage."""

    def should_admit(
        self,
        context: AccessContext,
        storage_result: StorageAccessResult,
    ) -> bool:
        del context, storage_result
        return True

    def reset(self) -> None:
        return None
