# 混合注意力支持：首轮验证

日期：2026-10-19。分支：`feat/hybrid-attention-support`；基线提交：`01251ab205346a683f0e90a6b3029fd33d2ebd31`，实现位于该分支工作区。公开接口和限制见 [使用说明](../docs/hybrid-attention-usage.md)。

后续兼容性与假阳性专项检查及修复见 [实现 review](hybrid-attention-review.md)。

## 实现范围

默认关闭的同步 backend 对象回放，公共 hash RadixTree、独立 FULL/LINEAR 组件状态与 DRAM 配额、显式供给、精确 endpoint 需求、联合可用性判定。提供 checkpoint LRU、衰减频率、收益/字节排序，以及对象批处理和连续 uint64 列式输入。

首版使用无限容量后备存储及同步逻辑 READ/WRITE；有限 SSD、异步状态与恢复点选择尚未实现，相关输入不被当作完整可用副本。关闭开关时原有 API、策略、指标与 canonical trace 继续工作。

## 正确性与兼容性

- 原基线在重建扩展后通过 267 项 Python 测试。
- 新增 44 项混合公开功能测试，包含 10 组 checkpoint LRU 随机账本对照、10 组频率/收益全扫描对照；每步比较实际组件命中和副本状态。
- 既有 4 项与新增 1 项 C++ 集成测试在 Debug CTest 中通过。
- 4 种旧 memory policy 与 7 种 storage policy 的 28 组固定回放，在改造前后比较确定性 stats 与原始 trace 内容，结果相同。使用默认关闭配置；覆盖有限/无限存储和后台推进。
- 主 `.venv`：309 passed，2 项可选依赖相关测试 skipped。包含完整可选依赖的 `build/test-venv` 用于运行全部 311 项。
- Ruff 与 `git diff --check` 通过。既有断言没有为适应新代码而改写。

这些验证覆盖声明的同步身份/事件模型，不等于真实 GPU 数值验证或所有输入上的形式化证明。

## 小规模效果回放

固定 backend 需求/供给的因果合成工作负载：5 个种子，每种子 400 个请求、8 个会话；每次请求后发布 FULL 前缀与 LINEAR endpoint；30% 概率追加一个 block，其余复用当前路径。FULL 配额设置为足以容纳全部对象，隔离 LINEAR 淘汰影响。

LINEAR 对象 16 字节，分别设置 2/8 个槽位；alpha=1、衰减 60 秒、回填和 dump-on-eviction 开启；3 种策略共 30 次运行。以下是 5 个种子总和：

| LINEAR 槽位 | 策略 | 逻辑 checkpoint READ 次数 | 逻辑 WRITE 字节 |
|---:|---|---:|---:|
| 2 | checkpoint_lru | 1040 | 9904 |
| 2 | frequency | 1057 | 9952 |
| 2 | benefit | 1057 | 9952 |
| 8 | checkpoint_lru | 270 | 9520 |
| 8 | frequency | 587 | 9920 |
| 8 | benefit | 587 | 9920 |

每个种子/配额下，各策略的联合可满足请求数和 FULL 指标相同。frequency 与 benefit 的完整统计相同，符合等大小、同读取成本时分数只差常数的退化预期。

该追加型负载中频率/收益排序没有改善 LRU。高频历史 endpoint 会在追加后失去部分需求，保留它们可能挤出更新状态。这支持保留 checkpoint LRU 作为主要基线，并在真实 endpoint 需求、历史分叉负载上继续研究；不能据此认定一种排序在生产中普遍更优。

本轮只验证逻辑 I/O 和实验链路，没有测量实际 TTFT、吞吐、NAND WAF 或耐久收益。

## 本地证据与复现

本轮生成物位于忽略的 `build/` 目录：

- `hybrid-legacy-before.json`、`hybrid-legacy-final.json`：28 组改造前后快照。
- `hybrid-final-pytest.txt`、`hybrid-final-full-pytest.txt`、`hybrid-ctest.txt`：测试结果。
- `hybrid-smoke-experiment/input.json`、`events-*.jsonl`：合成规则和输入 SHA。
- `hybrid-smoke-experiment/provenance.json`：源码 SHA、工作区 diff 和新增源码摘要。
- `hybrid-smoke-experiment/results.json`、`summary.csv`、`*.hybrid.csv`：逐运行统计和逻辑 I/O。

小规模实验的准备与执行命令：

```bash
.venv/bin/python benchmark/hybrid_memory_experiment.py prepare \
  --requests 400 --sessions 8 --seeds 5 --output build/hybrid-smoke-experiment
.venv/bin/python benchmark/hybrid_memory_experiment.py run \
  --slots 2 8 --output build/hybrid-smoke-experiment
.venv/bin/python benchmark/hybrid_memory_experiment.py report \
  --output build/hybrid-smoke-experiment
```

改造前快照必须在原基线上生成；不能使用改造后的模拟器重新生成它来宣称兼容。当前快照保留在本地，未纳入源码。
