import pytest

from dwpdsim import Query, SimulationConfig, TierConfig
from dwpdsim.models import AccessContext


def test_configuration_requires_positive_capacities_and_block_size() -> None:
    with pytest.raises(ValueError, match="capacity_blocks"):
        TierConfig(capacity_blocks=0)

    with pytest.raises(ValueError, match="block_size_bytes"):
        SimulationConfig(
            block_size_bytes=0,
            dram=TierConfig(1),
            tlc=TierConfig(1),
            qlc=TierConfig(1),
        )


def test_query_preserves_order_and_converts_sequence_to_tuple() -> None:
    query = Query(timestamp=10, block_ids=[3, 1, 3])  # type: ignore[arg-type]

    assert query.block_ids == (3, 1, 3)
    assert AccessContext.from_query(query, 1).block_id == 1


def test_access_context_rejects_invalid_block_index() -> None:
    query = Query(timestamp=10, block_ids=(1,))

    with pytest.raises(ValueError, match="block_index"):
        AccessContext.from_query(query, 2)

    with pytest.raises(ValueError, match="block_index"):
        AccessContext.from_query(query, -1)


def test_access_context_ttl_is_optional_and_can_be_provided_by_factories() -> None:
    query = Query(timestamp=10, block_ids=(1,))

    assert AccessContext.from_query(query, 0).ttl is None
    assert AccessContext.from_query(query, 0, ttl=30).ttl == 30
    assert AccessContext.for_block(1, timestamp=10, ttl=40).ttl == 40
