# Joint-cost优化与配对验证

状态：一小时主窗口及固定参数的两小时完整配对均已通过。两窗口的新策略命中率都高于LRU-2，分配主机写入都严格低于indexed/context300s；同版本、数据、有限容量和上限计算的最终审计通过。

## 目标与固定协议

目标是新策略联合token命中率不低于LRU-2减0.1个百分点，同时分配主机写入严格低于固定300秒的indexed/context混合适配版本。主窗口为 `[0,3600)`，3127请求、506,039,360输入token；从空缓存开始。Kimi-K3载荷、64token/块、request-end checkpoint、100GB共享DRAM、约1TB有限SSD、SLC:TLC=1:2、每池1stream、wear_share_round_robin保持不变。

indexed/context的FULL策略分别为indexed_segment_lru/context_lru，LINEAR均为checkpoint_lru；两组件retention均为300秒，context alpha0.01，不使用自适应retention。新策略与LRU-2使用同一LRU-K冷候选索引。所有实际恢复均要求连续可读FULL前缀及范围内最深完整LINEAR checkpoint；保留N-1上限，不生成decode身份。

## 一小时完整结果

| 策略 | 联合token命中率 | 距上限，百分点 | 分配主机写入GB | 重算token |
|---|---:|---:|---:|---:|
| 无限SSD上限，无Drop/Trim | 94.371688% | 0 | — | 28,481,472 |
| LRU-2，总是Dump | 92.977836% | 1.393852 | 2383.607235 | 35,534,912 |
| indexed，300s | 93.005382% | 1.366306 | 2349.747667 | 35,395,520 |
| context，300s | 93.098491% | 1.273197 | 2321.041850 | 34,924,352 |
| 原joint_cost，媒体权重0 | 91.612516% | 2.759172 | 1806.705885 | 42,443,968 |
| 迭代后continuation，媒体权重0，重算优先级1 | 93.013868% | 1.357820 | 1567.128289 | 35,352,576 |

选中的新配置相对LRU-2命中率高0.036032个百分点，少重算182,336token；写入相对indexed减少33.306529%、相对context减少32.481688%。其命中率比context低0.084623个百分点。相对原媒体权重0实现，命中率提高1.401352个百分点，主机写入再减少13.260465%。这些是本窗口的逻辑缓存指标，不是NAND写入、WAF或真实服务延迟结论。

## 两小时固定参数验证

窗口为 `[0,7200)`，6192请求、1,145,089,216输入token，全部从空缓存重放，参数和native与主窗口完全相同。该窗口包含主窗口及后续一小时，不是独立跨数据集测试，也不构成全天新策略性能结论。

| 策略 | 联合token命中率 | 距上限，百分点 | 分配主机写入GB | 重算token |
|---|---:|---:|---:|---:|
| 无限SSD上限，无Drop/Trim | 95.870433% | 0 | — | 47,287,232 |
| LRU-2，总是Dump | 93.317143% | 2.553289 | 4763.609465 | 76,524,672 |
| indexed，300s | 93.426432% | 2.444000 | 4711.904182 | 75,273,216 |
| context，300s | 93.568864% | 2.301568 | 4611.800826 | 73,642,240 |
| 迭代后continuation，媒体权重0，重算优先级1 | 94.272223% | 1.598210 | 2955.410407 | 65,588,160 |

新配置相对LRU-2提高0.955079个百分点，少重算10,936,512token；相对indexed/context的写入分别减少37.277791%/35.916348%，命中率也分别提高0.845791/0.703358个百分点。所有组两小时回放中的第一小时FULL、LINEAR、prefill、DRAM和SSD统计都与独立一小时结果完全一致。

新策略两小时SLC/TLC分配主机写入为1349.619/1605.791GB，context为813.840/3797.961GB；总量降低仍伴随更高SLC写入。实际容量Trim合计1981.267GB，两个池峰值均在配置容量内。

