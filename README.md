# DWPDSim

DWPDSim 回放 KV cache block 请求，在一棵公共 RadixTree 上模拟内存、SLC 和 TLC 的驻留、
placement、淘汰与迁移。C++17 core 是逻辑状态、pool-local 地址、虚拟时间、trace 和 metrics
的唯一所有者；policy 只读取视图并返回决策。vNext 是破坏性接口，设计契约见
[`.design/vnext-policy-refactor.md`](.design/vnext-policy-refactor.md)。

## 行为语义

每条请求携带全局纳秒时间、唯一 request id、用于 stream placement/session gap 的 affinity id，
以及完整有序的 hash path。`hash_id` 同时是全局唯一 `NodeId`，parent link 只表达前缀拓扑。

- memory hit 不产生 I/O；
- storage hit 产生 READ，MemoryPolicy 决定是否提升到内存；
- global miss 代表计算出新 block，并加入内存，不产生 READ；
- `baseline_lru` / `indexed_lru` 按 segment LRU 选择 leaf segment；`Drop` 只剪枝该 leaf segment，`Dump` 才以 segment
  为单位向 parent 贪婪，已写盘 segment 释放内存后继续向上，在首个含未写盘 block 的 segment
  写盘并停止；
- StoragePolicy 统一决定 Dump placement、同步 capacity reclaim、access migration 和后台维护；
- relocation 是管理意图，不是设备 opcode。Simulator 将每个 block 展开为
  `READ(source) -> WRITE(destination) -> TRIM(source)`；access migration 复用本次 storage-hit
  READ，后台 migration 显式产生 READ；
- adaptive-endurance policy 的后台 tick 独立于前台请求运行。相同 timestamp 的 tick 先执行，`finish()` 会将
  虚拟时间推进至配置中冻结的 `simulation_end_ns`；Python `finish()` 不另接收终点参数。

DWPDSim 不模拟 SSD 内部 GC、NAND latency、擦除或物理写放大；这些由下游 MQSim 计算。

## 安装与验证

需要 Python 3.11+、CMake 3.18+ 和 C++17 编译器：

```bash
python3 -m pip install -e .
python3 -m pip install -e '.[dev]'
ruff check .
pytest
```

### 使用 uv

项目的 uv 配置位于 `pyproject.toml`，依赖版本记录在 `uv.lock`。
默认 dev 依赖组复用 `dev` 和 `input` extras，包含测试、Ruff 和 Parquet 输入依赖。

```bash
uv sync --locked
uv run --locked ruff check .
uv run --locked pytest
uv run --locked python example/run_pipeline.py
```

`uv sync` 在项目目录创建 `.venv` 并编译 C++ 扩展。C++ 源码、头文件和 CMake 配置已加入
构建缓存键，修改后执行 `uv sync` 或 `uv run` 会重新检查构建；不要直接使用旧解释器运行测试。
需要强制重建时执行 `uv sync --locked --reinstall-package dwpdsim`。

