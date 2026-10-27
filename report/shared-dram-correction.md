# FULL/LINEAR 共享 DRAM 更正与验证

2026-10-27，工作目录 `/home/solidyang/.codex/worktrees/dwpdsim-kimi-k3`，分支
`codex/kimi-k3-lru-k`，基于提交 `001b06dc3de8b523ac889015aa94f8d2a2fef7e0` 的工作区修改。
原先 WEKA runner 默认的 FULL 80 GB / LINEAR 20 GB 分区已移除。现在默认只有总 DRAM
预算，两类对象共同竞争；SLC/TLC SSD 的固定双 pool 和 1:2 容量比例继续使用原配置。

## 容量与候选决策

默认 `pool_allocation="shared"`，唯一 DRAM 容量约束为：

```text
full.resident_bytes + linear.resident_bytes <= memory.capacity_bytes
```

FULL/LINEAR 的 `*_capacity_bytes` 必须为 0；共享模式收到非零独立配额会明确报错。
任一组件都可以使用整个预算，没有预留份额，也不根据需求量更新类型软目标。

FULL 和 LINEAR 保留独立驻留状态、SSD 副本、需求历史及组件统计。类型索引用于组织对象，
不表示分区。两类均为 `lru_k` 时，使用统一请求与准入时钟比较两个队首，等价于全局 LRU-K
排序。对象大小决定释放多少字节，不改变 LRU-K 次序。一次压力回收可以淘汰另一类型对象，
直到待准入对象能够放下；超出总预算的单个对象旁路 DRAM，尝试写入后备存储。

共享 LRU 还支持 FULL `segment_lru/indexed_segment_lru` 配合 LINEAR `checkpoint_lru`：
在合法 FULL 冷叶段和 LINEAR checkpoint 之间比较最近访问次序。不同类型使用不兼容的评分
函数时明确拒绝配置，避免隐式引入类型优先级。

真正恢复仍要求同一路径的 FULL `[0,r)` 连续可读，且边界 `r` 的 LINEAR checkpoint 可读；
副本可以来自 DRAM 或 SSD。只命中一种组件不计联合命中。回填前先完成全部来源判定与消费，
不能用回填后的新状态制造命中。

`fixed` 仅供显式复现历史固定分区；`adaptive` 仍是基于衰减需求质量的软目标实验模式。
本次实现共享容量下的 LRU-K/LRU 基线。完整 Gandalf Drop/Move 代价估计、复用概率模型和
准入策略尚未实现，不能把此次配置更正称为论文算法复现。

主要实现位置：

- [Python 默认配置](../src/dwpdsim/config.py) 与 [Kimi-K3 profile](../src/dwpdsim/model_profiles.py)。
- [C++ 配置校验](../cpp/src/simulator.cpp)：共享预算、组件配额和排序组合。
- [全局候选选择](../cpp/src/policies/memory/hybrid_memory_policy.cpp)：`decide_shared()`。
- [准入与回收](../cpp/src/hybrid_simulator.cpp)：`hybrid_admit()`、`hybrid_publish()`。
- [聚合统计](../cpp/src/bindings.cpp)：shared `pool` 包含总容量、当前/峰值驻留、
  `cross_component_reclaims`；不包含类型 target 或更新次数。

## WEKA 相同输入对照

沿用准备好的 WEKA 062126 前 1,000 条请求、空缓存启动、原始请求顺序和时间戳。
输入合计 39,720,064 token，匹配过往前缀 13,863,488 token，末请求时间 11.758 s。
模型载荷、LRU-2、总 DRAM 100,000,000,000 B、SSD 配置及两种 storage policy 参数均与
旧实验一致。shared 回放关闭 I/O trace；trace 开关的语义等价性另有集成测试覆盖。
旧 trace-on 与新 trace-off 的运行耗时不用于性能比较。

主指标为 `restored_tokens / input_tokens`，只统计 FULL/LINEAR 交集恢复的 token：

| SSD 容量 | storage policy | 旧固定 80/20 GB | 共享 100 GB | 变化（百分点） | 共享恢复 token | 共享有恢复请求 / 1,000 |
|---|---|---:|---:|---:|---:|---:|
| 1 TB | RR | 26.5216% | 26.6500% | +0.1284 | 10,585,408 | 197 |
| 1 TB | adaptive_endurance | 21.4235% | 23.5556% | +2.1320 | 9,356,288 | 174 |
| 4 TB | RR | 33.9752% | 33.9752% | 0 | 13,494,976 | 248 |
| 4 TB | adaptive_endurance | 33.9752% | 33.9752% | 0 | 13,494,976 | 248 |

“有恢复请求”表示至少恢复一个有效前缀，不能解释为整条请求无需计算。
共享配置纠正了额外容量约束，但本窗口的命中改善有限，不能把此前全部差距归因于 80/20。