## 无限SSD上限直接计算

从空缓存开始，按请求顺序先查询已发布的历史checkpoint，再发布当前请求最后完整块的checkpoint。无限后备存储永久保留全部FULL与LINEAR，不执行Drop/Trim，也没有分流容量约束。一个已发布checkpoint可证明对应连续FULL前缀完整存在，恢复位置仍受当前请求 `input_tokens-1` 与 `max_prefix_tokens` 限制。计算不调用Simulator，且不传入策略或训练器。

| 输入窗口 | 请求数 | 输入token | 最多恢复token | 联合token命中率上限 | 最少重算token |
|---|---:|---:|---:|---:|---:|
| 前1小时 | 3,127 | 506,039,360 | 477,557,888 | 94.371688% | 28,481,472 |
| 前2小时 | 6,192 | 1,145,089,216 | 1,097,801,984 | 95.870433% | 47,287,232 |
| 完整24小时 | 70,132 | 14,520,232,256 | 14,188,642,816 | 97.716363% | 331,589,440 |

这里是可恢复token占输入token的比例。全天剩余331,589,440token中，265,471,744为未匹配的新后缀，66,117,696为已有FULL前缀但缺少可用request-end checkpoint等约束引起的重放；无限SSD不能消除这两部分。它是在当前checkpoint生成与恢复语义下的无删除上限；若改变checkpoint生成方式，需要重新计算。直接结果与已归档的无限SSD native回放逐项一致。

## 代价模型调整

原压力存活模型在估计未来请求时维持DRAM副本，同时按推测写入压力随机使SSD副本失效。逐块独立抽样还会因长FULL前缀包含许多块而制造失效孔洞，低估Dump保留完整前缀的价值。

保留两种可明确选择的假设：pressure模式按存储池/Radix段共享存活分位数；continuation模式在固定未来延续中不额外注入未观察到的容量失效。continuation仍将当前报价的完整容量受害者/迁移计划与可确定后台失效计入成本。实际执行器在两种模式下都严格执行同一有限SSD容量、Trim和迁移，没有扩大设备容量。

选中的continuation配置使用与原实验相同的复用概率、硬件速率及媒体权重0，重算优先级保持1。原始逻辑需求计数和全部复用估计器评分/训练统计与原权重0运行完全一致，收益来自代价模型与行动选择变化。它没有使用报告中的命中率上限或未来标签进行在线决策。

本窗口中，新配置FULL最后副本Drop由14,726降至5,430，LINEAR最后副本Drop由1,156增至1,587。尽管删除更多LINEAR checkpoint，联合命中仍提高，说明要比较checkpoint与连续FULL前缀的实际边际价值，不能只按组件Drop数判定优劣。FULL写入由922.772GB降至877.075GB，LINEAR写入由883.934GB降至690.053GB。

## 固定配置与复现

选中参数：`eviction_mode="joint_cost"`、`cost.storage_survival="continuation"`、`cost.wear_weight=0`、`recompute_weight=1`、每类1组候选、最多4条历史路径；60/300/1800秒时域、每8请求每类抽32次forecast、seed42；16KiB页、4224页/块、mapping项0、OP0.125。带宽、prefill速率和NAND价格保持原实验值，未经设备标定。完整配置保存在每组config.json。

主窗口复现命令（使用新的输出目录）：

```bash
cd /home/solidyang/.codex/worktrees/dwpdsim-age-route-cost
.venv/bin/python benchmark/joint_cost_experiment.py \
  --prepared /home/solidyang/workspace/traceGen/runs/weka-subagent-24h-40tb-v1/dwpdsim \
  --output build/age-route-cost/reproduce-optimized-3600s \
  --end-seconds 3600 --capacities 1 --storage-policies wear_share_round_robin \
  --weights 0 --cost-recompute-weight 1 --cost-storage-survival continuation \
  --cost-candidates 1 --cost-scenarios 4 --streams 1 \
  --retention-baselines --retention-seconds 300 --context-alpha 0.01 --jobs 4
```

