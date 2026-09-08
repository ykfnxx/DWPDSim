# RR Storage 精确索引与消融实验

日期：2026-09-08。分支：`perf/rr-storage-lru`。
原始基线：`1beb530b99f80f1c528537790064bd05d5806e3c`，独立构建 wheel。

## 结论

默认启用精确 segment 索引，关闭子树计数；保留 scan、fused 和 counts 供对照。
写满场景的主表中，合成与 Mooncake 相对原提交分别约 5.02×、8.93×；
未满容量和长 segment hit 场景没有明确收益。所有已测变体的业务 metrics 和完整 trace 一致。
子树计数的初测趋势经复测与 profile 未得到支持，因此没有作为默认。

## 行为与实现

优化对象为 `WearShareRoundRobinStoragePolicy` 的 CPU 查询和状态维护。
保持一个全局 RadixTree 和串行 Simulator；没有新增线程、分组采样或近似淘汰。
其他 StoragePolicy 继续使用原有状态与查询算法。

RR 的 placement 保持原式：比较 `(slc_program_bytes + write_bytes) / slc_host_share`
与 `(tlc_program_bytes + write_bytes) / (1 - slc_host_share)`，相等选 SLC。
选中 tier 后，以已提交的 dump segment 数模 stream 数轮转。容量不足时不切换 tier，
按原来的逻辑容量上限和 protected 规则执行回收或拒绝。

候选仍须是全局 Storage leaf：endpoint 下不能存在 SLC 或 TLC 任意 Storage 后代，
且该段在目标 tier 有驻留 block，整段不与本次 protected nodes 相交。
排序键严格为 `(该段目标 tier 的最大 Storage 时间, endpoint)`。
每次只返回一个 trim intent，由 Simulator 执行并更新状态，再选择下一次 victim。

### Storage 时间归属

Simulator 在 `DumpWriteCommitted` 和 `StorageAccessCommitted` 通知前，更新
Node 的 `storage_last_access_timestamp_ns`。dump 只初始化实际写入的 block；
Storage hit 刷新当前段在命中 tier 的所有驻留 block；Memory hit 不刷新此字段。
这个字段与 `last_access_timestamp_ns` 分开，删除并重新创建节点时重新初始化。

`scan`、`fused` 保留原 `StoragePolicyState` 的逐 block map。
`indexed` 默认不维护这份 map，而是从树上读取时间来维护 segment 聚合索引。
开启校验时额外维护 map，作为独立的旧算法 oracle。
其他 StoragePolicy 仍维护自己的原有状态，不读取新字段。

### 三个查询路径

- `scan`：原来的全 Storage 条目扫描、逐 block 定位 endpoint、候选去重和多次段解析。
  同一 binary 中增加工作量计数；共同的节点时间和拓扑通知开销也存在。
- `fused`：仍扫描同一份 map；第一次遇到目标 tier 成员时解析整段，将成员标记为已访问，
  同一次遍历完成 protected 检查和最大时间计算，再检查后代。减少重复定位与临时 vector。
  临时 visited 集合按成员保存，可能比原 endpoint 集合更大；不保证所有场景都有收益。
- `indexed`：每个 tier 一个 `std::set<(last_ns, endpoint)>`，保存有该 tier 驻留的全部段。
  查询从最冷候选开始，逐个检查后代和 protected 条件，返回第一个合格候选。
  没有额外维护“当前可淘汰集合”，因此不会因后代删除而漏掉原来不合格的冷候选。

索引只增加 endpoint -> 段聚合信息、两个有序集合，以及可选的子树计数；
没有第二份 parent/children 拓扑、逐 block 时间表或 block -> segment 映射。
局部更新仍使用 RadixTree 的现有 `segment_leaf_for` 和 `resolve_segment`。

### 拓扑维护

StoragePolicy 增加默认空实现的节点创建/裁剪回调，由 Simulator 在拓扑变更后调用。

- 延长：旧 endpoint 变成内部节点时，删除旧键并重算新 endpoint。
- 分裂：父节点从一个孩子变成两个孩子时，分别重算前缀与原后缀。
- 缩短：删除末端空节点后，将有驻留的前缀重算到新的 endpoint。
- 合并：父节点只剩一个孩子时，删除前缀旧键并重算合并后的段。
- Storage 提交：重算当前段的两 tier 驻留量与最大时间。