四组共享 DRAM 的聚合峰值均为 **99,999,940,608 B**，不超过 100 GB。
LINEAR 的独立峰值均为 58,867,752,960 B，已经超过原来的 20 GB 限额；两种 4 TB 运行末尾
FULL 为 93,589,143,552 B、LINEAR 为 6,291,210,240 B，表明容量随实际全局次序变化。
组件峰值发生时刻可能不同，不能把两个独立峰值相加当作总峰值。

共享模式的跨组件回收决策数分别为 RR 1 TB 176,782、adaptive 1 TB 172,146、两组
4 TB 均 182,698。这些是候选决策次数，不是对象或字节数。共享后的逻辑 allocated host
write 分别为 1.376529 / 1.392954 / 1.291259 / 1.291259 TB；例如 RR 1 TB 的旧值为
1.367228 TB，不能由命中率改善推断写入量必然下降。

固定输入的四次共享热路径检查中，aggregate pybind/native 耗时分别为
13.807 / 83.366 / 9.965 / 5.457 s；输入准备和最终统计序列化不在该计时内。
每组仅一次，用于记录本次实现的可运行性与开销，不作统计性能结论。

原始结果：

- [RR 1 TB](../build/weka-shared-dram/rr-1tb-1000/result.json)。
- [adaptive 1 TB](../build/weka-shared-dram/adaptive-1tb-1000/result.json)。
- [RR 4 TB](../build/weka-shared-dram/rr-4tb-1000/result.json)。
- [adaptive 4 TB](../build/weka-shared-dram/adaptive-4tb-1000/result.json)。
- [旧固定分区结果](weka-storage-baseline.md)，原始目录未覆盖。

本窗口短于 900 s，四组 `background_ticks=0`。这是配置更正的短窗口核对，没有重新执行
全量 corpus，也没有为这四组重新运行设备 WAF 测试；不能将旧固定分区的 MQSim 结果套到
新共享结果上。本窗口尚不能评价 adaptive 周期维护，也不能作为 Gandalf 同条件实验。

## 验证

重建 native extension 后，Python 全套 **519 passed、2 skipped**，Debug CTest **5/5**。
[共享模式集成测试](../tests/test_hybrid_shared.py) 新增 21 个参数化用例，覆盖任一类型独占
总容量、带不同对象字节数的独立全局 LRU-K oracle（K=1/2/3/5，Dump/Drop）、跨类型 LRU、
末副本丢失后的联合命中、超大 checkpoint 旁路，以及无限后备/RR/adaptive 下的逐请求、
连续 buffer、trace 开关等价与转换字节核对。

另对共享 DRAM 做了小规模真实 MQSim 验证，链路为 native → finite trace → converter →
MQSim，核对 READ/WRITE/TRIM 字节和命令完成数：

| policy | 请求数 | DRAM 总量/峰值 | 已完成设备命令 |
|---|---:|---:|---:|
| RR | 100 | 16,384 / 16,384 B | 931 |
| adaptive_endurance | 100 | 16,384 / 16,384 B | 1,180 |

小设备使用 2 channel、8 KiB NAND page、SLC/TLC 64/128 KiB 逻辑容量，仅验证端到端功能。
它与主实验的 6 channel、16 KiB page、1/4 TB 几何不同，其 WAF 不用于主实验对照。
原始设备结果：[RR](../build/shared-device-verification/wear_share_round_robin/device_result.json)、
[adaptive](../build/shared-device-verification/adaptive_endurance/device_result.json)。

本次共享 WEKA native extension SHA-256 为
`f38cf9bd8449d5beda179ead1fc801dcbe9291376e2b6b2112d24bc05cbcdb72`；设备二进制 SHA-256 为
`256d5cca5c8696fb5877e359823bbbc5f71d6c186b9711a9f532ba3c181abb70`。

## 新配置与复现

在 Kimi-K3 profile 上只设置总 DRAM：

```python
from dwpdsim import MemoryConfig, kimi_k3_profile

memory = MemoryConfig(capacity_bytes=100_000_000_000)
hybrid = kimi_k3_profile(tokens_per_block=64).hybrid_config()
assert hybrid.pool_allocation == "shared"
assert hybrid.full_capacity_bytes == hybrid.linear_capacity_bytes == 0
```

四组逻辑回放方法如下；重新执行时使用新的输出目录，保留已记录的产物：

```bash
for tb in 1 4; do
  for policy in wear_share_round_robin adaptive_endurance; do
    .venv/bin/python benchmark/weka_kimi.py run \
      --prepared build/weka-kimi-062126/input \
      --output "build/weka-shared-reproduction/${policy}-${tb}tb" \
      --pool-allocation shared --memory-bytes 100000000000 \
      --storage-policy "$policy" --capacity-tb "$tb" \
      --requests 1000 --batch-size 16 --k 2
  done
done
```

需要交给 MQSim 时，逻辑回放显式加 `--emit-io-trace`，再调用
`benchmark/hybrid_device.py --replay <该输出目录> --capacity-tb 1|4 --binary <MQSim路径>`。
无限后备的历史性能脚本 `benchmark/kimi_no_trace.py` 显式保留 fixed 分区，用于既有版本
性能对照；共享 DRAM 的容量和命中实验使用上面的 runner。