扩展窗口将end-seconds设为7200，保留全部参数并从空缓存重放。完整结果位于 `build/age-route-cost/optimization-v1/continuation-validation-7200s`，最终配对验收已完成。

## 验证与证据

549项Python功能测试通过、2项可选跳过，Debug CTest7/7与Ruff通过。两种存活模式均检查合法联合恢复、有限容量、I/O依赖及训练与动作解耦。选中配置的真实MQSim功能回放完成48请求/组：baseline223、joint_cost395、indexed/context各345条命令，字节账本与完成计数通过；小逻辑池验证不证明GC/WAF收益。

同一小时的五组源码和native完全一致，且运行期间未改变；LRU-2/indexed/context全部stats与之前已冻结结果完全一致。native SHA256为 `7026ea306bdfc8bb248a969e4e0940aa71778933d0091262e63e8b9674a5ce2b`。源码冻结在 `build/age-route-cost/optimization-v1/runtime-continuation`，以各组provenance的逐文件digest为准。工作区代码整理后重编译得到完全相同的native二进制。

- `build/age-route-cost/optimization-v1/continuation-3600s/summary.json`：同native五组完整筛选结果，含各行距上限百分点。
- `build/age-route-cost/optimization-v1/primary-candidate-proof.json`：主窗口的数值验收。
- `build/age-route-cost/optimization-v1/primary-audit.json`：源码/native/数据、旧baseline复现及训练统计一致性审计。
- `build/age-route-cost/optimization-v1/continuation-validation-7200s/summary.json`：同native四组两小时完整配对，含各行距上限百分点。
- `build/age-route-cost/optimization-v1/final-audit.json`：两窗口验收、首小时结果复现、同版本与真实有限容量审计。
- `build/age-route-cost/retention300-comparison/infinite-upper-bound/result.json`：首小时和完整24小时直接计算及无限native交叉验证。
- `build/age-route-cost/optimization-v1/device-final-continuation-r1/summary.json`：选中配置MQSim功能完成证明。
- `docs/age-route-cost/optimization/PROGRESS.md`：阶段完成记录。

报告的无限SSD上限在回放结束后单独计算，每个请求先查历史checkpoint再发布当前checkpoint；与一小时及两小时已有无限SSD native结果完全一致。它只参与报告，不传入策略。


## 存活模型与重算优先级消融

全部为一小时完整输入、媒体权重0，其余配置相同。pressure使用段相关存活分位数，原实现使用逐块独立存活。两组模型族分别冻结自己的source/native，族内配对一致；各族的LRU-2/indexed/context结果完全一致。

| 存活模型 | 重算优先级 | 联合token命中率 | 距上限，百分点 | 主机写入GB | 命中门槛 |
|---|---:|---:|---:|---:|---|
| pressure | 1 | 92.636386% | 1.735302 | 1621.431943 | 未通过 |
| pressure | 4 | 92.679829% | 1.691859 | 2077.805773 | 未通过 |
| pressure | 16 | 92.873383% | 1.498306 | 2285.390266 | 未通过 |
| continuation | 1 | 93.013868% | 1.357820 | 1567.128289 | 通过 |
| continuation | 4 | 92.658494% | 1.713195 | 2041.395020 | 未通过 |

提高重算优先级并未使整个有限容量回放的命中率单调提高，候选选择、路由及后续容量状态均会改变。因此使用完整结果选择参数；选中优先级仍为1。

新策略的一小时SLC/TLC分配主机写入分别为727.407/839.722GB，context为409.500/1911.541GB。路由属于新策略的决策，总写入改善包含路由和保留选择的共同作用；这不是固定分流下的纯memory判据消融，也不等价于分池NAND磨损更低。实际SLC/TLC合计容量Trim为599.115GB，两个池峰值均未超过配置容量，证明实际回放仍使用有限SSD。
