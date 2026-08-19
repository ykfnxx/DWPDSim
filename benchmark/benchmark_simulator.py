"""Synthetic throughput benchmark for aggregate and detailed execution modes."""

from __future__ import annotations

import argparse
import time
from collections.abc import Iterator

from dwpdsim import DWPDSimulator, Query, SimulationConfig, TierConfig
from dwpdsim.policies import AlwaysQLCPolicy


def generate_queries(
    query_count: int,
    blocks_per_query: int,
    block_universe: int,
) -> Iterator[Query]:
    """Yield a deterministic trace without materializing it in memory."""

    stride = max(1, blocks_per_query // 2)
    for timestamp in range(query_count):
        first_block = (timestamp * stride) % block_universe
        yield Query(
            timestamp=timestamp,
            block_ids=tuple(
                (first_block + offset) % block_universe for offset in range(blocks_per_query)
            ),
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queries", type=int, default=100_000)
    parser.add_argument("--blocks-per-query", type=int, default=8)
    parser.add_argument("--block-universe", type=int, default=65_536)
    parser.add_argument("--dram-capacity", type=int, default=4_096)
    parser.add_argument(
        "--mode",
        choices=("aggregate", "detailed"),
        default="aggregate",
        help="run() aggregation or process_query() detailed-result construction",
    )
    args = parser.parse_args()
    for name in ("queries", "blocks_per_query", "block_universe", "dram_capacity"):
        if getattr(args, name) <= 0:
            parser.error(f"--{name.replace('_', '-')} must be positive")
    return args


def main() -> None:
    args = parse_args()
    config = SimulationConfig(
        block_size_bytes=4096,
        dram=TierConfig(capacity_blocks=args.dram_capacity),
        tlc=TierConfig(capacity_blocks=1),
        qlc=TierConfig(capacity_blocks=args.block_universe),
    )
    simulator = DWPDSimulator.from_config(
        config,
        initial_blocks=range(args.block_universe),
        storage_placement_policy=AlwaysQLCPolicy(),
    )
    queries = generate_queries(
        query_count=args.queries,
        blocks_per_query=args.blocks_per_query,
        block_universe=args.block_universe,
    )

    started_at = time.perf_counter()
    if args.mode == "aggregate":
        report = simulator.run(queries)
    else:
        for query in queries:
            simulator.process_query(query)
        report = simulator.report()
    elapsed_seconds = time.perf_counter() - started_at

    block_count = report.metrics.block_access_count
    print(f"mode: {args.mode}")
    print(f"queries: {report.metrics.query_count:,}")
    print(f"block accesses: {block_count:,}")
    print(f"elapsed: {elapsed_seconds:.3f} s")
    print(f"queries/s: {report.metrics.query_count / elapsed_seconds:,.0f}")
    print(f"blocks/s: {block_count / elapsed_seconds:,.0f}")
    print(f"DRAM hit rate: {report.metrics.dram_hit_rate:.2%}")


if __name__ == "__main__":
    main()