容量回收可能在 dump placement 和 dump commit 之间合并 segment。
因此 commit 使用 `tree.segment_leaf_for(mutation.segment_endpoint)` 取得当前边界，
不能把 dump 开始时的 endpoint 直接作为新的索引键。

所有局部聚合从 Node 时间重算，第一版不采用延迟时间标记。
因此分裂后的两部分能分别恢复正确时间；长段的拓扑维护和 Storage hit 仍有成员遍历成本。

### 子树计数消融

`rr_subtree_counts=True` 时，dump/trim 沿每个变更 block 的父链增减 Storage 驻留计数。
查询通过 endpoint 子树计数与 endpoint 自身驻留贡献比较，判断是否存在 Storage 后代。
计数覆盖 SLC + TLC，零计数记录删除；纯 Memory 节点创建不增加驻留数。

False 时不维护计数，查询使用原 `has_storage_descendant`。
两种方式返回相同结果；计数把查询的树搜索换成写入/删除时的祖先维护，收益由实验决定。

## 配置与观测

```python
storage_policy = StoragePolicyConfig(
    kind="wear_share_round_robin",
    rr_victim_search="indexed",  # scan / fused / indexed
    rr_subtree_counts=False,
    rr_verify_victims=False,
    rr_profile=False,
)
```

示例 `.env` 对应 `DWPDSIM_RR_VICTIM_SEARCH`、`DWPDSIM_RR_SUBTREE_COUNTS`、
`DWPDSIM_RR_VERIFY_VICTIMS`、`DWPDSIM_RR_PROFILE`。旧配置未填写时使用上述默认值。
Python API 本身不读取 `.env`。

`sim.storage_performance()` 独立于业务 metrics，包含查询数、全扫描条目数、
候选检查数、段重算/遍历成员数、祖先计数更新数、校验次数和当前索引大小。
`scan` 只统计全扫描条目；它的候选数和成员数没有插入计数，返回 0 不表示没有这些工作。
`fused` 的成员计数属于查询；`indexed` 的成员计数属于维护，不能当作同一种查询计数比较。

`rr_profile=True` 时，`decision_ns` 覆盖 RR `reclaim_for`，`maintenance_ns` 覆盖
RR commit 与拓扑回调；不包含 Simulator 的段收集、Node 时间写入、I/O 输出或 placement。
开启 `rr_verify_victims` 会把旧扫描与 oracle 维护也计入时间，因此不能用于性能主表。

## 验证方法

- 同一状态逐次比较优化查询与旧 `lru_leaf` 的 endpoint、时间及无候选结果，校验在 Release
  也执行，不依赖 assert，不在失败后回退继续模拟。
- 独立回放比较完整 metrics 和 trace 字节/哈希；每个消融配置每次重复都校验。
- 随机但固定 seed 的分叉、延长、缩短、合并、等时间戳、重复 hash、双 tier、不同 stream 数、
  hit promotion/bypass、protected 阻塞与连续回收场景覆盖于 Python/C++ 集成测试。
- C++ 直接验证 Storage hit 刷新同段其他 block，而随后 Memory hit 不修改 Storage 时间。

## 实验方法

Intel Core Ultra 7 155H，固定到逻辑 CPU 0；单线程回放。桌面机器非独占、未锁频。
每项独立进程，seed=42 打乱顺序，主表 3 次重复，报告中位数和范围。
包含 Parquet 首次解码、组批、每 batch 的窗口记录、真实 trace 写出及 finish；
不包含进程启动、Simulator 构造、trace 哈希。没有 MQSim 执行，测量的是模拟器吞吐。

Memory 固定 `indexed_lru` 单组单线程，无 retention；block=8 MiB，Memory=256 blocks。
SLC/TLC stream 数固定为 3/2。主测量关闭 profile 和 verify，另跑 profile 拆解成本。
`original` 使用原提交独立安装包，记录 extension 路径及 SHA256，避免 editable hook 导入当前代码。
`scan` 与其他新路径使用同一 extension；`index` 表示 indexed + counts=False，
`counts` 表示 indexed + counts=True。

