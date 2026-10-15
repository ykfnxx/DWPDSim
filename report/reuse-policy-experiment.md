# 两份合成 trace 的 Memory Policy 首轮实验

本轮实现并回放了条件写盘、近期复用优先级、选择性回填及组合；默认策略未修改。完整配置、逐请求计数和图表在 `build/reuse-policy-experiment/`。

## 结论

1. **最大且稳定的读盘收益来自开启已有的 Storage hit 回填机制。** 它不是预测算法的新收益。`MemoryPolicyConfig.admit_storage_hits` 原本默认就是 true，而此前 retention 报告的实验显式关闭了它。
2. **选择性回填暂未显示明显优势。** 与全部回填相比，逻辑读取量略多；有少量写入节省和分阶段命中差异，但没有一致改善。
3. **单独按近期返回概率重排 victim 不值得替换 LRU 默认排序。** 本轮保持粒度、retention和回填开关相同，结果没有稳定改善。
4. **条件 Dump/Drop 有局部价值，但不能固定一个阈值跨负载使用。** TraceLab 128 GiB，阈值0.35比固定300s节省更多写入、compute cost更低，但总命中率更低且loop历史miss请求从6增至19。unscaled 128 GiB同阈值的compute cost增加31.58%，高于固定300s的20.06%。
5. **组合策略有取舍，不能仅凭总计算代价选胜者。** TraceLab 256 GiB，与全部回填相比，组合的写入减少接近（19.81% vs19.47%），compute cost增加从10.89%降到4.67%，但读盘增加，loop历史全局miss请求从34增至41。unscaled 256 GiB组合进一步少写，但compute cost增加82.79%，高于全部回填的44.80%。

## 输入、隔离与配置

- unscaled：daily-mixed-2m-unscaled，原始 SHA256 `7061d977e65b9c25ca0d0938d670a72868096bb597c5411e8e2a619c426708f8`。
- tracelab：daily-mixed-tracelab-24h，原始 SHA256 `9390eba1760c6c8367acb5204ba93ff36ba34a4a1382bf726d7f10704670b6a4`。
- 各用自身 [0,6h) 训练九组 session gap 生存曲线，最后请求后的区间在6h右删失；冻结模型后对 [6h,24h) 从空缓存、空session历史回放。评估分别144,521 / 144,917请求。
- 不读取 task、用户轮次或 loop 结束标签进行决策；这些标签只用于离线归因。不是线上增量训练。
- 128/256 GiB、8 MiB/block、infinite_storage、finish不刷剩余内存。新策略共同使用尾部64-block上限、禁止向父段回收。
- 原生 indexed-300 保持其整段/父段规则，只作为完整policy对照；主消融均用相同粒度的bounded LRU控制。未改变 context_lru。
- 共40组：每个数据/容量10组，含dump-all、300s、条件阈值0.15/0.35/0.60、优先级、全部回填、选择性回填、组合、原生indexed。alpha=0.1，短预测窗口60s，写盘窗口3600s，选择性回填阈值0.5。
- 完整评估窗口一次性使用；没有独立调参验证集。没有扫描完整retention网格，不宣称新策略形成更优的全局Pareto边界。两份数据共享非coding合成机制，不是两组独立生产观测。

## TraceLab：同为300s时，回填的直接影响

分母均为44,073次评估窗口内coding loop续发请求，初次观察的session请求排除。下面统计缺失请求，不是新发生的淘汰事件；包含Dump后持续从Storage读取的影响。

| GiB | 回填方式 | loop历史内存缺失请求 % | loop历史全局缺失请求 % | 全部任务 READ 百万blocks | 写入减少 % | compute cost增加 % |
|---:|---|---:|---:|---:|---:|---:|
| 128 | 不回填 | 37.5173 | 0.0136 | 19.706 | 3.44 | 2.13 |
| 128 | 全部回填 | 5.6974 | 0.0000 | 6.068 | -0.37 | 0.00 |
| 128 | 选择性回填 | 5.6089 | 0.0000 | 6.110 | -0.36 | 0.00 |
| 256 | 不回填 | 13.8702 | 0.1974 | 6.766 | 64.67 | 43.15 |
| 256 | 全部回填 | 0.6172 | 0.0771 | 1.583 | 19.47 | 10.89 |
| 256 | 选择性回填 | 0.6149 | 0.0794 | 1.599 | 20.57 | 11.55 |

