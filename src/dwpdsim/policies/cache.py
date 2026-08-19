"""Cache-ordering algorithms usable through memory or storage policy protocols."""

from collections import OrderedDict

from dwpdsim.models import AccessContext, BlockId


class LRUPolicy:
    """Select the least recently accessed resident block."""

    def __init__(self, write_back_on_remove: bool = True) -> None:
        self._order: OrderedDict[BlockId, None] = OrderedDict()
        self._write_back_on_remove = write_back_on_remove

    def on_hit(self, context: AccessContext) -> None:
        self._order.move_to_end(context.block_id)

    def on_insert(self, context: AccessContext) -> None:
        self._order[context.block_id] = None
        self._order.move_to_end(context.block_id)

    def on_remove(self, block_id: BlockId, context: AccessContext) -> bool:
        del context
        self._order.pop(block_id, None)
        return self._write_back_on_remove

    def choose_victim(self, context: AccessContext) -> BlockId:
        del context
        if not self._order:
            raise RuntimeError("cannot choose a victim from an empty cache")
        return next(iter(self._order))

    def choose_overwrite(self, context: AccessContext) -> BlockId:
        return self.choose_victim(context)

    def reset(self) -> None:
        self._order.clear()


class FIFOPolicy:
    """Select the earliest inserted resident block."""

    def __init__(self, write_back_on_remove: bool = True) -> None:
        self._order: OrderedDict[BlockId, None] = OrderedDict()
        self._write_back_on_remove = write_back_on_remove

    def on_hit(self, context: AccessContext) -> None:
        del context

    def on_insert(self, context: AccessContext) -> None:
        self._order[context.block_id] = None

    def on_remove(self, block_id: BlockId, context: AccessContext) -> bool:
        del context
        self._order.pop(block_id, None)
        return self._write_back_on_remove

    def choose_victim(self, context: AccessContext) -> BlockId:
        del context
        if not self._order:
            raise RuntimeError("cannot choose a victim from an empty cache")
        return next(iter(self._order))

    def choose_overwrite(self, context: AccessContext) -> BlockId:
        return self.choose_victim(context)

    def reset(self) -> None:
        self._order.clear()