原始 JSON、trace hash、源码 hash、输入 hash 和窗口数据保存在忽略目录 `build/perf/rr-*`，
不提交生成的指标或数据集。报告中的时延只对应所列工作负载，不能外推到 64 TiB 写满规模，
也没有重现此前 14 万请求、3 亿 event 的原始输入。

## 主测量结果

时延单位秒，格式为中位数 [最小值, 最大值]。

| 场景 | 请求数 / block accesses | SLC / TLC blocks |
| --- | --- | --- |
| 合成，两 tier 写满 | 6,000 / 96,000 | 1,024 / 2,048 |
| 合成，仅 SLC 写满 | 6,000 / 96,000 | 128 / 7,340,032 |
| 合成，64 TiB 未满 | 6,000 / 96,000 | 1,048,576 / 7,340,032 |
| Mooncake，两 tier 写满 | 23,608 / 409,356 | 1,024 / 2,048 |
| 长段命中，64 TiB 未满 | 4,000 / 256,000 | 1,048,576 / 7,340,032 |

长段场景路径长 64，Storage hit 不提升到 Memory；其他合成场景路径长 16、允许提升。
64 TiB 场景只是配置总容量，数据远未填满；写满场景使用缩小容量触发持续回收。

| 场景 | original | scan | fused | index | counts |
| --- | --- | --- | --- | --- | --- |
| 合成，两 tier 写满 | 5.355 [4.693, 5.629] | 4.181 [3.447, 4.817] | 3.011 [2.887, 3.084] | 1.066 [0.838, 1.098] | 0.697 [0.671, 0.713] |
| 合成，仅 SLC 写满 | 1.183 [0.988, 1.274] | 0.990 [0.980, 0.990] | 1.012 [1.004, 1.019] | 0.640 [0.606, 0.913] | 0.627 [0.626, 0.637] |
| 合成，64 TiB 未满 | 0.588 [0.576, 0.588] | 0.589 [0.586, 0.599] | 0.583 [0.582, 0.610] | 0.591 [0.586, 0.607] | 0.609 [0.608, 0.614] |
| Mooncake，两 tier 写满 | 14.813 [14.332, 17.817] | 15.811 [15.663, 17.961] | 13.118 [11.937, 13.721] | 1.659 [1.657, 1.667] | 2.725 [2.711, 2.890] |
| 长段命中，64 TiB 未满 | 1.629 [1.597, 2.164] | 1.726 [1.659, 2.409] | 1.699 [1.655, 1.780] | 1.689 [1.572, 2.184] | 1.832 [1.821, 1.932] |

### 哪些修改有效

- **精确 segment 索引是主要收益来源。** 两 tier 写满时，合成主表 original/index 为
  **5.02×**，Mooncake 为 **8.93×**。同一 binary 的 scan/index 分别为 3.92×、9.53×。
  这些比值是本表中位数之比，不代表所有输入或实际 SSD 时延。
- **合并遍历有局部收益，但没有解决全扫描。** 合成写满 scan/fused 约 1.39×，
  Mooncake 约 1.21×；仅 SLC 写满时接近持平。大量 TLC 条目仍被全量枚举，
  临时成员集合也有代价。
- **子树计数默认关闭。** 首组合成主表中 counts 看似较快，但后续重复和 profile
  不支持稳定的额外收益，不能根据该单组结果选为默认。详见下表。
- **Storage 未满和长段命中没有明确提速。** 此时没有 victim 查询，维护索引无法消除
  Simulator 的整段 hit 时间更新；长段场景 indexed 累计重算约 620 万成员。
  这部分优化仍需后续单独研究。
- original 与 scan 的时延差异不能解释为算法收益：两者算法相同，共同接口、
  计数及 Node 布局有变化，而且非独占机器存在明显频率/桌面负载波动。
  将两种基线同时列出，避免把原始版本与新 binary 的差异都归因于索引。

### 子树计数的补充对照

针对首组趋势不一致，独立进程打乱顺序再各跑 5 次，代码和显式配置不变：