只安装运行依赖使用 `uv sync --locked --no-dev`；需要 Hugging Face 数据源使用
`uv sync --locked --extra hub`，后续 `uv run` 同样加 `--extra hub`。
修改依赖声明后运行 `uv lock`，并一起提交 `pyproject.toml` 和 `uv.lock`。
配置依据 [uv 官方文档](https://docs.astral.sh/uv/reference/settings/#cache-keys)。

## 基本用法

```python
from dwpdsim import (
    DWPDSimulator,
    MemoryConfig,
    Request,
    SimulationConfig,
    StoragePolicyConfig,
    StorageTierConfig,
)

MIB = 1024 * 1024
BLOCK_SIZE = 4096
config = SimulationConfig(
    block_size_bytes=BLOCK_SIZE,
    memory=MemoryConfig(capacity_bytes=2 * BLOCK_SIZE),
    slc=StorageTierConfig(capacity_bytes=MIB, stream_count=2),
    tlc=StorageTierConfig(capacity_bytes=MIB, stream_count=2),
    storage_policy=StoragePolicyConfig(
        kind="baseline_fixed_lru",
        fixed_tier="tlc",
        fixed_stream_id=0,
    ),
)

with DWPDSimulator(config, "simulation_trace.csv") as simulator:
    simulator.run(
        [
            Request(0, 1, 10, [1, 2, 3]),
            Request(1_000_000_000, 2, 10, [1, 2, 4]),
            Request(2_000_000_000, 3, 20, [5, 6]),
        ]
    )

simulator.write_stats("simulation_metrics.json")
```

`Request` 的位置参数依次是 `timestamp_ns, request_id, affinity_id, hash_ids`。request id 必须唯一，
timestamp 必须非递减。

## 批量输入

大数据集使用五个连续的 `uint64` buffer。第 `i` 条请求对应
`hash_ids[offsets[i]:offsets[i + 1]]`：

```python
import numpy as np

simulator.process_batch(
    np.asarray([0, 10], dtype=np.uint64),          # timestamps_ns
    np.asarray([100, 101], dtype=np.uint64),       # request_ids
    np.asarray([7, 7], dtype=np.uint64),           # affinity_ids
    np.asarray([0, 3, 5], dtype=np.uint64),        # offsets
    np.asarray([1, 2, 3, 1, 4], dtype=np.uint64), # hash_ids
)
```

批量和逐请求接口产生相同的 tick、决策、trace 和 metrics。

## Policy

顶层接口只有 `MemoryPolicy` 和 `StoragePolicy`：

- `baseline_lru`：memory admission、leaf-first segment LRU，并返回向 parent segment 贪婪的
  `Dump`；
- `baseline_fixed_lru`：固定 tier/stream placement，leaf-LRU capacity reclaim；
- `baseline_ratio_lru`：按 SLC write ratio placement，pool 内 round-robin stream；
- `wear_share_round_robin`：wear-share tier placement 与 pool 内 round-robin stream；
- `wear_share_affinity`：wear-share tier placement 与 affinity hash stream；
- `wear_balanced`：Shadow FTL 反馈修正寿命预算和迁移压力，可选在线 idle 控制，见文末配置说明。
- `adaptive_endurance`：endurance-weighted placement、session gap/q95、自适应 promotion、access
  migration、周期 idle eviction 和后台 migration。

参考目录中的三种算法在当前代码中使用按决策机制命名的 policy：

| 参考算法 | 当前 `StoragePolicyConfig.kind` | C++ policy | 保留的决策 |
| --- | --- | --- | --- |
| RR / `rr_wear_sb` | `wear_share_round_robin` | `WearShareRoundRobinStoragePolicy` | 按目标写入份额选择 SLC/TLC，pool 内 round-robin stream |
| Algorithm1 / `session_wear_sb` | `wear_share_affinity` | `WearShareAffinityStoragePolicy` | 按目标写入份额选择 SLC/TLC，用 affinity 稳定散列到 stream |
| Algorithm2 / `tiered2` | `adaptive_endurance` | `AdaptiveEnduranceStoragePolicy` | endurance-weighted placement、session gap/q95、occupancy pressure、动态 promotion、后台 migration 和 idle eviction |

RR 默认使用精确 segment 索引，tier placement、stream 轮转、Storage LRU 时间与
`(last_ns, endpoint)` 平局顺序保持原行为。`StoragePolicyConfig` 的 RR 专用参数：

- `rr_victim_search="indexed"`：有序索引；`"scan"` 使用原始扫描；`"fused"` 合并候选的重复遍历。
- `rr_subtree_counts=False`：默认使用树搜索检查 Storage 后代；True 启用增量子树计数用于消融。
- `rr_verify_victims=False`：True 时额外维护原扫描状态，逐次校验 victim 和时间；用于正确性验证。
- `rr_profile=False`：True 时计时 RR 查询与 policy 状态维护，通过 `sim.storage_performance()` 读取。

示例入口对应 `DWPDSIM_RR_VICTIM_SEARCH`、`DWPDSIM_RR_SUBTREE_COUNTS`、
`DWPDSIM_RR_VERIFY_VICTIMS`、`DWPDSIM_RR_PROFILE`；旧 `.env` 未填写时使用上述默认值。
其他 StoragePolicy 不使用这些 RR 参数。完整实现、消融与复现命令见
[RR 性能报告](.design/perf/rr-storage-ablation.md)。

这是算法决策的对应关系，不是旧名兼容层；参考算法名不能作为当前 `kind`
传入。三种 policy 都只在 MemoryPolicy 产生 Dump 时决定初始 placement，global miss
只先进入内存。`adaptive_endurance` 在 Dump 和 access migration 中使用请求 affinity；
后台 migration 没有请求上下文，使用 segment endpoint 选择目标 stream。

所有 storage 决策共用一个 policy state，并只通过 commit notification 更新。实现位于
`cpp/include/dwpdsim/policies/storage/` 和 `cpp/src/policies/storage/`；memory policy 的接口与实现
分别位于 `cpp/include/dwpdsim/policies/memory/` 和 `cpp/src/policies/memory/`。Simulator 保持 RadixTree、StorageState
和 LBA allocator 的唯一写权限。
后台 tick、segment 展开和 `READ -> WRITE -> TRIM` 降低由 Simulator 统一执行；MQSim
只模拟已决定的物理 I/O，不运行上述 policy。

## Canonical trace 与 metrics

trace schema version 4 固定为 14 列：

```text
sequence,timestamp_ns,request_id,access_sequence,operation,storage_tier,stream_id,offset_bytes,length_bytes,node_id,hash_id,reason,move_id,depends_on_sequence
```

operation 只有 `READ`、`WRITE`、`TRIM`。`sequence` 是全局 semantic I/O 编号；offset 是原始
pool-local byte address；stream id 是 tier-local。relocation 的同一 segment 共用 `move_id`，
每个 block 各自形成 READ、WRITE、TRIM completion chain。非 relocation I/O 不设置 dependency。

`metrics.json` 的主要口径包括：request/block accesses 与 hit rate、Dump admission/rejection、
前台 capacity eviction、后台 tick/idle eviction、三类 migration、relocation source read/
destination write/source trim、SLC/TLC live/peak/program/host-write bytes、每 stream write bytes、
adaptive-endurance gap/q95/idle threshold、placement 和错误计数。DWPDSim 的
`host_write_bytes` 只统计
Memory Dump；relocation destination WRITE 只进入 program bytes。

命中统计按每个请求在 DRAM 与 SLC/TLC 中的联合连续前缀计算：层级切换不打断命中，
首次两处都不存在的 block 及其全部后缀均计入 `global_misses`，即使后缀仍有缓存副本。
例如 A 在 Storage、B 在 DRAM、C 缺失、D 驻留时，该请求计 2 hit、2 miss。
`accesses.compute_cost` 累计每个请求的 `miss block 数 × 完整 context block 数`，单位为
block²；miss block 数沿用上述连续前缀口径。例如 100 blocks 的请求连续命中前 60 blocks，
贡献 40 个 `global_misses` 和 4000 的 `compute_cost`。全命中或空请求贡献 0。
`memory_hit_rate` 和 `total_hit_rate` 的分母为全部 block 访问数；`storage_hit_rate`
为 `(slc_hits + tlc_hits) / (slc_hits + tlc_hits + global_misses)`，分母为未计作内存命中的访问数。
该前缀规则用于访问命中统计；节点访问状态、缓存读写、提升与淘汰仍按逐 block 的实际驻留状态执行，
因此 Storage READ 数不一定等于统计的 Storage hit 数。

## MQSim pipeline

完整的 workload、DWPDSim policy、SSD XML 和运行配置见
[`docs/mqsim-pipeline.md`](docs/mqsim-pipeline.md)。
可直接复制 dotenv 模板并运行完整示例：

```bash
cp example/.env.example example/.env
.venv/bin/python example/run_pipeline.py
```

配套 MQSim vNext 在一次运行中回放全部 SLC/TLC flow。先编译同级 MQSim，然后运行：

```bash
make -C ../MQSim
python3 scripts/mqsim_pipeline.py simulation_trace.csv simulation_metrics.json \
  --mqsim-binary ../MQSim/MQSim \
  --ssd-config example/mqsim/ssdconfig.xml \
  --output build/mqsim-run
```

converter 的稳定契约是：

- 只接受 schema v4 的精确 header，并在完整转换时核对 metrics `trace.events`；
- 只接受一个同时包含精确 `slc`/`tlc` pool 的 SSD XML；pool logical capacity 必须与 DWPDSim
  metrics 一致，measurement window 使用同一绝对纳秒时间轴；设备必须使用 NVMe、FLASH、
  PAGE_LEVEL mapping，关闭 preconditioning，且两个 pool 分别引用 SLC/TLC media profile；
- 固定生成全部配置 stream：SLC flow 先于 TLC flow，空 stream 也生成空 trace；MQSim V1 的
  NVMe queue ABI 限制总 flow 数不超过 8；
- 保留 tier-local stream 与 pool-local LBA，不按 NodeId 或 active stream 重映射；
- 为每个实际 command 分配全局连续 id。超过 65535 sectors 的 semantic I/O 按地址切分，
  `commands.csv` 保存 command id 到 source sequence/chunk 的映射；
- `DWPDSIM_DEPENDENCY_V1` 的最后一列是 `depends_on_request_ids`，使用 `-1` 或逗号分隔的多个
  command id。converter 合并 relocation/chunk dependency 与同一 `(tier, stream, range)` 的
  mutation/read hazard，不串行并发 READ；
- `manifest.json` 冻结 trace/metrics/config path、SSD 内容 hash、measurement window、固定 flow
  映射及逐 pool/flow command 和 byte totals；
- `summary.json` 按显式 Flow_ID、Pool_ID、Channel ID 消费 MQSim result ABI v1，核对配置 hash、
  window、容量、请求和字节数，并计算 measurement host DWPD、NAND DWPD、WAF 与最大 block
  PE/day。零分母写 `null`。

MQSim 的 pool `Host_Write_Bytes` 包含 Memory Dump 和 relocation destination WRITE；它与
DWPDSim dump-only `storage.<tier>.host_write_bytes` 是两个独立口径，summary 同时保留两者。
`--event-limit N` 只用于转换/回放前 N 条 semantic I/O 的快速检查；manifest 仍记录完整输入行数。

## Memory 性能实验与批量输入

Memory 的索引实现为显式可选项，StoragePolicy 保持原实现。默认 `baseline_lru` 保留原始
链表扫描；`indexed_lru` 用 Memory block 的逻辑访问序号维护 segment 的最大热度：

```python
from dwpdsim import MemoryPolicyConfig

memory_policy = MemoryPolicyConfig(
    kind="indexed_lru",
    groups=1,           # 精确单组索引
    sampled_groups=0,   # 0 查询全部组；非零是近似选择，会改变模拟结果
    workers=1,          # >1 使用常驻 C++ 线程并行查询各组
    seed=42,            # 近似分组的确定性 seed
    profile=False,      # True 额外计量 Memory 决策和维护耗时
    retention_ns=None,   # 可选空闲保留时间；例如 60 * 1_000_000_000 表示 60 秒
)
```

将它传入 `SimulationConfig(memory_policy=memory_policy, ...)`。多线程仅并行候选查询，
树修改、索引更新与 Dump 提交保持串行。精确模式保持原有 victim、metrics 和 trace 顺序。
`simulator.memory_performance()` 返回独立工作量计数；启用 profile 后包含纳秒耗时，
不污染业务 `stats()`。组数增加和 worker 增加不保证更快。

使用 `example/run_pipeline.py` 时，在 `example/.env` 中设置：

```dotenv
DWPDSIM_MEMORY_POLICY=indexed_lru
DWPDSIM_MEMORY_RETENTION_NS=60000000000
```

这里的 `60000000000` 表示 60 秒，须填写纳秒整数，不能写 Python 表达式。
该环境变量映射到 `MemoryPolicyConfig.retention_ns`；留空或不设置表示关闭，`0` 是有效阈值。
模板见 [example/.env.example](example/.env.example)。shell 中同名环境变量优先于 `.env`；
直接构造 Python `SimulationConfig` 不会自动读取这些环境变量。

`retention_ns` 用于 `indexed_lru` 和 `context_lru`，默认 `None` 关闭。选出 victim 后，以当前请求的模拟时间
减去该 segment 中最近被访问的 **Memory 驻留 block** 的访问时间；严格大于阈值时返回
`Drop`，否则返回 `Dump`。等于阈值仍 Dump，`0` 表示正的空闲时间就 Drop。
一次访问会刷新判断依据；分裂/合并后按新段的 Memory 成员判断。LRU 选择顺序保持不变，
只决定选中段的处理动作。不设置定时过期任务；未被选中淘汰的段不会因为超时自动移除。
Drop 只移除所选段的 Memory 副本，不向父段继续 Dump，不删除已有 Storage 副本。
启用后会改变写入量及命中结果，原有精确模式等价性与消融结果仅适用于关闭此参数时。


### 无限 Storage 的 Memory 实验模式

`StoragePolicyConfig(kind="infinite_storage")` 使用轻量运行路径，Memory 淘汰产生的逻辑副本
统一保存在无限 TLC，永不回收。它不创建 Storage policy，不维护 Storage LRU、迁移、地址和
后台任务，不展开 Storage 命中的整段，也不创建 trace 文件。保留 Memory admission、段淘汰和
各 policy 的回收范围规则，以及逻辑读写计数。它不会在首次访问时提前把仍在 Memory 的 block 写盘。

在 `example/.env` 中设置（模板为 `example/.env.example`）：

```dotenv
DWPDSIM_STORAGE_POLICY=infinite_storage
DWPDSIM_MEMORY_POLICY=context_lru
DWPDSIM_MEMORY_ALPHA=0.01
DWPDSIM_MEMORY_MAX_EVICTION_BLOCKS=64
DWPDSIM_MEMORY_RETENTION_NS=
DWPDSIM_ADMIT_STORAGE_HITS=true
```

然后运行 `uv run --locked python example/run_pipeline.py`。输出为
`build/example-pipeline/simulation_metrics.json`（或配置的输出目录），入口自动跳过 MQSim。
SLC/TLC 容量和 stream 配置在该模式下忽略；Python API 可传入 `StorageTierConfig(0, 0)`。
`trace_path` 参数仍接受路径但不会创建或覆盖文件，已有同名 trace 不代表本次输出。
结果中 `configuration.storage_mode="infinite_storage"`，TLC 容量为 `null`，`trace.events=0`。
`storage.tlc.reads/writes` 是逻辑 I/O，不是生成的 MQSim trace 或设备时间。

比较同一数据上的 LRU 与 context LRU：

```bash
uv run --locked python benchmark/memory_ablation.py \
  --dataset input.parquet --output build/memory-effect/alpha-001 \
  --variants index,context --storage-policy infinite_storage \
  --memory-blocks 4096 --alpha 0.01 --repeats 3
```

输入须包含 `timestamp_ns, request_id, affinity_id, hash_ids` 四列，按时间顺序排列。
各次实验固定输入、Memory 容量和 admission，仅改变 alpha，并使用不同输出目录。
`summary.json` 中每个变体的 `stats.accesses.memory_hit_rate` 表示连续前缀口径的 Memory 命中率；
`stats.storage.tlc.reads.blocks` 表示实际读盘 block 数，两者应同时比较。
还可查看 `stats.memory.evicted_blocks`、`stats.accesses.global_misses` 和 `compute_cost`。
`--profile` 可额外测量策略维护及决策时间，耗时对比时所有变体使用相同设置。

要验证“全部能存住”的条件，保持 retention 关闭；启用 retention 后仍会按 Memory policy 执行
Drop，未写盘的 block 可能丢失。无限模式并不覆盖这个决定。正常无 Drop 时，已见数据不会消失，
策略间的 global miss 与 compute cost 应相同。首次访问依旧是冷 miss，不代表 Storage 预热。
适合用不同长度、有重复访问的输入验证命中效果；脚本默认等长合成数据主要用于运行性能检查。
无限模式仍保留所有已写盘节点，进程内存会随唯一 block 数增长。

### Context-aware Memory LRU

`context_lru` 复用 indexed LRU 的 segment 索引、拓扑更新、storage-hit admission 和
retention 动作判断，使用单组全局精确候选选择：

1. 按 segment 最近访问序号从老到新遍历，只纳入没有 Memory 驻留后代段的合法 leaf segment。
2. 累计候选段的 **Memory 驻留 block 数**，达到 `alpha × Memory 容量 block 数` 后停止。
   至少纳入一段；跨过预算的最后一段完整纳入；合法候选不足时使用全部候选。
3. 按 `(endpoint 深度, 段内 Memory 驻留 block 数, 最近访问序号, endpoint ID)` 升序选择 victim。
   根下第一个 block 的深度为 1；深度沿全局树父链计算，包括非 Memory 驻留前缀。

`alpha` 范围为 `(0, 1]`，默认 `0.01`，不接受 NaN 或无穷值。预算以配置容量为基准，
不是候选总容量。较小 alpha 更接近 LRU；若最老的一段已经达到预算，本次只考虑该段。
`groups=1`、`sampled_groups=0`、`workers=1` 是此 policy 的固定约束，seed 不参与选择。

Python 配置：

```python
memory_policy = MemoryPolicyConfig(
    kind="context_lru", alpha=0.01, max_eviction_blocks=64,
)
```

使用配置文件时，修改 `example/.env`（完整模板为 `example/.env.example`）：

```dotenv
DWPDSIM_MEMORY_POLICY=context_lru
DWPDSIM_MEMORY_ALPHA=0.01
DWPDSIM_MEMORY_MAX_EVICTION_BLOCKS=64
DWPDSIM_MEMORY_RETENTION_NS=
```

`context_lru` 的 Drop 和 Dump 都只处理选中的当前 segment，不向父段回收。
`max_eviction_blocks` 是两种动作共用的段内上限：从 endpoint 向段首选取最多 N 个 Memory
驻留 block，保留其余前缀；提交仍按从前到后的顺序执行。Storage-only block 不占用上限。
参数为正整数，默认 `None`（环境变量留空），表示回收当前段全部 Memory 驻留。
即使上限未用满，也不继续到父段。Drop 丢弃所选 Memory 副本；Dump 仅写入其中没有 Storage
副本的 block 后释放所选 Memory 副本。保留前缀不会因为本次部分淘汰被写盘或移除。

该上限只影响 `context_lru`，并不改变 alpha 候选预算或深度/段长排序；候选段长仍按完整段的
Memory 驻留数计算。`baseline_lru` / `indexed_lru` 保留原有整段和向父段 Dump 规则。
实验脚本可加 `--max-eviction-blocks 64`，只传给 context 变体；index 对照组保持原规则。
上限是每次淘汰决策的上限，一个长请求触发多次淘汰时，总淘汰量可以超过 N。
endpoint 深度近似重算 context 长度，不等于未来请求的完整长度；Memory 淘汰也可能保留
Storage 副本。策略效果需比较回放的 `accesses.compute_cost` 和 `accesses.global_misses`。
候选选择顺序扫描冷段索引，并沿父链计算各候选深度；较大 alpha 会增加决策开销。

实验结果与复现命令见[context_lru实验报告](report/context-lru-retention.md)。

### 自适应 retention 与淘汰粒度

以下参数仅用于 `context_lru`，默认关闭自适应，保持固定 retention / 粒度行为：

- `retention_growth_seconds_per_block`：非负系数 beta（秒/block），默认 0。
  有效 retention = `retention_ns + beta × 1e9 × 上次完成请求的新增 block 数`。
  使用非零 `affinity_id` 标识 session；新增长度为相邻请求 `max(0, N_current - N_previous)`。
  首次请求和 context 缩短的新增量为 0；affinity=0 不跟踪 session 增长。
  请求完成后才将新增量发布给本次触及且仍驻留 Memory 的 block；不会用当前未完成请求的增量
  影响当前淘汰。共享 block 采用最近访问它的请求数据。
- `eviction_gap_reference_ns`：正整数参考间隔，默认 `None` 关闭动态粒度。
  开启后，选择段内最新 Memory 成员最近两次 Memory 访问所属的不同请求之间的时间间隔 G，
  按 `floor(eviction_base_blocks × G / reference)` 计算粒度，限制在 `[1, max_eviction_blocks]`。
  这里 G 是历史间隔，不是当前空闲时间；同请求内重复 hash 不更新 G。首次没有间隔时使用基础粒度。
- `eviction_base_blocks`：动态模式的基础粒度，默认 64；动态模式仍要求显式设置正整数
  `max_eviction_blocks` 作为硬上限。Drop/Dump 使用同一动态粒度，仍只处理当前段尾部。

段的 retention 和粒度都由段内最近访问的 Memory block 对应的历史决定。
新加入的 block 没有历史访问间隔，因此使用基础粒度，即使所属 session 已经有多次请求。
block 被彻底剪枝时清除其历史；session 长度历史保留到本次模拟结束。

组合配置示例：

```dotenv
DWPDSIM_MEMORY_RETENTION_NS=60000000000
DWPDSIM_MEMORY_RETENTION_GROWTH_SECONDS_PER_BLOCK=1
DWPDSIM_MEMORY_EVICTION_GAP_REFERENCE_NS=60000000000
DWPDSIM_MEMORY_EVICTION_BASE_BLOCKS=64
DWPDSIM_MEMORY_MAX_EVICTION_BLOCKS=1024
```

只调整 retention：将 gap reference 留空，max blocks 设为固定值（例如64）。
只调整粒度：将增长系数设为0，retention 保持60000000000。

三阶段 sweep（输入为下述四列 canonical Parquet）：

```bash
uv run python benchmark/context_adaptive_sweep.py \
  --dataset build/trace-1-3-sweep/input.parquet \
  --output build/context-adaptive-trace-1-3 \
  --memory-blocks 16384 --retention-s 60 --alpha 0.01 \
  --base-blocks 64 --max-blocks 1024 \
  --betas 0,1,2,5 --gap-references-s 15,30,60,120,300 --jobs 6
```

脚本固定 8 MiB/block、infinite_storage、关闭回填，先跑 retention（固定粒度64），
再跑粒度（固定 retention 60s），最后组合前两阶段各自命中率最高的两个非零 beta / reference。
同时运行 retention 关闭的 baseline；输出每组配置和指标、`results.csv`、`report.md`
及源码/输入校验信息 `provenance.json`。组合选择使用同一 trace，属于参数探索。

扩大 retention / beta 范围时，传入 `--retentions-s 60,120,300`
和 `--betas 0,5,10,20`。此模式运行完整交叉组合及 retention 关闭的 baseline，
固定粒度为 `--base-blocks`，跳过动态粒度阶段；使用不同的 `--output` 保存结果。


安装本地 Parquet 输入依赖：

```bash
python3 -m pip install -e '.[dev,input]'
```

输入四列为 `timestamp_ns:uint64`、`request_id:uint64`、`affinity_id:uint64` 和
`hash_ids:list<uint64>`；保持请求与路径原始顺序，不 shuffle 或去重：

```python
from dwpdsim import InputConfig, parquet_batches, replay_batches

input_config = InputConfig(
    batch_requests=1024,
    batch_hashes=262144,
    max_request_hashes=1048576,
    queue_batches=3,
    inflight_bytes=64 * 1024 * 1024,
    prefetch=True,
)
# simulator 已按上面的 SimulationConfig 创建；按显式分片顺序消费，EOF 后自动 finish。
input_metrics = replay_batches(
    simulator,
    parquet_batches(["part-000.parquet", "part-001.parquet"], input_config),
    input_config,
)
```

`prefetch=False` 是同步批量对照。预算覆盖构造中、队列中及消费中的自有 uint64 buffer，
不包含 Arrow/HF 解码 workspace；单请求不拆分，超长请求明确报错。
已存在的 HF Dataset/IterableDataset 使用 `huggingface_batches(dataset, input_config)`；
Hub 入口为 `hub_batches(repo, revision=固定版本, split="train", config=input_config)`，
需要额外安装 `python3 -m pip install -e '.[hub]'`。调用者记录数据来源、revision、分片顺序
及时间原点；适配器不隐式换算时间。

完整消融包含逐请求/批量/输入预取、单组索引/32 组/4 workers/采样 4 组及组合：

```bash
python3 benchmark/memory_ablation.py --output build/perf/synthetic --requests 20000 --repeats 3
python3 benchmark/memory_ablation.py --dataset input.parquet --output build/perf/real --repeats 3
# profile 单独跑，不把额外计时开销混入主时延结果。
python3 benchmark/memory_ablation.py --dataset input.parquet --output build/perf/profile \
    --profile --repeats 1 --variants batch,index,groups,workers
```

实验逐项启动独立进程，记录时延、RSS、窗口进度、Memory 工作量和 trace SHA-256，检查
精确模式业务结果相同及近似模式可重复；每次计算哈希后删除大 trace，保留 JSON 结果。
设计与实测结论见 [Memory 性能文档](.design/perf/batched-input-and-segment-eviction.md) 和
[消融报告](.design/perf/memory-ablation-report.md)。

### Memory Drop 诊断

`infinite_storage` 模式下，在首次请求前调用
`sim.enable_memory_diagnostics("evictions.csv")`，或给 sweep 添加 `--diagnostics`。
日志每行是一块实际被淘汰的 Memory block，包含触发访问的全局序号、时间、hash ID 和动作：
`D` 为 Drop，`W` 为本次实际写入，`C` 为已有存储副本而直接移除 Memory。
关闭日志是默认行为；`finish()` 关闭文件。

在输出目录保存 `selection.json`（待分析 job 对象列表，与 sweep 每组 JSON 的 `job` 一致），运行：

```bash
uv run python benchmark/memory_diagnosis.py \
  --dataset build/context-adaptive-full/input.parquet \
  --output build/context-diagnosis
```

分析需要对应的每组 JSON 和 `.evictions.csv`，输入 hash ID 应先映射为紧凑整数。
输出 `diagnosis.json` 与每组 `.diagnosis.json`，包含 Drop 后再次访问比例及间隔分布、
重复 Drop/写入次数，以及写入后未再访问的 block 数。下一次访问按全局访问序号查找，
包含同一请求内的后续访问；没有未来访问仅指本 trace 结束前，没有外推至窗口之外。
重复计数按 hash ID 跨剪枝/重建累计。

“写入后未再访问”衡量实际淘汰轨迹中的潜在可避免写入，不是重新选择所有候选的
全局最优策略，也不是部署策略的可实现收益。改变选择可能改变后续缓存状态。

### Wear-balanced Storage policy

`wear_balanced` 采用 `KVCache_Online_v5_Shadow_Pipeline_20260916` 的新放置公式、容量修正和
Shadow FTL 反馈。它始终消费已提交的 canonical I/O，在生成 trace 的过程中估计 NAND program、
GC 写入和寿命压力。完整 trace 仍由 MQSim 独立回放；MQSim 结果不反馈到本次生成过程。
`adaptive_endurance` 保留原有行为。两者均可与 `context_lru` 组合。

#### 算法和时间顺序

设 tier 的逻辑缓存容量为 C、擦写预算为 E、累计逻辑 program bytes 为 W：

- 有效寿命预算 `B = C × E / WA`；目标 TLC 比例 `t = B_TLC / (B_SLC + B_TLC)`。
- 实际 TLC 比例 `a = W_TLC / (W_SLC + W_TLC)`，包括 Dump 和迁移目的端写入。
- 首次写入 SLC；以后 `p_TLC = clamp(t + direct_gain × (t - a), 0, 0.75)`。
  affinity 的稳定 hash 决定选择，不是每次独立随机抽样。新公式从开始就启用，初始 WA=1。
- 当 `u_SLC > 0.9`、`u_TLC < 0.9` 且 `a < t`，令 `b = min(0.5, (u_SLC - 0.9)/0.08)`，
  再取 `p = max(p, min(0.75, t + b × (1 - t)))`。
- Shadow 按窗口计算 `delta(NAND program)/delta(host program)`，并以 alpha=0.25 平滑 WA。
  无 host 写入时保持 WA。迁移目的端写入计为 host program，GC 搬移单独计数。
- 压力 `P = 累计 Shadow NAND program bytes / (Shadow nominal capacity × E)`。
  有效迁移阈值为 `promotion_seconds × clamp(((P_TLC+1e-12)/(P_SLC+1e-12))^gain, 0.5, 4)`。
- 请求 gap/q95、idle 容量衰减、leaf 回收和迁移执行流程沿用 Algorithm2。

反馈窗口按从时间 0 开始的固定周期推进，包括空窗口和 `finish()` 补跑时间。
同一时间 t 的顺序为：结算 `[t-period,t)` 反馈 → 后台 tick → 请求。
历史 timer 始终先于之后的反馈执行；一条请求内所有访问在其时间戳上原子地归入同一窗口。
末尾不足一个完整窗口时仅保留最终 Shadow 计数，不额外运行控制器。

#### 在线 idle 与 ghost

`online_tuning=False`（默认）固定 idle multiplier；Shadow WA 和压力仍然更新。
设为 `True` 时，idle 从 24 开始，以 `hit/(hit+ghost_miss)` 更新 EMA，倍率限制在 `[24,64]`。
前四个窗口不调整；连续三个可信违约窗口才增大倍率。容量或 GC 压力高且连续八次违约时
允许降低倍率。样本不足时冻结 EMA/idle 并清空 streak，不沿用旧 streak 继续调整。

复用样本单位是 **KV block**，不是 token 或 NAND page。`min_reuse_blocks` 默认4096，
`reuse_ema_scale_blocks` 默认50000，`alpha=min(0.2,N/(N+scale))`；应按 workload 的
block 粒度配置。`reuse_loss_budget` 默认0.01，目标保有率为 `min(0.9999,1-budget+0.003)`。

只有 DRAM 和 Storage 都失去副本才记录 ghost；Memory Drop、Dump 拒绝以及 Storage Trim
均按最后副本判定。ghost 用全局 prefix-block hash 保存历史证据，不复制 Radix 拓扑。
请求中最深 ghost hash 对应的前缀长度超过连续命中长度的部分计为 ghost miss；新 suffix 不入分母。
记录在24小时后过期，定时反馈/请求会全局清理，重复淘汰刷新时间。ghost 并非无限历史 oracle。

`promotion_seconds` 始终是显式的基准，默认14400秒，不再保留源代码中无实际学习效果的
promotion 控制状态或隐式翻倍。采用8小时基准时显式设28800。固定模式 idle 默认32；
在线模式使用24作为初始值。direct bias 恒为零，因此没有额外参数或学习状态。

#### 配置

在完整的 `example/.env` 中设置（block 大小必须是 Shadow page 大小的整数倍）：

```dotenv
DWPDSIM_MEMORY_POLICY=context_lru
DWPDSIM_STORAGE_POLICY=wear_balanced
DWPDSIM_ONLINE_TUNING=true
DWPDSIM_FEEDBACK_PERIOD_NS=900000000000
DWPDSIM_PROMOTION_SECONDS=28800
DWPDSIM_SHADOW_PAGE_BYTES=4096
DWPDSIM_SHADOW_PAGES_PER_BLOCK=256
DWPDSIM_SHADOW_OVERPROVISIONING=0.07
DWPDSIM_SHADOW_SLC_NOMINAL_BYTES=0
DWPDSIM_SHADOW_TLC_NOMINAL_BYTES=0
DWPDSIM_SHADOW_SLC_PHYSICAL_BLOCKS=0
DWPDSIM_SHADOW_TLC_PHYSICAL_BLOCKS=0
DWPDSIM_REUSE_LOSS_BUDGET=0.01
DWPDSIM_MIN_REUSE_BLOCKS=4096
DWPDSIM_REUSE_EMA_SCALE_BLOCKS=50000
```

Shadow nominal bytes 为0时使用对应 Storage 逻辑容量；显式值必须页对齐且不小于该逻辑容量。
physical blocks 为0时，物理块数为 `ceil(nominal_bytes/page_bytes/pages_per_block/(1-OP))`；小池至少配置为
`stream_count+2` 个块以容纳写入前沿和GC备用块。模型采用每流前沿和 tier 内共享容量，
GC 按无效页最多、最后写入最早、擦除次数最少的顺序选择满块。
可通过 `shadow_slc_physical_blocks`/`shadow_tlc_physical_blocks` 指定精确物理块数，
用于 nominal 容量被独立覆盖的 profile；要求原始容量大于 nominal，并保留写入前沿和GC空间。
Shadow 几何是显式配置，不自动读取 MQSim XML；需要对齐时按实际设备 profile 填写，
记录中的物理块数包含取整和小池下限。不能将小池 smoke 结果外推到大容量设备。

Python 使用 `StoragePolicyConfig(kind="wear_balanced", online_tuning=True, ...)`；字段名为
上面环境变量去掉 `DWPDSIM_` 后的小写形式。旧固定 `slc_wa`/`tlc_wa` 构造参数和环境变量
已删除，不保留固定WA回退路径。

#### 输出与验证边界

- canonical CSV schema v4 不变，Memory 命中仍不产生 I/O。
- `<trace文件路径>.controller_windows.csv` 输出每个完整反馈窗口的复用样本、EMA、idle、
  有效迁移阈值、WA、压力和累计 host/GC program pages。
- `stats.algorithm` 中的 WA/`shadow_slc_pressure`/`shadow_tlc_pressure` 是最后一个完整窗口
  应用到策略的反馈，另有 `feedback_windows`、`reuse_retention_ema`、`learned_idle_multiplier`。
- `stats.shadow_ftl` 是包含末尾未满窗口写入的最终计数，记录几何、参数、host/GC program、
  擦除次数、总WA、最后窗口WA EMA、当前累计寿命压力、ghost数量和累计ghost miss。
- 固定WA压力指标 `estimated_slc_pressure`/`estimated_tlc_pressure` 已被 Shadow 压力替代。

Shadow 同步执行写入和GC，忽略READ，不模拟时延、队列、通道竞争或物理完成时间。
它提供模型估计，正式物理性能和磨损评估仍使用 MQSim；两者的WA可能不同。
GC 页表和 ghost 历史会增加内存开销；它们均在 C++ 中执行，没有逐块 Python 回调。

固定合成工作负载的开销检查：

```bash
uv run python benchmark/shadow_overhead.py --requests 20000 --output build/shadow-overhead
```

脚本使用128/256页的小池、每池2个stream、25%预留空间，分别运行原策略、Shadow固定超参数、
Shadow在线调参。各策略产生的I/O可能不同，耗时不是等工作量加速比。小池多流的未满写入块
会占用容量；预留空间不足时模型明确报错，不跳过事件或回退固定WA。
