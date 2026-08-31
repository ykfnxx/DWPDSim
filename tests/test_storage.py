import pytest

from dwpdsim import Medium
from dwpdsim.errors import InvalidPolicyDecisionError
from dwpdsim.managers import SSDManager, StorageManager
from dwpdsim.models import (
    AccessContext,
    ChunkState,
    Placement,
    PlacementContext,
    Query,
    SequenceNodeView,
)
from dwpdsim.policies import FixedPlacementPolicy


def _placement_context(block_id):
    query = Query(0, (block_id,))
    root = SequenceNodeView(0, None, None, 0, 0)
    access = AccessContext(query, 0, block_id, None, root, None)
    return PlacementContext(block_id, None, access)


def test_ssd_appends_seals_and_naively_compacts_without_gc_write_count(config_factory):
    config = config_factory(chunks=4, blocks_per_chunk=2)
    ssd = SSDManager(Medium.SLC, config.slc, config.block_size_bytes)

    first = ssd.program(1, 0)
    second = ssd.program(2, 0)
    third = ssd.program(3, 0)

    assert first.chunk_id == second.chunk_id
    assert ssd.chunks[first.chunk_id].state is ChunkState.SEALED
    assert ssd.chunks[third.chunk_id].state is ChunkState.ACTIVE

    ssd.delete(3)
    assert ssd.whole_erase_count == 1

    ssd.delete(1)
    assert ssd.non_full_erase_count == 1
    assert ssd.contains(2)
    assert ssd.location(2).chunk_id != first.chunk_id
    assert ssd.logical_writes_by_stream == (3,)


def test_each_stream_has_at_most_one_active_chunk(config_factory):
    config = config_factory(chunks=4, blocks_per_chunk=2, streams=2)
    ssd = SSDManager(Medium.SLC, config.slc, config.block_size_bytes)

    ssd.program(1, 0)
    ssd.program(2, 1)
    ssd.program(3, 0)
    ssd.program(4, 0)

    assert set(ssd.active_chunk_ids) == {0, 1}
    assert len(set(ssd.active_chunk_ids.values())) == 2


class _BadGC:
    def relocation_order(self, chunk):
        return ()


def test_invalid_gc_plan_is_rejected_before_chunk_changes(config_factory):
    config = config_factory(chunks=3, blocks_per_chunk=2)
    ssd = SSDManager(Medium.SLC, config.slc, config.block_size_bytes, _BadGC())
    ssd.program(1, 0)
    ssd.program(2, 0)
    before = ssd.chunks

    with pytest.raises(InvalidPolicyDecisionError):
        ssd.delete(1)

    assert ssd.chunks == before
    assert ssd.contains(1)
    assert ssd.contains(2)


def test_storage_capacity_eviction_keeps_one_unique_location(config_factory):
    config = config_factory(chunks=2, blocks_per_chunk=1)
    storage = StorageManager(
        config,
        placement_policy=FixedPlacementPolicy(Medium.SLC, 0),
    )
    storage.seed(1, Medium.SLC)

    storage.write_from_dram(_placement_context(2))

    assert storage.blocks == frozenset({2})
    assert storage.medium_of(2) is Medium.SLC
    assert storage.slc.whole_erase_count == 1
    assert storage.writes_from_dram[Medium.SLC] == 1


def test_storage_can_reclaim_a_whole_chunk_from_another_stream(config_factory):
    config = config_factory(chunks=3, blocks_per_chunk=1, streams=2)
    storage = StorageManager(
        config,
        placement_policy=FixedPlacementPolicy(Medium.SLC, 1),
    )
    storage.seed(1, Medium.SLC, stream_id=0)
    storage.seed(2, Medium.SLC, stream_id=0)

    storage.write_from_dram(_placement_context(3))

    assert storage.blocks == frozenset({2, 3})
    assert storage.stream_of(2) == 0
    assert storage.stream_of(3) == 1


def test_explicit_transfer_is_bidirectional_and_counts_only_target_writes(config_factory):
    storage = StorageManager(config_factory())
    storage.seed(1, Medium.SLC)

    storage.transfer(1, Medium.TLC, 0)
    assert storage.medium_of(1) is Medium.TLC
    assert not storage.slc.contains(1)
    assert storage.tlc.contains(1)

    storage.transfer(1, Medium.SLC, 0)
    assert storage.medium_of(1) is Medium.SLC
    assert storage.writes_from_dram == {Medium.SLC: 0, Medium.TLC: 0}
    assert storage.transfers[(Medium.SLC, Medium.TLC)] == 1
    assert storage.transfers[(Medium.TLC, Medium.SLC)] == 1
    assert storage.slc.logical_writes_by_stream == (1,)
    assert storage.tlc.logical_writes_by_stream == (1,)


def test_failed_transfer_keeps_both_media_unchanged(config_factory):
    storage = StorageManager(
        config_factory(chunks=2, blocks_per_chunk=1),
        eviction_policy=_InvalidStorageVictim(),
    )
    storage.seed(1, Medium.SLC)
    storage.seed(2, Medium.TLC)
    slc_before = storage.slc.chunks
    tlc_before = storage.tlc.chunks

    with pytest.raises(InvalidPolicyDecisionError):
        storage.transfer(1, Medium.TLC, 0)

    assert storage.medium_of(1) is Medium.SLC
    assert storage.medium_of(2) is Medium.TLC
    assert storage.slc.chunks == slc_before
    assert storage.tlc.chunks == tlc_before
    assert sum(storage.transfers.values()) == 0


class _InvalidPlacement:
    def choose(self, context, storage):
        return Placement("invalid", 0)


class _InvalidStorageVictim:
    def choose(self, medium, stream_id, incoming_block_id, candidates):
        return 999


@pytest.mark.parametrize(
    "placement,invalid_eviction",
    [
        (_InvalidPlacement(), False),
        (FixedPlacementPolicy(Medium.SLC, 99), False),
        (FixedPlacementPolicy(Medium.SLC, 0), True),
    ],
)
def test_invalid_storage_policy_does_not_change_existing_blocks(
    config_factory,
    placement,
    invalid_eviction,
):
    config = config_factory(chunks=2, blocks_per_chunk=1)
    storage = StorageManager(
        config,
        placement_policy=placement,
        eviction_policy=_InvalidStorageVictim() if invalid_eviction else None,
    )
    storage.seed(1, Medium.SLC)

    with pytest.raises(InvalidPolicyDecisionError):
        storage.write_from_dram(_placement_context(2))

    assert storage.blocks == frozenset({1})
    assert storage.slc.contains(1)
