# MemoryPolicy 性能实现与消融报告

日期：2026-09-07。分支：`perf/memory-segment-lru`。
本文记录空闲保留时间功能加入前的消融；对应当前配置 `retention_ns=None`，
不包含启用保留时间后 Drop 行为的效果。
基线：`main == origin/main == 82fbcbd3a5c477665f236042f2bf85515ad6f795`（创建分支前已 fetch）。

## 结论

- 有明确收益的是 **Memory segment 增量索引**。在 Memory=4096 blocks 的实验中，
  相对原始批量 LRU，合成数据中位时延加速 **6.06×**，
  Mooncake 加速 **15.96×**；精确模式的 metrics 和 trace 完全一致。
- 分组本身没有稳定额外收益。全局单组有序索引已经使一次选择只需检查少量候选；
  32 组增加组头查询和归并工作。
- 当前每次决策唤醒 4 个常驻 worker 的实现是**负收益**。它确实多线程执行，但同步成本
  大于候选查询成本，默认保持单线程。不把这个结论推广为所有批量并行方案都无效。
- 输入 batching/prefetch 在这几组工作负载中没有稳定、足以超过重复波动的收益。
  它们建立了统一输入与有界队列入口，但不是当前的主要提速来源。
- 采样近似会改变模拟结果，当前未显示相对单组精确索引的稳定优势，不建议作为默认。
- 小 Memory=256 blocks 时，原始扫描已经便宜，增量维护基本抵消了候选查询节省。
  **不能把大 Memory 场景的加速比套到所有配置。**

## 实现范围

仅迭代 MemoryPolicy，StoragePolicy 的接口、实现和通知协议均未修改。
Simulator 增加 Memory 专用的创建/剪枝回调与可选计时，原有 Dump、Storage placement、
容量回收、迁移、后台 tick 和 canonical trace 语义保持原样。

新增 `MemoryPolicyConfig(kind="indexed_lru")`，支持独立配置 `groups`、`sampled_groups`、
`workers`、`seed`、`profile`。原始 `baseline_lru` 仍为默认和对照；只增加工作量计数及共同
调用边界，不修改其选择算法。实验基线是同一 Release binary 内的原始算法，并非另一个
未经统计适配的历史 binary。

索引保存 Memory resident 的逻辑访问序号、segment 有序成员、组内有序比较键，以及
Memory resident 祖先的子树计数。Accessed/Inserted/Removed、分裂/合并/延长/缩短同步维护。
查询期间暂停逻辑修改，worker 只读取 Memory 派生状态；结果不跨决策缓存，不需要异步
版本重试。Memory 空段及时删除派生记录，不累积历史候选。

输入提供本地 Parquet、HF Dataset/IterableDataset、固定 revision 的 Hub 适配器，以及
同步/预取可切换的 replay 入口。四个业务字段保持 `timestamp_ns, request_id, affinity_id,
hash_ids`，完整请求不拆分，不去重、不 shuffle。队列预算覆盖自有 uint64 buffers，
**不包含 Arrow/HF decoder workspace 或整个 Simulator RSS**。

## 实验方法与边界

- 机器：Intel Core Ultra 7 155H，Linux，固定 affinity 到六个不同性能核的逻辑 CPU
  `0,1,3,6,8,10`，避免迁移到能效核；没有锁频，也不是独占机器。
- 每个配置独立进程；主时延每项 3 次，固定 seed=42 并打乱测试顺序。
  表格为 **中位数 [最小值, 最大值]，单位秒**。桌面负载和动态频率仍有明显波动，
  不以几个百分点的差异宣称确定加速。
- 主时延关闭逐次 Memory 计时；另做一次 profile 回放拆分成本，不能将 profile 的数值
  与主表中位数直接相加。计数、少量每 batch 窗口记录及真实 trace 写出均保留。