| 场景 | index | counts |
| --- | --- | --- |
| 合成写满 | 0.611 [0.604, 0.628] | 0.670 [0.663, 0.704] |
| Mooncake 写满 | 2.162 [1.735, 2.382] | 2.262 [2.255, 2.782] |

合成复测 counts 慢约 9.8%；Mooncake 两组范围有交叠，不能宣称几个百分点的确定差异。
但计数维护成本在下述 profile 中明确存在，而查询节省很小，因此默认 False。
主测量和复测的绝对时延也有明显变化，不跨轮拼接加速比。

### 查询与维护的单独计时

下表每项独立跑 1 次，打开 profile、关闭 verify。单位秒。
计时会扰动执行，不能直接与主表中位数相加。

| 场景 | 实现 | 总时延 | RR 查询 | RR 维护 |
| --- | --- | --- | --- | --- |
| 合成写满 | scan | 4.196 | 3.2515 | 0.0449 |
| 合成写满 | fused | 3.728 | 2.8140 | 0.0434 |
| 合成写满 | index | 0.928 | 0.0051 | 0.0434 |
| 合成写满 | counts | 1.050 | 0.0054 | 0.1226 |
| Mooncake 写满 | scan | 16.108 | 14.0885 | 0.1042 |
| Mooncake 写满 | fused | 11.577 | 9.3690 | 0.1099 |
| Mooncake 写满 | index | 2.292 | 0.0321 | 0.1042 |
| Mooncake 写满 | counts | 2.360 | 0.0249 | 0.6593 |

Mooncake 的查询从约 14.09 s 降为 0.032 s；counts 进一步只节省约 0.007 s，
维护却从约 0.104 s 增至 0.659 s，发生 1,629.9 万次祖先计数更新。

确定性工作量也支持索引收益：

| 场景 | victim 查询次数 | scan 枚举条目 | fused 候选 | index 候选 |
| --- | ---: | ---: | ---: | ---: |
| 合成写满 | 5,475 | 16,386,412 | 736,909 | 6,135 |
| Mooncake 写满 | 23,304 | 68,896,040 | 3,072,353 | 28,117 |

indexed 的平均候选检查数约为每次 1.12 / 1.21 个，仍然是精确全局顺序。
仅 SLC 写满时为约 9.66 个，说明 protected 或非 leaf 冷候选仍可能拖长查询；
最坏情况下可以扫描全部候选，未声称每次都是 O(1)。

### RSS 与规模边界

RSS 使用独立进程 `/proc/self/status` 的 VmHWM，包含解释器、Arrow 和整个 Simulator。
它是整体高水位，不能把小幅差异全部归因于索引。

| 场景 | original MiB | index MiB | counts MiB |
| --- | ---: | ---: | ---: |
| 合成，两 tier 写满 | 130.2 | 130.6 | 130.7 |
| 合成，仅 SLC 写满 | 135.6 | 134.1 | 135.4 |
| 合成，64 TiB 未满 | 138.7 | 136.3 | 138.3 |
| Mooncake，两 tier 写满 | 133.3 | 133.1 | 133.3 |
| 长段命中，64 TiB 未满 | 168.2 | 160.6 | 165.8 |

Node 增加一个 uint64 Storage 时间字段；indexed 不再分配原 RR 的逐 resident Entry map，
但增加段聚合和有序索引。开启 counts 还会维护所有 Storage resident 祖先的计数。
百万级驻留量下的索引/拓扑维护内存开销没有在本轮量化，不能据此承诺 64 TiB 满容量 RSS。

## 复现命令

从仓库根目录运行。这里以当前环境的 `build/test-venv/bin/python` 为例：

