# 固定300秒 retention 的混合模型配对实验

新一轮算法迭代、固定参数及更长窗口复核见 [优化迭代报告](OPTIMIZATION.md)。

## 配置与比较范围

在同一native版本上从空缓存回放WEKA准备数据的 `[0,3600)` 窗口；Kimi-K3载荷、100 GB共享DRAM、约1 TB SSD、SLC:TLC=1:2、每池1条stream、wear_share_round_robin存储策略。请求时间和hash身份保持不变；request-end发布最后完整块的LINEAR checkpoint，不推断decode身份。

五组为原LRU-2总是Dump基线、indexed_lru固定300秒、context_lru固定300秒，以及joint_cost权重0/1。两个retention组均设置 `full_retention_ns=linear_retention_ns=300_000_000_000`、`eviction_mode="retention"`、`linear_retention_policy="idle"`，不使用Drop配额、frontier强制Drop或自适应retention。

混合适配规则：

- indexed_lru：FULL使用 `indexed_segment_lru`，LINEAR使用 `checkpoint_lru`。
- context_lru：FULL使用 `context_lru`，LINEAR仍使用 `checkpoint_lru`；`alpha=0.01`，候选累计预算为总DRAM折算FULL块数的1%。整段进入候选，包含跨过预算的最后一段；在候选内按endpoint深度、驻留块数、LRU顺序选择。
- 两组均在共享DRAM中比较选中FULL段与LINEAR checkpoint的最近驻留访问顺序；无FULL/LINEAR固定配额。
- 对选中单元，以其最新驻留访问时间计算idle，严格 `idle > 300s` 才Drop；等于300秒仍Dump。已有SSD副本的对象只释放DRAM，不计入丢失最后副本的Drop。retention不是TTL。
- FULL只回收选中的段，LINEAR只回收完整checkpoint；不复制旧非混合接口的向父段贪婪Dump。因此此处策略名表示混合适配版本，不是原单组件API行为完全相同的回放。
- LRU-2/joint_cost与这两个组的候选选择算法不同；该表比较完整策略组合，不是只替换Drop/Dump判据的单因素消融。

## 五组配对结果

五组均完成3127请求、506,039,360输入token。写入单位为十进制TB，包含分配padding和迁移。

| 策略 | 联合恢复率 | 距上限百分点 | 分配主机写入TB | 重算token |
|---|---:|---:|---:|---:|
| LRU-2，总是Dump | 92.977836% | 1.393852 | 2.383607 | 35,534,912 |
| indexed，300秒 | 93.005382% | 1.366306 | 2.349748 | 35,395,520 |
| context，300秒，alpha=0.01 | 93.098491% | 1.273197 | 2.321042 | 34,924,352 |
| joint_cost，权重0 | 91.612516% | 2.759172 | 1.806706 | 42,443,968 |
| joint_cost，权重1 | 48.469374% | 45.902314 | 0.480037 | 260,765,248 |

本窗口context相对indexed恢复率高0.093109个百分点、少写28.705817 GB（1.221655%）、少重算471,168 token。两组无SSD准入失败。

以context 300秒为基准比较新策略：

- 权重0：主机写入减少22.159702%，联合恢复率降低1.485975个百分点，多重算7,519,616 token；重算总量为context的1.215312倍。
- 权重1：主机写入减少79.318036%，联合恢复率降低44.629117个百分点，多重算225,840,896 token；重算总量为context的7.466574倍。

新策略在写入量上有改进，但当前结果没有证明总体性能更好。两组新策略均以更多重算换取更少主机写入，并未在这两个指标上同时优于context。权重0是较温和的取舍；权重1导致很大的恢复损失，不作为当前推荐配置。对恢复率和计算负载优先的目标，本窗口context更好；对主机写入量优先的目标，新策略更少。没有实测NAND写放大、GPU队列和端到端延迟，不能把这些结果转换成已证实的SSD寿命、吞吐或TTFT提升。

| 策略/组件 | 无SSD副本的淘汰数 | Drop数 | Dump数 | Drop占无SSD副本淘汰比例 |
|---|---:|---:|---:|---:|
| indexed FULL | 458,500 | 0 | 458,500 | 0% |
| indexed LINEAR | 3,086 | 0 | 3,086 | 0% |
| context FULL | 450,361 | 9,839 | 440,522 | 2.184692% |
| context LINEAR | 3,106 | 0 | 3,106 | 0% |

Drop数仅表示淘汰时失去最后副本的对象；已有SSD副本的DRAM淘汰不进入此分母，也不能由这些计数反推联合命中率。context的FULL Drop动作共11,883个，其中2,044个已有SSD副本；indexed和两组LINEAR均无Drop动作。context使用不同的选段顺序，300秒规则会作用于不同的候选；固定相同retention不保证相同Drop率。