- elapsed 从 Simulator 构造完成后开始，到输入耗尽并 finish 结束；包含首次 decoder
  初始化、组批、消费、trace flush，排除进程启动、Simulator 构造和最后的 trace 哈希计算。
- 全部使用相同未修改的 `baseline_fixed_lru` StoragePolicy，固定写 TLC；无下游 MQSim
  执行，因此报告的是 Simulator CPU/输入/输出时延，不是 SSD 延迟或 DWPD。
- 前期环境安装与未固定 affinity 的预实验不纳入主表；Mooncake 的早期环境未完成记录
  已重新测量。RSS 使用独立 profile 的 `/proc/self/status VmHWM`；早期主实验中的
  `getrusage` 高水位可能继承父进程，不用于方案间内存比较。
- 数据、配置、源码文件 hash、原始时延及结果 hash 保存在
  `build/perf/memory-ablation-results.json`，完整窗口 JSON 在对应 `build/perf/` 目录。
  这些机器生成的数据仅保留在本地，不纳入版本控制；复现实验命令见文末。

| 场景 | 请求数 | block accesses | Memory blocks | SLC/TLC blocks | 存储状态 |
| --- | ---: | ---: | ---: | --- | --- |
| 合成，路径长度 16 | 20,000 | 320,000 | 4096 | 1,048,576 / 7,340,032 | TLC 未满 |
| Mooncake | 23,608 | 409,356 | 4096 | 1,048,576 / 7,340,032 | TLC 未满 |
| 合成，小 Memory 对照 | 6,000 | 96,000 | 256 | 128 / 7,340,032 | TLC 未满 |
| 同一合成数据，容量压力 | 6,000 | 96,000 | 256 | 128 / 1024 | TLC 满并持续回收 |

block=8 MiB，前两组总存储容量为 64 TiB、Memory=32 GiB；这只是配置容量，并未填满
64 TiB。合成和 Mooncake 最终分别保留 100,012、183,166 个全局节点，不能外推为已验证
800 万驻留 block 的管理性能。容量压力组有 84,084 个前台容量回收 block。

Mooncake 来自本地 `datasets/mooncake-traces/mooncake/train-00000-of-00001.parquet`。
使用完整文件，timestamp 按其说明从相对毫秒乘 1,000,000 转为 ns；无 request_id/affinity_id，
显式使用行号补齐，hash_ids 不变，不排序。转换脚本及来源 hash 保留。
**尚未取得此前 14 万请求、3 亿 events 的原始运行配置和数据，以上不是该次运行的复现。**

## 主消融：大 Memory

| 配置 | 合成数据，秒 | Mooncake，秒 |
| --- | --- | --- |
| 原始 LRU，逐请求调用 | 9.151 [8.376, 9.625] | 26.468 [26.224, 28.232] |
| 原始 LRU，批量调用 | 9.411 [8.833, 10.596] | 29.003 [26.235, 30.335] |
| 原始 LRU + 输入预取 | 8.833 [8.172, 8.894] | 27.994 [26.210, 28.238] |
| 单组精确索引 | 1.554 [1.104, 1.889] | 1.817 [1.622, 2.275] |
| 32 组精确索引，单线程 | 1.731 [1.513, 1.874] | 1.747 [1.650, 2.277] |
| 32 组精确索引，4 workers | 2.354 [2.297, 2.878] | 2.718 [2.680, 3.556] |
| 32 组采样 4 组，单线程 | 1.606 [1.577, 1.622] | 1.684 [1.595, 1.988] |
| 采样 4 组，4 workers | 2.111 [1.918, 2.905] | 2.695 [2.642, 3.705] |
| 单组精确索引 + 输入预取 | 1.516 [1.420, 1.670] | 2.038 [1.693, 2.376] |

逐请求和批量对照都从同一 Parquet 批量解码器获取数据；逐请求模式额外执行逐行 Python/C++
调用。它隔离的是调用粒度，不表示同时比较不同原始文件格式。输入队列和 Memory worker
是两个独立开关，不能将两者一起打开的结果都归功于多线程候选查询。

