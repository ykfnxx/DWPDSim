# WEKA 固定 DRAM 分区与双池 SSD 联动验证

**2026-10-27 配置审计更正：本轮额外固定 FULL/LINEAR DRAM 为 80/20 GB，这是 runner 自行采用的约束，不是用户指定的 SLC/TLC SSD 1:2，也不是 Gandalf 的共享 DRAM 设置。以下数据仅代表该固定分区下的联动验证，不能作为 Gandalf 同条件对照。原始结果保持不变。**

共享 DRAM 现已实现并完成四组同输入回放，结果见 [共享容量更正与验证](shared-dram-correction.md)。

论文的 DRAM 总容量由 FULL 和 LINEAR 共同使用，按候选对象的 Drop/Move 代价决定保留、迁移或删除；按类型组织候选队列不代表类型容量限额。现有 `pool_allocation="adaptive"` 按需求量更新两类软目标，也不能直接等同于这一规则。后续对照需采用统一 100 GB DRAM 总量约束及跨类型候选决策；SLC/TLC SSD 的固定 1:2 配置继续保留。[Gandalf §3.2–3.4](https://arxiv.org/pdf/2610.30791#page=5)

日期：2026-10-26。工作分支：`codex/kimi-k3-lru-k`，本轮基于已提交的 `001b06dc3de8b523ac889015aa94f8d2a2fef7e0` 增加有限 SSD 和 MQSim 联动，改动尚未提交。原 DWPDSim checkout 与 MQSim 用户工作区未修改。

本轮比较 **RR / adaptive_endurance × 1 TB / 4 TB** 四组，内存策略统一为 FULL 和 LINEAR 各自的 LRU-2。每组均从空缓存开始，使用相同的 **WEKA 前 1,000 请求**。这是设备联动首轮基线，未覆盖全部 98,827 请求。

## 结果

下表的命中率均为 `restored_tokens / input_tokens`，仅当同一恢复边界上连续 FULL 前缀与 LINEAR checkpoint 都可读，才累计 restored tokens。DRAM 与 SSD 可以共同提供这两个组件。

| Storage policy | 标称 SSD 容量 | 联合 token 命中率 | 有恢复的请求 | 主机写入 TB | NAND 写入 TB | 总 WAF | 设备状态 |
|---|---:|---:|---:|---:|---:|---:|---|
| RR | 1 TB | 26.5216% | 196 / 1,000 | 1.367228 | 1.475768 | 1.079387 | 已排空 |
| adaptive_endurance | 1 TB | 21.4235% | 160 / 1,000 | 1.407849 | 1.518198 | 1.078381 | 已排空 |
| RR | 4 TB | 33.9752% | 248 / 1,000 | 1.291257 | 1.291257 | 1.000000 | 已排空 |
| adaptive_endurance | 4 TB | 33.9752% | 248 / 1,000 | 1.291257 | 1.291257 | 1.000000 | 已排空 |

这里的 TB 均为十进制 `10^12 bytes`。总 WAF 定义为两池 NAND programmed bytes 之和除以两池 host write bytes 之和，不能直接平均两个池的 WAF。完整数值和审计结果保存在 [summary.json](../build/weka-storage-baseline/summary.json)。

| 策略 / 容量 | SLC WAF | TLC WAF | SLC / TLC GC programs (pages) | SLC / TLC 总擦除次数 | SLC / TLC 最大单块擦除次数 |
|---|---:|---:|---:|---:|---:|
| RR / 1 TB | 1.000000 | 1.096409 | 0 / 6,624,768 | 0 / 7,040 | 0 / 2 |
| adaptive / 1 TB | 1.137201 | 1.000000 | 6,735,168 / 0 | 7,808 / 0 | **3** / 0 |
| RR / 4 TB | 1.000000 | 1.000000 | 0 / 0 | 0 / 0 | 0 / 0 |
| adaptive / 4 TB | 1.000000 | 1.000000 | 0 / 0 | 0 / 0 | 0 / 0 |

adaptive 1 TB 正常完成，SLC 最大擦除次数恰好为配置上限 3，尚未因下一次超限擦除中止。它的总 WAF 虽比 RR 低约 0.0010，但 host writes 多 2.9710%、NAND writes 多 2.8751%，且磨损集中到较小的 SLC 池。不能只看 WAF 比值判断 endurance 效果。

四组共 **3,470,635 条命令**，全部 generated=completed。RR 1 TB / adaptive 1 TB / RR 4 TB / adaptive 4 TB 的命令数分别为 1,131,945 / 1,095,506 / 621,592 / 621,592。四组 READ、WRITE、请求 TRIM 字节均与 native 和 manifest 一致；两组有 TRIM 的实验中 effective trimmed bytes 也与 requested trim bytes 相等。

下表拆开 payload 与分配粒度造成的传输量，并记录逻辑容量淘汰位置；segment 数不是对象数。

| 策略 / 容量 | Payload writes TB | Allocated host writes TB | SLC capacity trim segments | TLC capacity trim segments |
|---|---:|---:|---:|---:|
| RR / 1 TB | 1.222581 | 1.367228 | 0 | 466 |
| adaptive / 1 TB | 1.256925 | 1.407849 | 505 | 0 |
| RR / 4 TB | 1.158411 | 1.291257 | 0 | 0 |
| adaptive / 4 TB | 1.158411 | 1.291257 | 0 | 0 |

## 数据、模型与时间线

- 数据集：`semianalysisai/cc-traces-weka-062126`，revision `23f152f6f0f9399a85901b89a6458def0ef16729`；下载源 SHA256 为 `29b6a19e751ff5230771519aab755f80a0f43a4ba9cf96b72d3a6a437ec99276`。准备文件位于 [dataset.json](../build/weka-kimi-062126/input/dataset.json)。
- 全集包含 393 个 root session、98,827 个请求。本轮在全局稳定时间排序后截取前 1,000 请求，含 620,626 个输入 hash block，共 **39,720,064 个输入 token slots**。原数据的输入长度由 64-token hash block 表示，应视为该数据集的长度代理。
- root session 均从 t=0 对齐；嵌套请求的 `t` 已经是 root 相对时间，不重复加父节点时间。排序键为 `(t, trace_index, recursive position)`。不同 root 的 hash namespace 隔离，子请求共享所属 root 的 namespace。`affinity_id=trace_index+1`。
- 所有源模型调用统一投影到 Kimi-K3 的逻辑 whole-model 缓存，不模拟各源模型的实际结构。模型参数 revision 为 `f831ab66814297da540d832a5235f8e904f29d06`，缓存实现参考 revision 为 `ffcabb8c7ab3e066e1c77b55d7673e5d08cf26cc`。
- 输入原长直接传入，不加 1；单次恢复上限为 `input_tokens-1`。每次 prefill 在最后一个完整输入 block 发布一个 LINEAR checkpoint。没有根据输出长度捏造 decode hash。
- 该窗口最后请求到达时间为 **11.758 s**。DWPDSim 按请求时间回放并生成开放环路 I/O，MQSim 继续执行直到全部命令完成，测量区间配置为 `[0, 10^18 ns)`。MQSim 的完成时间不反馈给 DWPDSim 的命中决策，因此这里的命中率是逻辑缓存可用性结果。

## 缓存与 storage policy 配置

| 参数 | 本轮值 |
|---|---|
| FULL DRAM 预算 | 80,000,000,000 bytes |
| LINEAR DRAM 预算 | 20,000,000,000 bytes |
| 两组件内存策略 | `lru_k`, K=2 |
| FULL 64-token block payload | 1,769,472 bytes |
| LINEAR checkpoint payload | 449,372,160 bytes |
| DWPDSim SSD allocation unit | 1 MiB = 1,048,576 bytes |
| FULL / LINEAR 实际 SSD 分配 | 2,097,152 / 449,839,104 bytes |
| 写入规则 | DRAM eviction 时 Dump；已有 SSD 副本不重复写 |
| 每池 stream 数 | 1；共 2 个 flow，与 6 个物理 channel 区分 |
| RR 实现 | 既有 `wear_share_round_robin`，indexed victim search |
| RR SLC host share | `(1×3)/(1×3+2×7)=3/17` |
| 两策略 endurance budgets | SLC=3，TLC=7 |
| logical_fill_fraction | 0.98 |
| adaptive idle_multiplier / promotion_seconds | 32 / 14,400 s |
| adaptive adaptation_gain / direct_gain | 2 / 1 |
| adaptive SLC soft utilization / occupancy_decay | 0.75 / 8 |
| adaptive background period | 900 s；本窗口实际 ticks=0 |
| online_tuning | false |

FULL 与 LINEAR 的 payload、DRAM 索引、SSD 地址、读出及命中判定独立。为了复用原 storage policy 的 radix segment 状态，**同一 prefix node 已存储的组件共用 tier/stream**；segment trim 或迁移作用于该 segment 的全部 SSD 组件，DRAM 副本保持可用。这是本轮明确采用的放置约束，尚未实现同一 prefix 的 FULL/LINEAR 自由分配到不同池。

设备 trace 传输整个 allocation unit，**包含 padding**。报告同时保留逻辑 payload bytes 与 allocated/host bytes；WAF 分母使用 allocated host writes，不把 NAND 页、token block 和 1 MiB allocation unit 混为一谈。

## NAND 几何与容量倒推

全设备共 `6 × 2 × 2 × 4 = 96 planes`。每 block 的数据容量为 `4224 × 16384 = 69,206,016 bytes`。相同 `Block_No_Per_Plane` 配合 SLC 2 channel、TLC 4 channel，给出容量 1:2。

先将可见容量量化到 `3 MiB` 的整数倍，确保两池精确 1:2，再计算：

```text
L_slc = floor(C_nominal / (3 × 1 MiB)) × 1 MiB
L_tlc = 2 × L_slc
blocks_per_plane = ceil((L_slc + L_tlc) / ((1 - 0.125) × 96 × 4224 × 16384))
```

| 标称容量 | Block / plane | SLC 可见 bytes | TLC 可见 bytes | 物理数据 bytes 总量 | 实际 spare 比例 |
|---|---:|---:|---:|---:|---:|
| 1 TB | **173** | 333,332,873,216 | 666,665,746,432 | 1,149,373,513,728 | 12.9962% |
| 4 TB | **689** | 1,333,332,541,440 | 2,666,665,082,880 | 4,577,562,722,304 | 12.6173% |

XML 的 OP=0.125；因整数 block 向上取整，`1-L/P` 略大于 12.5%。当前 MQSim 对显式 pool 容量检查 `L <= P × (1-OP)`，实际 spare 仍由 L/P 决定。

| 硬件参数 | 配置 |
|---|---|
| Flash_Channel_Count | 6 |
| SLC / TLC Channel_IDs | `0,1` / `2,3,4,5` |
| Chip_No_Per_Channel / Die_No_Per_Chip / Plane_No_Per_Die | 2 / 2 / 4 |
| Page_No_Per_Block / Page_Capacity | 4,224 / **16,384 bytes** |
| SLC read LSB/CSB/MSB | 75,000 / 75,000 / 75,000 ns |
| SLC program | 750,000 ns |
| TLC read LSB/CSB/MSB | 75,000 / 120,000 / 140,000 ns |
| TLC program LSB/CSB/MSB | 1,500,000 / 2,000,000 / 2,800,000 ns |
| SLC / TLC erase | 3,800,000 / 3,800,000 ns |
| SLC / TLC P/E limit | **3 / 7 cycles**，按提供值直接使用 |

控制器基线沿用本机模板：seed=321、PCIe 每 lane 1.0 GB/s ×4、channel transfer rate=333、NVDDR2、NVMe queue depth=64、fetch=16、page-level mapping、CWDP、out-of-order scheduling。数据 cache 通过 workload 关闭；初始 occupancy=0、preconditioning=false。`Ideal_Mapping_Table=true`，因此本轮不研究 mapping-cache miss 与 translation page 写放大。

GC 使用 `KV_THREE_GREEDY`，soft/hard thresholds=0.10/0.05；copyback、preemptible GC、dynamic/static wear leveling 均关闭。其余原样参数见每组保存的 [ssd.xml](../build/weka-storage-baseline/rr-1tb-1000/device/ssd.xml) 与 [workload.xml](../build/weka-storage-baseline/rr-1tb-1000/device/workload.xml)。

## 联动、审计与验证

联动路径：

1. [benchmark/weka_kimi.py](../benchmark/weka_kimi.py) 从 prepared hash mmap 流式构造请求，调用混合组件模拟器。
2. [hybrid_simulator.cpp](../cpp/src/hybrid_simulator.cpp) 生成组件的 READ/WRITE/TRIM；[hybrid_storage.cpp](../cpp/src/hybrid_storage.cpp) 接入既有 RR/adaptive placement、capacity trim、访问和后台迁移。
3. [hybrid_mqsim.py](../src/dwpdsim/hybrid_mqsim.py) 将有限地址组件 trace v2 转为 `DWPDSIM_DEPENDENCY_V1` 七列 flow trace。连续 allocation units 合并到不超过 65,535 sectors 的命令；保持同对象分片顺序、pool 全局地址复用依赖，以及迁移 READ → WRITE → TRIM 依赖。容量回收后的逻辑地址碎片会增加命令数，四组统一使用同一合并规则。
4. [hybrid_device.py](../benchmark/hybrid_device.py) 保存本组 XML、manifest、原始日志和 device_result.json。
5. [summarize_storage_baseline.py](../benchmark/summarize_storage_baseline.py) 按显式 Flow_ID/Pool_ID 核对 generated=completed=manifest commands；逐池核对 native→manifest→device read/write/TRIM bytes，并检查 `NAND programmed bytes = host writes + GC programs × 16 KiB`。

4 TB 的原始 MQSim 将 GMT 全量放入匿名内存，超出本机当时可用内存。本轮额外构建一个仅改变 **host GMT backing** 的隔离版本：固定长度 GMT 改用输出目录中的文件 mmap，初始 sentinel 内容保持一致；未改变 FTL 映射、NAND、GC 或仿真时间。构建脚本为 [build_mqsim_host_backing.py](../benchmark/build_mqsim_host_backing.py)，源差异为 [mqsim-host-backing.patch](../benchmark/mqsim-host-backing.patch)。原始 MQSim 仓库和二进制保留。

MQSim 源快照：`b8a1dff35057714e6ad04e99df2675fc05344ea2`。RR 1 TB 使用原始二进制；其余三组使用上述 mmap 版本。二进制 SHA256 逐组记录在原始 JSON 中。对照验证中，原版与 mmap 版在 1-flow / 2-flow 的 GC 压力用例所得 host bytes、NAND/GC program counts 和 WAF 完全一致：WAF 分别为 2.8851356589 / 2.4787984496，原始证据见 [equivalence.json](../build/mqsim-backing-validation/equivalence.json)。此对照没有覆盖全部设备配置或延迟分布。

验证已完成：Python **498 passed, 2 skipped**；Debug CTest **5/5**；Ruff 和 diff 检查通过。另跑过 RR/adaptive 小几何的真实 native→converter→MQSim 回放，以及 `example/run_pipeline.py` 的 finite-hybrid 端到端回放。小几何用例只用于功能验证，其 WAF 不混入上方设备结果。

4 TB 两策略的恢复数还与同窗口独立 persistent-checkpoint control 一致：13,494,976 tokens、248 个请求；见 [persistent-control.json](../build/weka-storage-baseline/persistent-control.json)。每个 result.json 保留实际 native extension SHA；最后一组使用增加有限策略入口校验后的构建，计算路径相同。

## 复现命令

在本 worktree 根目录使用 `.venv/bin/python`。已有准备数据可跳过 prepare，原始 corpus 校验值固定于脚本。

```bash
.venv/bin/python benchmark/weka_kimi.py prepare \
  --source /home/solidyang/workspace/G35/datasets/cc-traces-weka-062126/traces.jsonl \
  --prepared build/weka-kimi-062126/input

.venv/bin/python benchmark/build_mqsim_host_backing.py \
  --repository /home/solidyang/workspace/G35/MQSim \
  --output build/mqsim-mapped-reproduction

# 示例：其余组合将容量换为 4、policy 换为 adaptive_endurance，输出目录对应修改。
.venv/bin/python benchmark/weka_kimi.py run \
  --prepared build/weka-kimi-062126/input \
  --output build/weka-storage-baseline/rr-1tb-1000 \
  --storage-policy wear_share_round_robin --capacity-tb 1 \
  --pool-allocation fixed --memory-bytes 100000000000 \
  --full-bytes 80000000000 --linear-bytes 20000000000 \
  --requests 1000 --batch-size 16 --emit-io-trace

.venv/bin/python benchmark/hybrid_device.py \
  --replay build/weka-storage-baseline/rr-1tb-1000 --capacity-tb 1 \
  --binary build/mqsim-mapped-reproduction/MQSim

.venv/bin/python benchmark/summarize_storage_baseline.py --require-complete
```

四组原始数据目录：

- [RR 1 TB](../build/weka-storage-baseline/rr-1tb-1000/result.json)
- [adaptive 1 TB](../build/weka-storage-baseline/adaptive-1tb-1000/result.json)
- [RR 4 TB](../build/weka-storage-baseline/rr-4tb-1000/result.json)
- [adaptive 4 TB](../build/weka-storage-baseline/adaptive-4tb-1000/result.json)

## 结论边界与下一轮

4 TB 下没有 SSD capacity trim，两策略恢复能力一致；1 TB 下 RR 在该前缀窗口保存了更多可共同恢复的 FULL/LINEAR 状态。adaptive 的 SLC staging 在短窗口产生更多 SLC 压力，不能据此认定它的长期寿命或长期命中率较差。

本窗口不足 900 s，所有组 `background_ticks=0`，没有测到 adaptive 的周期迁移和 idle trim 效果。MQSim 的设备排空不能替代 DWPDSim 时间线的延长。下一轮需要覆盖真实的跨周期请求区间，并保持预先固定参数；不能为了触发后台行为悄悄拉伸本轮时间戳。

两个 adaptive 组各记录 607 个 session gap，q95 仍为初始化值 257.2775 s。既有 [GapEstimator::observe](../cpp/src/policies/storage/adaptive_endurance_storage_policy.cpp) 每 2,048 个样本才重算 q95，所以这里还没有根据本轮样本刷新该分位数。当前 idle threshold 在 1 TB / 4 TB 分别为 1,340.8872 / 8,232.88 s；前者因 SLC 占用压力缩短。下一轮除跨越后台周期外，还应覆盖 q95 更新门槛。

对 prepared 请求索引的只读检查表明：前 3,000 / 10,000 请求分别只覆盖 79.982 / 433.371 s；第 **15,454** 个请求才到达 900.015 s，第 **30,454** 个请求到达 3,600.146 s。因此，下一轮可预先固定一个覆盖至少 1 小时的实际前缀窗口，分别记录后台动作及 q95 更新；这只是下一轮窗口建议，本轮没有执行这些更大设备回放。继续采用 P/E=3/7 时，还应允许并单独报告正常的 wear-limit 终止，不能在观察到结果后提升预算。

多 session 从零对齐会制造 I/O burst；设备 response time 含长队列和依赖等待，不能解释为模型 TTFT，也不宜直接用作 policy 的空载 dump/read cost 常量。后续代价校准应分离实际服务、传输、排队和依赖等待。当前是预生成 trace 的开放环路实验，尚未将设备完成延迟反馈给计算、请求调度或缓存可见性。没有 GPU/计算服务时间模型，也不从 P/E=3/7 的短实验外推多年寿命或 DWPD。
