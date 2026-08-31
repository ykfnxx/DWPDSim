"""Least-recently-used DRAM policy."""

from dwpdsim.models import AccessContext, BlockId, DramView


class LRUDramPolicy:
    """Admit storage hits and evict the least recently accessed block."""

    def should_admit(self, context: AccessContext, dram: DramView) -> bool:
        del context, dram
        return True

    def choose_victim(self, context: AccessContext, dram: DramView) -> BlockId:
        del context
        if not dram.blocks:
            raise ValueError("cannot choose a victim from empty DRAM")
        return next(iter(dram.blocks))