## Memory 小容量与目标存储满的对照

| 配置 | Memory=256，TLC 未满 | 同一输入/Memory，TLC 满 |
| --- | --- | --- |
| 原始 LRU，批量调用 | 0.549 [0.546, 0.553] | 2.723 [2.502, 2.774] |
| 单组精确索引 | 0.532 [0.530, 0.536] | 2.541 [2.528, 2.713] |

小 Memory 未满存储对照中，单组索引的中位数改善约
3.0%；容量压力组约
6.7%，但后者重复范围明显重叠。
只降低 TLC 容量后，整个模拟的耗时大幅增加；这证明不能只观察 Memory 的局部选择耗时。
容量压力组的 4-worker 中位数为 4.868 秒，比单线程更慢。

## 时延拆分：独立 profile 运行

下表单位秒。Memory 决策是 `MemoryPolicy::evict()` 的调用时间；维护包括 Memory 通知
和拓扑回调，包含序号/成员/组索引/祖先计数维护，不包含全部树和 Storage 操作。
其余时间没有进一步按 Storage/trace 分开，不能据此给单个 StoragePolicy 函数定量归因。

| 场景/配置 | 总时延 | Memory 决策 | Memory 维护 | 输入构造 |
| --- | ---: | ---: | ---: | ---: |
| 合成/batch | 9.451 | 8.370 | 0.082 | 0.366 |
| 合成/index | 1.851 | 0.009 | 0.699 | 0.399 |
| 合成/groups | 1.645 | 0.043 | 0.570 | 0.368 |
| 合成/workers | 2.684 | 0.735 | 0.734 | 0.387 |
| Mooncake/batch | 28.748 | 27.523 | 0.089 | 0.449 |
| Mooncake/index | 2.307 | 0.012 | 1.143 | 0.387 |
| Mooncake/groups | 2.210 | 0.055 | 1.079 | 0.333 |
| Mooncake/workers | 3.870 | 1.138 | 1.372 | 0.377 |
| 小 Memory 未满/batch | 0.577 | 0.158 | 0.019 | 0.259 |
| 小 Memory 未满/index | 0.534 | 0.001 | 0.118 | 0.263 |
| 小 Memory 满/batch | 1.865 | 0.112 | 0.023 | 0.251 |
| 小 Memory 满/index | 1.852 | 0.001 | 0.135 | 0.246 |

主要结论是用较小的持续维护成本替代昂贵的全量扫描。小 Memory 下原始选择已经很便宜，
这个交换就接近收支相抵；在满容量组的 profile 中，选择节省与维护增加几乎相等。

Mooncake 中 32 组单线程的选择耗时约 0.055 秒，4 workers 约 1.138 秒，共 27,328 轮
worker 同步。这里并没有足够重的独立候选任务支撑线程并行，不能靠增加线程修复。

| 场景 | 原始检查 block 数 | 单组检查 segment 候选数 | 32 组检查候选数 |
| --- | ---: | ---: | ---: |
| 合成 | 81,342,464 | 23,188 | 759,722 |
| Mooncake | 111,935,488 | 39,762 | 1,187,268 |

计数对象不同，表格反映的是算法工作量转换，不能将计数比直接当成时延加速比。
索引后下一项值得考虑的成本是祖先更新：Mooncake 中达到 16,021,103 次，当前复杂度
与路径深度成正比；分裂只局部移动成员，没有重建整棵树。

## 近似选择的结果偏差

| 场景 | 精确 Memory hit rate | 采样 Memory hit rate | 精确 events | 采样 events |
| --- | ---: | ---: | ---: | ---: |
| 合成 | 0.183725 | 0.184275 | 260,156 | 259,888 |
| Mooncake | 0.359000 | 0.361336 | 259,363 | 258,539 |

