import pytest

from dwpdsim import StorageTier, TierConfig
from dwpdsim.errors import BlockNotFoundError, StorageCapacityError
from dwpdsim.models import AccessContext, IOOperation, IOReason
from dwpdsim.policies import AlwaysQLCPolicy, AlwaysTLCPolicy, LRUPolicy
from dwpdsim.storage import StorageManager


class HighestBlockIdStorageCachePolicy:
    """Storage-only policy used to prove the MemCachePolicy is not required."""

    def __init__(self) -> None:
        self._blocks: set[int] = set()

    def on_hit(self, context: AccessContext) -> None:
        del context

    def on_insert(self, context: AccessContext) -> None:
        self._blocks.add(context.block_id)

    def on_remove(self, block_id: int, context: AccessContext) -> bool:
        del context
        self._blocks.remove(block_id)
        return True

    def choose_overwrite(self, context: AccessContext) -> int:
        del context
        return max(self._blocks)

    def reset(self) -> None:
        self._blocks.clear()


def test_tlc_promotion_demotes_policy_selected_overwrite_when_full() -> None:
    storage = StorageManager(
        tlc_config=TierConfig(1),
        qlc_config=TierConfig(2),
        placement_policy=AlwaysTLCPolicy(),
        storage_cache_policy=LRUPolicy(),
    )
    storage.seed_block(10, StorageTier.TLC)
    storage.seed_block(1, StorageTier.QLC)

    result = storage.load_block(AccessContext.for_block(1, timestamp=5))

    assert result.source_tier is StorageTier.QLC
    assert result.final_tier is StorageTier.TLC
    assert storage.blocks_in_tier(StorageTier.TLC) == frozenset((1,))
    assert storage.blocks_in_tier(StorageTier.QLC) == frozenset((10,))
    assert [
        (event.tier, event.operation, event.reason, event.block_id) for event in result.io_events
    ] == [
        (StorageTier.QLC, IOOperation.READ, IOReason.DEMAND, 1),
        (StorageTier.TLC, IOOperation.READ, IOReason.DEMOTION, 10),
        (StorageTier.QLC, IOOperation.WRITE, IOReason.DEMOTION, 10),
        (StorageTier.TLC, IOOperation.WRITE, IOReason.PROMOTION, 1),
    ]
    assert storage.tlc_eviction_count == 1


def test_qlc_full_rejects_policy_requested_demotion() -> None:
    storage = StorageManager(
        tlc_config=TierConfig(1),
        qlc_config=TierConfig(1),
        placement_policy=AlwaysQLCPolicy(),
    )
    storage.seed_block(1, StorageTier.TLC)
    storage.seed_block(2, StorageTier.QLC)

    result = storage.load_block(AccessContext.for_block(1))

    assert result.placement_rejected is True
    assert result.final_tier is StorageTier.TLC
    assert storage.placement_rejected_count == 1
    assert len(result.io_events) == 1


def test_seed_is_capacity_checked_and_unknown_blocks_fail_explicitly() -> None:
    storage = StorageManager(TierConfig(1), TierConfig(1))
    storage.seed_block(1)

    with pytest.raises(StorageCapacityError):
        storage.seed_block(2)

    with pytest.raises(BlockNotFoundError, match="99"):
        storage.load_block(AccessContext.for_block(99))


def test_writeback_to_full_tlc_writes_policy_victim_to_qlc() -> None:
    storage = StorageManager(
        tlc_config=TierConfig(1),
        qlc_config=TierConfig(2),
        placement_policy=AlwaysTLCPolicy(),
        storage_cache_policy=LRUPolicy(write_back_on_remove=True),
    )
    storage.seed_block(10, StorageTier.TLC)
    storage.seed_block(1, StorageTier.QLC)

    result = storage.write_block(AccessContext.for_block(1, timestamp=7))

    assert result.final_tier is StorageTier.TLC
    assert storage.tier_of(10) is StorageTier.QLC
    assert [
        (event.tier, event.operation, event.reason, event.block_id) for event in result.io_events
    ] == [
        (StorageTier.TLC, IOOperation.READ, IOReason.DEMOTION, 10),
        (StorageTier.QLC, IOOperation.WRITE, IOReason.DEMOTION, 10),
        (StorageTier.TLC, IOOperation.WRITE, IOReason.WRITEBACK, 1),
    ]


def test_tlc_on_remove_can_drop_victim_without_downward_write() -> None:
    storage = StorageManager(
        tlc_config=TierConfig(1),
        qlc_config=TierConfig(2),
        placement_policy=AlwaysTLCPolicy(),
        storage_cache_policy=LRUPolicy(write_back_on_remove=False),
    )
    storage.seed_block(10, StorageTier.TLC)
    storage.seed_block(1, StorageTier.QLC)

    result = storage.write_block(AccessContext.for_block(1))

    assert result.final_tier is StorageTier.TLC
    assert len(result.io_events) == 1
    assert result.io_events[0].reason is IOReason.WRITEBACK
    with pytest.raises(BlockNotFoundError, match="10"):
        storage.tier_of(10)


def test_storage_cache_policy_controls_full_tlc_overwrite_position() -> None:
    storage = StorageManager(
        tlc_config=TierConfig(2),
        qlc_config=TierConfig(3),
        placement_policy=AlwaysTLCPolicy(),
        storage_cache_policy=HighestBlockIdStorageCachePolicy(),
    )
    storage.seed_blocks((10, 20), StorageTier.TLC)
    storage.seed_block(1, StorageTier.QLC)

    storage.load_block(AccessContext.for_block(1))

    assert storage.blocks_in_tier(StorageTier.TLC) == frozenset((1, 10))
    assert storage.tier_of(20) is StorageTier.QLC
