from dwpdsim import Query, TierConfig
from dwpdsim.memory import MemManager
from dwpdsim.models import AccessContext, IOOperation, IOReason, StorageTier
from dwpdsim.policies import AlwaysQLCPolicy, FIFOPolicy, LRUPolicy
from dwpdsim.storage import StorageManager


class LowestBlockIdMemCachePolicy:
    """Memory-only policy that intentionally has no choose_overwrite method."""

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

    def choose_victim(self, context: AccessContext) -> int:
        del context
        return min(self._blocks)

    def reset(self) -> None:
        self._blocks.clear()


class NeverAdmitPolicy:
    """Reject storage-loaded blocks so the storage-miss path can be isolated."""

    def should_admit(self, context: AccessContext, storage_result: object) -> bool:
        del context, storage_result
        return False

    def reset(self) -> None:
        return None


def make_storage() -> StorageManager:
    return StorageManager(
        tlc_config=TierConfig(2),
        qlc_config=TierConfig(10),
        placement_policy=AlwaysQLCPolicy(),
        initial_blocks=(1, 2, 3),
    )


def test_mem_manager_processes_blocks_sequentially_within_query() -> None:
    memory = MemManager(
        config=TierConfig(2),
        lower_storage=make_storage(),
        mem_cache_policy=LRUPolicy(),
    )

    result = memory.process_query(Query(timestamp=100, block_ids=(1, 2, 1, 3)))

    assert [item.memory.hit for item in result.block_results] == [False, False, True, False]
    assert result.memory_hits == 1
    assert result.memory_misses == 3
    assert memory.resident_blocks == frozenset((1, 3))
    assert memory.eviction_count == 1
    assert all(
        item.storage is None or item.storage.source_tier is StorageTier.QLC
        for item in result.block_results
    )


def test_mem_cache_policy_is_injected_and_changes_eviction_behavior() -> None:
    lru_memory = MemManager(
        config=TierConfig(2),
        lower_storage=make_storage(),
        mem_cache_policy=LRUPolicy(),
    )
    fifo_memory = MemManager(
        config=TierConfig(2),
        lower_storage=make_storage(),
        mem_cache_policy=FIFOPolicy(),
    )
    query = Query(timestamp=1, block_ids=(1, 2, 1, 3))

    lru_memory.process_query(query)
    fifo_memory.process_query(query)

    assert lru_memory.resident_blocks == frozenset((1, 3))
    assert fifo_memory.resident_blocks == frozenset((2, 3))


def test_on_remove_decides_whether_eviction_is_written_downward() -> None:
    writeback_memory = MemManager(
        config=TierConfig(1),
        lower_storage=make_storage(),
        mem_cache_policy=LRUPolicy(write_back_on_remove=True),
    )
    drop_memory = MemManager(
        config=TierConfig(1),
        lower_storage=make_storage(),
        mem_cache_policy=LRUPolicy(write_back_on_remove=False),
    )
    query = Query(timestamp=1, block_ids=(1, 2))

    writeback_result = writeback_memory.process_query(query)
    drop_result = drop_memory.process_query(query)

    writeback_admission = writeback_result.block_results[1].admission
    assert writeback_admission is not None
    assert writeback_admission.writeback is not None
    assert [
        (event.tier, event.operation, event.reason)
        for event in writeback_admission.writeback.io_events
    ] == [(StorageTier.QLC, IOOperation.WRITE, IOReason.WRITEBACK)]

    drop_admission = drop_result.block_results[1].admission
    assert drop_admission is not None
    assert drop_admission.writeback is None


def test_mem_cache_policy_does_not_require_storage_overwrite_interface() -> None:
    memory = MemManager(
        config=TierConfig(1),
        lower_storage=make_storage(),
        mem_cache_policy=LowestBlockIdMemCachePolicy(),
    )

    memory.process_query(Query(timestamp=1, block_ids=(1, 2)))

    assert memory.resident_blocks == frozenset((2,))


def test_storage_miss_inserts_block_directly_into_dram() -> None:
    storage = make_storage()
    memory = MemManager(
        config=TierConfig(2),
        lower_storage=storage,
        mem_cache_policy=LRUPolicy(),
    )

    result = memory.process_query(Query(timestamp=1, block_ids=(99, 99)))

    assert [item.memory.hit for item in result.block_results] == [False, True]
    assert result.block_results[0].storage is None
    assert result.block_results[0].inserted_on_storage_miss is True
    assert result.block_results[0].admission is not None
    assert result.block_results[0].admission.admitted is True
    assert result.block_results[1].inserted_on_storage_miss is False
    assert result.insertions == 1
    assert memory.resident_blocks == frozenset((99,))
    assert storage.contains_block(99) is False


def test_inserted_block_reaches_storage_only_after_dram_writeback() -> None:
    storage = StorageManager(
        tlc_config=TierConfig(2),
        qlc_config=TierConfig(10),
        placement_policy=AlwaysQLCPolicy(),
    )
    memory = MemManager(
        config=TierConfig(1),
        lower_storage=storage,
        mem_cache_policy=LRUPolicy(write_back_on_remove=True),
    )

    result = memory.process_query(Query(timestamp=1, block_ids=(99, 100)))

    assert result.insertions == 2
    first_admission = result.block_results[0].admission
    second_admission = result.block_results[1].admission
    assert first_admission is not None and first_admission.writeback is None
    assert second_admission is not None and second_admission.writeback is not None
    assert [
        (event.tier, event.operation, event.reason, event.block_id)
        for event in second_admission.writeback.io_events
    ] == [(StorageTier.QLC, IOOperation.WRITE, IOReason.WRITEBACK, 99)]
    assert storage.tier_of(99) is StorageTier.QLC
    assert storage.contains_block(100) is False


def test_storage_miss_insertion_bypasses_loaded_block_admission_policy() -> None:
    memory = MemManager(
        config=TierConfig(2),
        lower_storage=make_storage(),
        admission_policy=NeverAdmitPolicy(),
    )

    result = memory.process_query(Query(timestamp=1, block_ids=(1, 99)))

    loaded_admission = result.block_results[0].admission
    inserted_admission = result.block_results[1].admission
    assert loaded_admission is not None and loaded_admission.admitted is False
    assert inserted_admission is not None and inserted_admission.admitted is True
    assert result.block_results[1].inserted_on_storage_miss is True
    assert memory.resident_blocks == frozenset((99,))