完整结果：`build/age-route-cost/retention300-comparison/paired-rr1-3600s/summary.json`。附加审计：`build/age-route-cost/retention300-comparison/audit.json`。同一native SHA256为 `f424ffb2e1c2350cae241e0f23590e85e012f931cdd02ce2704bbf093b4bb93e`；五组运行源码digest集合相同，运行中源码/native均未变化。LRU-2与权重0/1的全部logical stats分别与原 `final-v6-rr1-3600s` 完全一致。源码为 `ea9d063` 加本次适配修改，准确快照以各组provenance的文件digest为准。

## 无限SSD联合命中率上限

直接按时间顺序维护历史已发布的请求末checkpoint集合；对当前请求查找不超过 `input_tokens-1` 的最深历史checkpoint，再在本请求结束后发布当前checkpoint。每个历史checkpoint对应的完整FULL前缀在无限保留条件下必然存在，因此无需运行内存/分流策略。假设从空缓存开始，无Memory Drop、无SSD Trim，保留request-end publication与64-token块粒度；没有提前生成checkpoint或引用未来请求。

| 范围 | 请求数 | 输入token | 最多恢复token | 必须计算token | 联合token命中率上限 |
|---|---:|---:|---:|---:|---:|
| 配对窗口，1小时 | 3,127 | 506,039,360 | 477,557,888 | 28,481,472 | 94.371688% |
| 完整数据，24小时 | 70,132 | 14,520,232,256 | 14,188,642,816 | 331,589,440 | 97.716363% |

一小时上限为 `477,557,888 / 506,039,360 = 94.3716884%`。即使无限保留，也有24,387,136个新后缀token无法从历史恢复，另有4,094,336个token因checkpoint发布位置、块粒度及末token计算约束需要回放，共28,481,472个必须计算token。这里不是假设任意位置都有LINEAR checkpoint的更宽松上限。

| 一小时策略 | 距上限，百分点 | 超过上限基准的额外重算token |
|---|---:|---:|
| baseline | 1.393852 | 7,053,440 |
| indexed_lru-r300s | 1.366306 | 6,914,048 |
| context_lru-r300s | 1.273197 | 6,442,880 |
| cost-w0 | 2.759172 | 13,962,496 |
| cost-w1 | 45.902314 | 232,283,776 |

直接计算约1.46秒，一小时和24小时的输入量、恢复量、重算量与恢复请求数均与已有无限SSD native回放完全一致；数据文件digest也一致。1小时上限用于本报告五组比较，24小时值仅作完整数据参考。结果与独立计算脚本位于 `build/age-route-cost/retention300-comparison/infinite-upper-bound/{result.json,calculate.py}`。

## 复现

每次选择新的输出目录：

```bash
cd /home/solidyang/.codex/worktrees/dwpdsim-age-route-cost
.venv/bin/python benchmark/joint_cost_experiment.py \
  --prepared /home/solidyang/workspace/traceGen/runs/weka-subagent-24h-40tb-v1/dwpdsim \
  --output build/age-route-cost/reproduce-retention300 \
  --end-seconds 3600 --capacities 1 \
  --storage-policies wear_share_round_robin --weights 0 1 --streams 1 \
  --retention-baselines --retention-seconds 300 --context-alpha 0.01 --jobs 3
```

每组在独立进程中串行执行自己的Simulator。runner保存完整配置、数据digest、所有运行源码digest、native副本与digest。比较脚本要求相同输入、请求数、截止时间、模型大小、系统容量及native；结果目录不覆盖。

## 验证

544项Python功能测试通过、2项可选测试跳过；Debug CTest 7/7通过；Ruff通过。新增及扩展的公开功能检查包括冷候选预算下的FULL victim、跨组件回收、FULL/LINEAR的300秒严格边界、丢失最后副本不能产生联合命中、标量/批量前缀恢复一致性。原非混合context选择逻辑被提取复用，原接口回归通过。

统计边界：主指标为联合恢复token/input token；主机写入包含分配padding和迁移。此回放不提供真实NAND写入、WAF或TTFT结论。并发回放耗时仅记录作业执行情况，不作为策略吞吐比较。

真实MQSim功能回放使用相同native：48请求/组，baseline223条命令、joint_cost395条、indexed/context各345条；四组的READ/WRITE/TRIM分配字节与DWPDSim账本一致且执行完成。此处使用2 MiB DRAM和16/32 MiB逻辑池置于参考1 TB物理几何，仅验证转换与完成；合成输入持续94秒，不能验证300秒后Drop的设备效果，严格阈值由功能测试单独覆盖。

```bash
.venv/bin/python benchmark/joint_cost_device_smoke.py \
  --output build/age-route-cost/reproduce-retention300-device \
  --binary /home/solidyang/.codex/worktrees/dwpdsim-kimi-k3/build/mqsim-mapped/MQSim \
  --retention-baselines
```