采样的变化不必然更差，也不必然更好。每个近似配置重复运行、以及相同 seed 下单线程/
多线程的 trace hash 一致，但与精确模式不同，因此它是策略变体，不是等价提速。

## 内存与随时间变化

独立 profile 的进程峰值 RSS：合成约 151 MiB，Mooncake 约 169 MiB，容量压力约 130 MiB；
精确索引与原版差异在约 1 MiB 内，这个小工作集不足以证明大规模元数据没有成本。
索引中的 Memory resident 数与实际驻留数一致，空段删除；未维护全局 Storage 候选副本。

窗口记录显示优化后仍有前后段每 block 时延差异，不能宣称消除了所有随时间下降的现象。
请求路径、命中/I/O 比例、全局树和原有 Storage 状态、动态 CPU 频率均可能影响剩余时间。
原始 3 亿 events 工作负载仍需单独测量；本轮精确优化不会减少其必须产生的 events。

## 验证与使用建议

- Python：64 项测试通过，包括精确 trace/metrics 对照、全部现有 Storage 算法组合、
  空路径、同时间请求、重复 hash、全 uint64 范围、HF Dataset/IterableDataset、
  有界队列、生产/消费错误、超长请求及不拆请求。
- Debug CTest：3/3 通过；ASan+UBSan CTest：3/3 通过，包括随机树生命周期与线程结果对照。
- Ruff 与 diff 格式检查通过；StoragePolicy 源码和接口 diff 为空。
- 实验中所有精确配置与基线 trace SHA-256 和业务 metrics 一致，近似配置自身可重复。

建议先使用 `MemoryPolicyConfig(kind="indexed_lru", groups=1, workers=1)` 验证实际大 Memory
工作负载；保留默认 baseline 作为回退和对照。暂不默认启用分组采样、worker 或输入预取。
输入规范/队列可以先作为数据接入能力使用，启用预取前单独测量。

## 复现

在仓库根目录安装并重建当前扩展：

```bash
python3 -m pip install -e '.[dev,input,hub]'
# 以下命令用于本机 Linux；其他机器按实际 CPU 拓扑调整或移除 taskset。
taskset -c 0,1,3,6,8,10 python3 benchmark/memory_ablation.py \
    --output build/perf/synthetic-final --requests 20000 --repeats 3

python3 benchmark/prepare_mooncake.py \
    ../datasets/mooncake-traces/mooncake/train-00000-of-00001.parquet \
    build/perf/mooncake.parquet
taskset -c 0,1,3,6,8,10 python3 benchmark/memory_ablation.py \
    --dataset build/perf/mooncake.parquet --output build/perf/mooncake-pinned --repeats 3

taskset -c 0,1,3,6,8,10 python3 benchmark/memory_ablation.py \
    --output build/perf/saturated --requests 6000 --memory-blocks 256 \
    --slc-blocks 128 --tlc-blocks 1024 --repeats 3 \
    --variants batch,index,groups,workers,index_queue
taskset -c 0,1,3,6,8,10 python3 benchmark/memory_ablation.py \
    --dataset build/perf/saturated/workload.parquet --output build/perf/small-memory-open \
    --memory-blocks 256 --slc-blocks 128 --tlc-blocks 7340032 --repeats 3 --variants batch,index

# 独立 profile，保持 dataset、Memory、SLC/TLC 参数与对应主实验相同。
taskset -c 0,1,3,6,8,10 python3 benchmark/memory_ablation.py \
    --dataset build/perf/mooncake.parquet --output build/perf/mooncake-profile \
    --profile --repeats 1 --variants batch,index,groups,workers
```

每个目录保存 `provenance.json`、每次实验的 JSON、`summary.json`；trace 在哈希后删除，
如需观察业务 trace，使用普通 Simulator API 回放同一配置。对照设计见
[批量输入与 Memory segment 设计](batched-input-and-segment-eviction.md)。
