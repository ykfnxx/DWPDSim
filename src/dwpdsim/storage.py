"""Exclusive TLC/QLC storage manager and physical I/O generation."""

from collections.abc import Iterable

from dwpdsim.config import TierConfig
from dwpdsim.errors import (
    BlockNotFoundError,
    InvalidPolicyDecisionError,
    StorageCapacityError,
)
from dwpdsim.models import (
    AccessContext,
    BlockId,
    CapacitySnapshot,
    IOEvent,
    IOOperation,
    IOReason,
    StorageAccessResult,
    StorageRequestType,
    StorageTier,
    StorageWriteResult,
)
from dwpdsim.policies.base import (
    PlacementContext,
    PlacementPolicy,
    StorageCachePolicy,
)
from dwpdsim.policies.cache import LRUPolicy
from dwpdsim.policies.storage import AlwaysTLCPolicy


class StorageManager:
    """Owns mutually exclusive TLC/QLC placement and enforces capacities."""

    def __init__(
        self,
        tlc_config: TierConfig,
        qlc_config: TierConfig,
        placement_policy: PlacementPolicy | None = None,
        storage_cache_policy: StorageCachePolicy | None = None,
        initial_blocks: Iterable[BlockId] = (),
        initial_tier: StorageTier = StorageTier.QLC,
    ) -> None:
        self._tlc_config = tlc_config
        self._qlc_config = qlc_config
        self._placement_policy = (
            placement_policy if placement_policy is not None else AlwaysTLCPolicy()
        )
        self._cache_policy = (
            storage_cache_policy if storage_cache_policy is not None else LRUPolicy()
        )
        self._locations: dict[BlockId, StorageTier] = {}
        self._tlc_blocks: set[BlockId] = set()
        self._qlc_blocks: set[BlockId] = set()
        self._tlc_peak_used_blocks = 0
        self._qlc_peak_used_blocks = 0
        self._tlc_eviction_count = 0
        self._placement_rejected_count = 0
        self.seed_blocks(initial_blocks, tier=initial_tier)

    @property
    def tlc_capacity(self) -> CapacitySnapshot:
        return CapacitySnapshot(
            capacity_blocks=self._tlc_config.capacity_blocks,
            used_blocks=len(self._tlc_blocks),
        )

    @property
    def qlc_capacity(self) -> CapacitySnapshot:
        return CapacitySnapshot(
            capacity_blocks=self._qlc_config.capacity_blocks,
            used_blocks=len(self._qlc_blocks),
        )

    @property
    def tlc_peak_used_blocks(self) -> int:
        return self._tlc_peak_used_blocks

    @property
    def qlc_peak_used_blocks(self) -> int:
        return self._qlc_peak_used_blocks

    @property
    def tlc_eviction_count(self) -> int:
        return self._tlc_eviction_count

    @property
    def placement_rejected_count(self) -> int:
        return self._placement_rejected_count

    def blocks_in_tier(self, tier: StorageTier) -> frozenset[BlockId]:
        """Return a read-only snapshot of blocks in one storage tier."""

        return frozenset(self._blocks_for_tier(tier))

    def tier_of(self, block_id: BlockId) -> StorageTier:
        """Return the current storage tier for a block."""

        try:
            return self._locations[block_id]
        except KeyError as error:
            raise BlockNotFoundError(f"block not found in storage: {block_id}") from error

    def seed_block(self, block_id: BlockId, tier: StorageTier = StorageTier.QLC) -> None:
        """Place one initial block without generating simulated I/O."""

        self.seed_blocks((block_id,), tier=tier)

    def seed_blocks(
        self,
        block_ids: Iterable[BlockId],
        tier: StorageTier = StorageTier.QLC,
    ) -> None:
        """Atomically place initial blocks without generating simulated I/O."""

        self._validate_tier(tier)
        blocks_to_seed = tuple(block_ids)
        if len(set(blocks_to_seed)) != len(blocks_to_seed):
            raise ValueError("seed block IDs must be unique")

        existing_blocks = [block_id for block_id in blocks_to_seed if block_id in self._locations]
        if existing_blocks:
            raise ValueError(f"blocks already exist in storage: {existing_blocks}")

        destination = self._blocks_for_tier(tier)
        capacity = self._capacity_for_tier(tier)
        if len(destination) + len(blocks_to_seed) > capacity.capacity_blocks:
            raise StorageCapacityError(
                f"cannot seed {len(blocks_to_seed)} blocks into {tier.value}: "
                f"{capacity.free_blocks} free"
            )

        for block_id in blocks_to_seed:
            destination.add(block_id)
            self._locations[block_id] = tier
            if tier is StorageTier.TLC:
                self._cache_policy.on_insert(AccessContext.for_block(block_id))
        self._update_peaks()

    def load_block(self, context: AccessContext) -> StorageAccessResult:
        """Read one block and apply the configured post-read placement policy."""

        source_tier = self.tier_of(context.block_id)
        if source_tier is StorageTier.TLC:
            self._cache_policy.on_hit(context)

        io_events = [
            IOEvent(
                tier=source_tier,
                operation=IOOperation.READ,
                reason=IOReason.DEMAND,
                block_id=context.block_id,
                timestamp=context.timestamp,
            )
        ]
        placement_context = PlacementContext(
            access=context,
            request_type=StorageRequestType.READ,
            current_tier=source_tier,
            tlc=self.tlc_capacity,
            qlc=self.qlc_capacity,
        )
        decision = self._placement_policy.decide(placement_context)
        self._validate_tier(decision.target_tier)

        placement_rejected = False
        if decision.target_tier is not source_tier:
            if decision.target_tier is StorageTier.TLC:
                io_events.extend(self._promote_to_tlc(context))
            else:
                demotion_events, placement_rejected = self._demote_to_qlc(context)
                io_events.extend(demotion_events)

        final_tier = self._locations[context.block_id]
        return StorageAccessResult(
            block_id=context.block_id,
            source_tier=source_tier,
            requested_tier=decision.target_tier,
            final_tier=final_tier,
            decision_reason=decision.reason,
            placement_rejected=placement_rejected,
            io_events=tuple(io_events),
        )

    def write_block(self, context: AccessContext) -> StorageWriteResult:
        """Write one DRAM-evicted block using the configured placement policy."""

        source_tier = self._locations.get(context.block_id)
        placement_context = PlacementContext(
            access=context,
            request_type=StorageRequestType.WRITEBACK,
            current_tier=source_tier,
            tlc=self.tlc_capacity,
            qlc=self.qlc_capacity,
        )
        decision = self._placement_policy.decide(placement_context)
        self._validate_tier(decision.target_tier)

        if decision.target_tier is StorageTier.TLC:
            io_events, placement_rejected = self._write_to_tlc(context, source_tier)
        else:
            io_events, placement_rejected = self._write_to_qlc(context, source_tier)

        return StorageWriteResult(
            block_id=context.block_id,
            source_tier=source_tier,
            requested_tier=decision.target_tier,
            final_tier=self._locations[context.block_id],
            decision_reason=decision.reason,
            placement_rejected=placement_rejected,
            io_events=tuple(io_events),
        )

    def _promote_to_tlc(self, context: AccessContext) -> list[IOEvent]:
        block_id = context.block_id
        io_events: list[IOEvent] = []

        if self.tlc_capacity.is_full:
            overwrite_block = self._cache_policy.choose_overwrite(context)
            self._validate_tlc_overwrite(overwrite_block)
            self._tlc_blocks.remove(overwrite_block)
            should_write_back = self._remove_from_tlc_policy(overwrite_block, context)
            self._tlc_eviction_count += 1
            if should_write_back:
                self._qlc_blocks.add(overwrite_block)
                self._locations[overwrite_block] = StorageTier.QLC
                io_events.extend(
                    (
                        IOEvent(
                            tier=StorageTier.TLC,
                            operation=IOOperation.READ,
                            reason=IOReason.DEMOTION,
                            block_id=overwrite_block,
                            timestamp=context.timestamp,
                        ),
                        IOEvent(
                            tier=StorageTier.QLC,
                            operation=IOOperation.WRITE,
                            reason=IOReason.DEMOTION,
                            block_id=overwrite_block,
                            timestamp=context.timestamp,
                        ),
                    )
                )
            else:
                del self._locations[overwrite_block]

        self._qlc_blocks.remove(block_id)
        self._tlc_blocks.add(block_id)
        self._locations[block_id] = StorageTier.TLC
        self._cache_policy.on_insert(context)
        io_events.append(
            IOEvent(
                tier=StorageTier.TLC,
                operation=IOOperation.WRITE,
                reason=IOReason.PROMOTION,
                block_id=block_id,
                timestamp=context.timestamp,
            )
        )
        self._update_peaks()
        return io_events

    def _demote_to_qlc(self, context: AccessContext) -> tuple[list[IOEvent], bool]:
        if self.qlc_capacity.is_full:
            self._placement_rejected_count += 1
            return [], True

        block_id = context.block_id
        self._tlc_blocks.remove(block_id)
        self._remove_from_tlc_policy(block_id, context)
        self._qlc_blocks.add(block_id)
        self._locations[block_id] = StorageTier.QLC
        self._update_peaks()
        return [
            IOEvent(
                tier=StorageTier.QLC,
                operation=IOOperation.WRITE,
                reason=IOReason.DEMOTION,
                block_id=block_id,
                timestamp=context.timestamp,
            )
        ], False

    def _write_to_tlc(
        self,
        context: AccessContext,
        source_tier: StorageTier | None,
    ) -> tuple[list[IOEvent], bool]:
        block_id = context.block_id
        if source_tier is StorageTier.TLC:
            self._cache_policy.on_hit(context)
            return [self._writeback_event(StorageTier.TLC, context)], False

        io_events: list[IOEvent] = []
        if self.tlc_capacity.is_full:
            overwrite_block = self._cache_policy.choose_overwrite(context)
            self._validate_tlc_overwrite(overwrite_block)
            self._tlc_blocks.remove(overwrite_block)
            should_write_back = self._remove_from_tlc_policy(overwrite_block, context)
            self._tlc_eviction_count += 1
            if should_write_back:
                qlc_used_after_move = len(self._qlc_blocks) - int(source_tier is StorageTier.QLC)
                if qlc_used_after_move >= self._qlc_config.capacity_blocks:
                    raise StorageCapacityError(
                        "cannot write back overwritten TLC block: QLC has no free capacity"
                    )
                self._qlc_blocks.add(overwrite_block)
                self._locations[overwrite_block] = StorageTier.QLC
                io_events.extend(
                    (
                        IOEvent(
                            tier=StorageTier.TLC,
                            operation=IOOperation.READ,
                            reason=IOReason.DEMOTION,
                            block_id=overwrite_block,
                            timestamp=context.timestamp,
                        ),
                        IOEvent(
                            tier=StorageTier.QLC,
                            operation=IOOperation.WRITE,
                            reason=IOReason.DEMOTION,
                            block_id=overwrite_block,
                            timestamp=context.timestamp,
                        ),
                    )
                )
            else:
                del self._locations[overwrite_block]

        if source_tier is StorageTier.QLC:
            self._qlc_blocks.remove(block_id)
        self._tlc_blocks.add(block_id)
        self._locations[block_id] = StorageTier.TLC
        self._cache_policy.on_insert(context)
        io_events.append(self._writeback_event(StorageTier.TLC, context))
        self._update_peaks()
        return io_events, False

    def _write_to_qlc(
        self,
        context: AccessContext,
        source_tier: StorageTier | None,
    ) -> tuple[list[IOEvent], bool]:
        block_id = context.block_id
        if source_tier is StorageTier.QLC:
            return [self._writeback_event(StorageTier.QLC, context)], False

        if self.qlc_capacity.is_full:
            if source_tier is StorageTier.TLC:
                self._placement_rejected_count += 1
                self._cache_policy.on_hit(context)
                return [self._writeback_event(StorageTier.TLC, context)], True
            raise StorageCapacityError("cannot write new block: QLC has no free capacity")

        if source_tier is StorageTier.TLC:
            self._tlc_blocks.remove(block_id)
            self._remove_from_tlc_policy(block_id, context)
        self._qlc_blocks.add(block_id)
        self._locations[block_id] = StorageTier.QLC
        self._update_peaks()
        return [self._writeback_event(StorageTier.QLC, context)], False

    def _remove_from_tlc_policy(
        self,
        block_id: BlockId,
        context: AccessContext,
    ) -> bool:
        should_write_back = self._cache_policy.on_remove(block_id, context)
        if not isinstance(should_write_back, bool):
            raise InvalidPolicyDecisionError("StorageCachePolicy.on_remove() must return bool")
        return should_write_back

    @staticmethod
    def _writeback_event(tier: StorageTier, context: AccessContext) -> IOEvent:
        return IOEvent(
            tier=tier,
            operation=IOOperation.WRITE,
            reason=IOReason.WRITEBACK,
            block_id=context.block_id,
            timestamp=context.timestamp,
        )

    def _validate_tlc_overwrite(self, block_id: BlockId) -> None:
        if block_id not in self._tlc_blocks:
            raise InvalidPolicyDecisionError(
                f"StorageCachePolicy selected non-resident TLC block: {block_id}"
            )

    @staticmethod
    def _validate_tier(tier: StorageTier) -> None:
        if not isinstance(tier, StorageTier):
            raise InvalidPolicyDecisionError(f"invalid storage tier: {tier!r}")

    def _blocks_for_tier(self, tier: StorageTier) -> set[BlockId]:
        self._validate_tier(tier)
        return self._tlc_blocks if tier is StorageTier.TLC else self._qlc_blocks

    def _capacity_for_tier(self, tier: StorageTier) -> CapacitySnapshot:
        self._validate_tier(tier)
        return self.tlc_capacity if tier is StorageTier.TLC else self.qlc_capacity

    def _update_peaks(self) -> None:
        self._tlc_peak_used_blocks = max(
            self._tlc_peak_used_blocks,
            len(self._tlc_blocks),
        )
        self._qlc_peak_used_blocks = max(
            self._qlc_peak_used_blocks,
            len(self._qlc_blocks),
        )