```bash
build/test-venv/bin/python -m pip install -e '.[dev,input]'

# 在独立目录构建原提交，不能用当前 editable extension 充当 original。
mkdir -p build/rr-original-src
git archive 1beb530 | tar -x -C build/rr-original-src
build/test-venv/bin/python -m pip install --no-deps \
  --target build/rr-original-package ./build/rr-original-src

# 主表：合成，两 tier 写满。
taskset -c 0 build/test-venv/bin/python benchmark/rr_ablation.py \
  --output build/perf/rr-saturated --requests 6000 \
  --memory-blocks 256 --slc-blocks 1024 --tlc-blocks 2048 \
  --variants original,scan,fused,index,counts --original-package build/rr-original-package

# 相同输入，容量未满和只有 SLC 写满。
taskset -c 0 build/test-venv/bin/python benchmark/rr_ablation.py \
  --output build/perf/rr-open --dataset build/perf/rr-saturated/workload.parquet \
  --slc-blocks 1048576 --tlc-blocks 7340032 \
  --variants original,scan,fused,index,counts --original-package build/rr-original-package
taskset -c 0 build/test-venv/bin/python benchmark/rr_ablation.py \
  --output build/perf/rr-slc-full --dataset build/perf/rr-saturated/workload.parquet \
  --slc-blocks 128 --tlc-blocks 7340032 \
  --variants original,scan,fused,index,counts --original-package build/rr-original-package

# 本地 Mooncake 源文件转为 timestamp_ns/request_id/affinity_id/hash_ids。
build/test-venv/bin/python benchmark/prepare_mooncake.py \
  ../datasets/mooncake-traces/mooncake/train-00000-of-00001.parquet \
  build/perf/mooncake.parquet
taskset -c 0 build/test-venv/bin/python benchmark/rr_ablation.py \
  --output build/perf/rr-mooncake --dataset build/perf/mooncake.parquet \
  --variants original,scan,fused,index,counts --original-package build/rr-original-package

# 长段命中：不提升 Storage hit，64 TiB 配置未满。
taskset -c 0 build/test-venv/bin/python benchmark/rr_ablation.py \
  --output build/perf/rr-long-hit --requests 4000 --path-length 64 --bypass \
  --slc-blocks 1048576 --tlc-blocks 7340032 \
  --variants original,scan,fused,index,counts --original-package build/rr-original-package

# 5 次子树计数复测；替换 dataset 可重做 Mooncake 对照。
taskset -c 0 build/test-venv/bin/python benchmark/rr_ablation.py \
  --output build/perf/rr-saturated-index-check \
  --dataset build/perf/rr-saturated/workload.parquet --variants index,counts --repeats 5

# 独立 profile。逐次 oracle 校验使用 --verify，不能与主表混用。
taskset -c 0 build/test-venv/bin/python benchmark/rr_ablation.py \
  --output build/perf/rr-saturated-profile \
  --dataset build/perf/rr-saturated/workload.parquet --repeats 1 --profile
```

本轮 Mooncake 原始文件 SHA256：
`10dd50eb8617b995cf14a63134fabfb8ea7001480cf92b4517b638ac55655ae6`。
归一化保留 hash_ids 和行顺序，毫秒乘 1,000,000，request_id/affinity_id 使用行号。
主实验直接使用此前已归一化的同一文件；不是新下载的数据快照。

主测量的每项都显式传入 search/counts 开关；最终选择 indexed、counts=False 为默认，
不改变这些已测参数。具体测量 binary 的 SHA256、当时的源码 hash 以各目录 provenance
及每次测量 JSON 为准，不将分支基线 HEAD 当成未提交实现的版本指纹。


## 最终检查

最终默认配置为 indexed + subtree_counts=False，已重新构建 extension。

- Python 全量：111 passed，包含新增 RR 回放对照与 env -> native 回收配置测试。
- Debug C++：4/4 passed。
- ASan + UBSan Debug C++：4/4 passed。
- Ruff 与 `git diff --check` 通过。
- 最终 binary 另跑 1,000 请求的 original/scan/fused/index/counts 对照，开启逐次 oracle
  校验；所有 metrics 和 trace 一致。结果在 `build/perf/rr-final-verification`，
  校验模式的时延不用于性能结论。

日志位于 `build/rr-final-pytest.log`、`build/rr-final-ctest.log`、
`build/rr-sanitizers.log`、`build/rr-final-verification.log`。
两个 C++ 构建使用独立 TMPDIR，避免测试固定临时文件名在并行检查时互相覆盖。
未执行下游 MQSim；本轮没有修改其输入格式、转换或执行行为。
