import pytest

from dwpdsim import AccessResult, DWPDSimulator, Medium, Query, SequenceIndex
from dwpdsim.errors import InvalidPolicyDecisionError, OutOfOrderQueryError


def test_fixed_lookup_flow_preserves_duplicates_and_parallel_media(config_factory):
    simulator = DWPDSimulator.from_config(config_factory())
    simulator.storage.seed(10, Medium.SLC)
    simulator.storage.seed(20, Medium.TLC)

    results = simulator.process_query(Query(0, (1, 1, 10, 20)))

    assert results == (
        AccessResult.GLOBAL_MISS,
        AccessResult.DRAM_HIT,
        AccessResult.SLC_HIT,
        AccessResult.TLC_HIT,
    )
    assert simulator.storage.medium_of(10) is Medium.SLC
    assert simulator.storage.medium_of(20) is Medium.TLC
    assert simulator.sequence.history(1).access_count == 2

    first = simulator.sequence.existing_child(0, 1)
    assert first is not None
    assert simulator.sequence.existing_child(first, 1) is not None

    writes_before = simulator.storage.writes_from_dram
    simulator.process_query(Query(1, (30,)))
    assert simulator.storage.writes_from_dram == writes_before


def test_sequence_index_shares_request_prefixes():
    sequence = SequenceIndex()
    first_a = sequence.observe(1, 0, 0)
    first_b = sequence.observe(2, 0, first_a)
    second_a = sequence.observe(1, 1, 0)
    second_c = sequence.observe(3, 1, second_a)

    assert first_a == second_a
    assert first_b != second_c
    assert sequence.node(first_a).access_count == 2


class _RejectStorageHits:
    def should_admit(self, context, dram):
        return False

    def choose_victim(self, context, dram):
        return next(iter(dram.blocks))


def test_global_miss_bypasses_storage_hit_admission(config_factory):
    simulator = DWPDSimulator.from_config(
        config_factory(dram_blocks=1),
        dram_policy=_RejectStorageHits(),
    )
    simulator.storage.seed(2, Medium.SLC)

    assert simulator.process_query(Query(0, (1,))) == (AccessResult.GLOBAL_MISS,)
    assert simulator.dram.resident_blocks == frozenset({1})
    assert simulator.process_query(Query(1, (2,))) == (AccessResult.SLC_HIT,)
    assert simulator.dram.resident_blocks == frozenset({1})


class _CaptureDropPlacement:
    def __init__(self):
        self.context = None

    def choose(self, context, storage):
        self.context = context


def test_eviction_places_the_victim_with_its_own_history(config_factory):
    placement = _CaptureDropPlacement()
    simulator = DWPDSimulator.from_config(
        config_factory(dram_blocks=1),
        placement_policy=placement,
    )
    simulator.process_query(Query(0, (1,), other_info={"request": "first"}))
    simulator.process_query(Query(1, (2,), other_info={"request": "second"}))

    assert placement.context.block_id == 1
    assert placement.context.history.access_count == 1
    assert placement.context.trigger.block_id == 2
    assert placement.context.trigger.query.other_info == {"request": "second"}
    assert simulator.dram.resident_blocks == frozenset({2})
    assert not simulator.storage.contains(1)


class _InvalidVictim:
    def should_admit(self, context, dram):
        return True

    def choose_victim(self, context, dram):
        return 999


def test_invalid_dram_decision_does_not_commit_the_block(config_factory):
    simulator = DWPDSimulator.from_config(
        config_factory(dram_blocks=1),
        dram_policy=_InvalidVictim(),
    )
    simulator.process_query(Query(0, (1,)))
    before = simulator.stats()

    with pytest.raises(InvalidPolicyDecisionError):
        simulator.process_query(Query(1, (2,)))

    assert simulator.dram.resident_blocks == frozenset({1})
    assert simulator.sequence.history(2) is None
    assert simulator.stats() == before
    assert simulator.process_query(Query(0.5, (1,))) == (AccessResult.DRAM_HIT,)


def test_equal_timestamps_are_kept_and_decreasing_time_is_rejected(config_factory):
    simulator = DWPDSimulator.from_config(config_factory())
    simulator.process_query(Query(5, (1,)))
    simulator.process_query(Query(5, (2,)))

    with pytest.raises(OutOfOrderQueryError):
        simulator.process_query(Query(4, (3,)))