写入减少和compute cost增加相对各容量的bounded-LRU dump-all，负写入节省表示写入增加。回填降低读盘与重算，但会改变淘汰年龄和写盘轨迹，不能当作无代价提升。

## 两份数据的关键取舍

| 数据 | GiB | 配置 | 总前缀命中 % | 写入减少 % | compute cost增加 % | READ 百万blocks |
|---|---:|---|---:|---:|---:|---:|
| unscaled | 128 | retention-300 | 94.5592 | 25.82 | 20.06 | 16.250 |
| unscaled | 128 | conditional-035 | 94.3871 | 34.95 | 31.58 | 15.853 |
| unscaled | 128 | admit-all | 95.1279 | -0.60 | 0.09 | 4.944 |
| unscaled | 128 | combined | 95.1086 | -0.29 | 1.32 | 5.027 |
| unscaled | 256 | retention-300 | 93.0113 | 87.08 | 90.79 | 3.162 |
| unscaled | 256 | conditional-035 | 93.2312 | 84.71 | 89.64 | 3.489 |
| unscaled | 256 | admit-all | 93.9029 | 57.50 | 44.80 | 0.706 |
| unscaled | 256 | combined | 93.3890 | 67.53 | 82.79 | 0.602 |
| tracelab | 128 | retention-300 | 94.5685 | 3.44 | 2.13 | 19.706 |
| tracelab | 128 | conditional-035 | 94.5029 | 8.90 | 1.14 | 19.649 |
| tracelab | 128 | admit-all | 94.6402 | -0.37 | 0.00 | 6.068 |
| tracelab | 128 | combined | 94.6369 | -0.19 | 0.02 | 6.106 |
| tracelab | 256 | retention-300 | 93.2923 | 64.67 | 43.15 | 6.766 |
| tracelab | 256 | conditional-035 | 93.9819 | 46.71 | 16.85 | 9.442 |
| tracelab | 256 | admit-all | 94.2724 | 19.47 | 10.89 | 1.583 |
| tracelab | 256 | combined | 94.3027 | 19.81 | 4.67 | 1.642 |

## 解释边界与下一步

- 本轮概率预测的是session返回，不是某个具体block或分支的复用；共享前缀使用最近访问者的类别。下一步应检验分支失效和有界ghost反馈能否减少无效Dump，不能把本轮局部收益当作这些机制已实现。
- 近期概率排序没有显式计算重算成本、Storage副本状态或共享owner需求；暂不采用作默认victim选择。
- 首先在常规回填开启的基线上优化Dump/Drop，保留简单LRU排序；若要验证组合的收益，应增加“条件写盘+全部回填、不改排序”的单独消融。
- `is_agent_turn_end` 来源于下一条用户消息判断；没有作为当前到达时已知hint。引擎响应完成后的真实hint和时间需要另建事件语义，本轮未实现。
- 历史global miss按物理缺失减去当前评估窗口首次出现的cold blocks计算。连续前缀miss另算，不能把第一个缺失之后的全部后缀都算作物理丢失。
- 不模拟真实推理时长、排队或SSD延迟。compute cost为block²代理；字节写入为逻辑量，不含FTL写放大。各组并发运行，elapsed_s不用于策略运行性能排名。
- 默认参数未修改，实验代码保留为reuse_lru。未提交、未推送。

## 产物与验证

- [策略与配置说明](../docs/reuse-policy.md)
- [完整40组表格](../build/reuse-policy-experiment/report.md)
- [CSV](../build/reuse-policy-experiment/results.csv) / [JSON](../build/reuse-policy-experiment/results.json)
- [写入—计算图](../build/reuse-policy-experiment/write_compute_tradeoff.png)
- [loop内存/全局缺失图](../build/reuse-policy-experiment/loop_missing.png)
- [Storage READ图](../build/reuse-policy-experiment/storage_reads.png)
- 输入校验、模型、完整配置及native扩展SHA256保存在实验目录。每组物理hit+miss与block引用数相符；Storage读计数、连续前缀miss和compute cost与Simulator聚合统计核对。
- 261项Python测试通过；Debug CTest 4/4通过；Ruff与git diff --check通过。

```sh
build/test-venv/bin/python benchmark/reuse_policy_experiment.py prepare
build/test-venv/bin/python benchmark/reuse_policy_experiment.py run --workers 4
/tmp/tracegen-plot-venv/bin/python benchmark/report_reuse_policy.py
```
