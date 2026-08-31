"""DRAM residency manager."""

from types import MappingProxyType

from dwpdsim.errors import InvalidPolicyDecisionError
from dwpdsim.models import AccessContext, BlockId, DramBlockView, DramView
from dwpdsim.policies.dram import DramPolicy, LRUDramPolicy


class DRAMManager:
    """Own DRAM state and ask one policy for admission and victims."""

    def __init__(self, capacity_blocks: int, policy: DramPolicy | None = None) -> None:
        self.capacity_blocks = capacity_blocks
        self.policy = policy if policy is not None else LRUDramPolicy()
        self._blocks: dict[BlockId, DramBlockView] = {}
        self._order = 0

    @property
    def resident_blocks(self) -> frozenset[BlockId]:
        return frozenset(self._blocks)

    @property
    def is_full(self) -> bool:
        return len(self._blocks) >= self.capacity_blocks

    def view(self) -> DramView:
        return DramView(
            capacity_blocks=self.capacity_blocks,
            blocks=MappingProxyType(self._blocks),
        )

    def contains(self, block_id: BlockId) -> bool:
        return block_id in self._blocks

    def access(self, block_id: BlockId) -> bool:
        entry = self._blocks.pop(block_id, None)
        if entry is None:
            return False
        self._order += 1
        self._blocks[block_id] = DramBlockView(
            block_id=block_id,
            insert_order=entry.insert_order,
            last_access_order=self._order,
        )
        return True

    def should_admit(self, context: AccessContext) -> bool:
        decision = self.policy.should_admit(context, self.view())
        if not isinstance(decision, bool):
            raise InvalidPolicyDecisionError("DramPolicy.should_admit() must return bool")
        return decision

    def victim_for(self, context: AccessContext) -> BlockId | None:
        if not self.is_full:
            return None
        victim = self.policy.choose_victim(context, self.view())
        if victim not in self._blocks:
            raise InvalidPolicyDecisionError(f"DramPolicy selected non-resident block: {victim!r}")
        return victim

    def insert(
        self,
        block_id: BlockId,
        victim: BlockId | None = None,
    ) -> None:
        if block_id in self._blocks:
            raise ValueError(f"block already exists in DRAM: {block_id}")
        if self.is_full and victim is None:
            raise InvalidPolicyDecisionError("full DRAM insertion requires a victim")
        if not self.is_full and victim is not None:
            raise InvalidPolicyDecisionError("DRAM victim is only valid when DRAM is full")
        if victim is not None and victim not in self._blocks:
            raise InvalidPolicyDecisionError(f"DRAM victim is not resident: {victim!r}")
        if victim is not None:
            del self._blocks[victim]
        self._order += 1
        self._blocks[block_id] = DramBlockView(
            block_id=block_id,
            insert_order=self._order,
            last_access_order=self._order,
        )
