"""运行一个小型 DRAM + SLC/TLC 模拟并写出原始统计。"""

from pathlib import Path

from dwpdsim import DWPDSimulator, Medium, Query, SimulationConfig, SSDConfig


def build_simulator() -> DWPDSimulator:
    """创建满足 stream 与 GC reserve 约束的模拟器。"""

    config = SimulationConfig(
        block_size_bytes=4096,
        dram_capacity_bytes=2 * 4096,
        slc=SSDConfig(
            capacity_bytes=64 * 1024,
            chunk_size_bytes=16 * 1024,
            stream_count=2,
            gc_reserve_chunks=1,
        ),
        tlc=SSDConfig(
            capacity_bytes=128 * 1024,
            chunk_size_bytes=16 * 1024,
            stream_count=2,
            gc_reserve_chunks=1,
        ),
    )
    simulator = DWPDSimulator.from_config(config)

    # 初始持久层必须显式 seed；seed 不计模拟写入。
    simulator.storage.seed(1, Medium.SLC, stream_id=0)
    simulator.storage.seed(4, Medium.TLC, stream_id=1)
    return simulator


def build_queries() -> tuple[Query, ...]:
    """构造以秒为单位、保留重复 block 的有序请求。"""

    return (
        Query(timestamp=0.0, hash_ids=(1, 2, 1)),
        Query(
            timestamp=3600.0,
            hash_ids=(3, 4, 2),
            other_info={"request_id": "request-2"},
        ),
        Query(timestamp=7200.0, hash_ids=(5, 1, 5)),
    )


def main() -> None:
    simulator = build_simulator()
    stats = simulator.run(build_queries())

    output = Path("simulation_stats.json")
    simulator.write_stats(output)

    print("DWPDSim accesses")
    print(stats["accesses"])
    print(f"statistics written to {output}")


if __name__ == "__main__":
    main()
