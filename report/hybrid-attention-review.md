# 混合注意力实现 review：兼容性与命中正确性

日期：2026-10-19。范围：`feat/hybrid-attention-support` 当前工作区相对基线 `01251ab` 的实现。发现的问题已直接修复，未改变原有测试断言或基线快照。

## 已修复的问题

### 1. Node/RadixTree 的 C++ 值语义与初始化不兼容

初始实现把 `unique_ptr<HybridNodeState>` 放在 Node 首字段，导致原来的 `Node{42, 10}` 初始化不再编译；Node 和包含它的 RadixTree 拷贝也因 unique_ptr 被删除。即使默认关闭混合功能，这些 C++ 源码使用方式仍然被破坏。

已将混合字段移到原数据字段之后，并改成只在需要时分配的值语义存储：关闭模式不分配组件状态；复制时深拷贝混合数据；移动保留 noexcept。新增 C++ 集成检查覆盖原聚合初始化、Node 复制/赋值、RadixTree 复制/赋值，以及副本修改不影响原对象。

相关实现：`cpp/include/dwpdsim/types.hpp`、`hybrid_types.hpp`。

### 2. const 树视图可间接修改混合驻留

const unique_ptr 的 operator-> 仍返回可变指针，因此 policy 虽然只得到 const RadixTree，也能修改混合组件的驻留。这违反只读 policy 契约，存在绕过执行器制造错误可用状态的风险；本轮未发现现有策略实际进行了这种修改。

已提供区分 const/non-const 的组件访问：const 树只返回 const HybridNodeState；只有可变树访问才能创建或修改状态。新增编译期接口检查，并验证复制的树不共享可变混合组件。

相关实现：`cpp/include/dwpdsim/hybrid_types.hpp`。

## 关闭开关时的检查

- hybrid runtime、组件状态和 `.hybrid.csv` 都由显式开关控制，默认路径仍执行原 policy 和原 I/O。
- 原 Node 字段顺序与原有聚合初始化前缀已恢复；公开值复制得到独立数据。
- 原有 Python 调用、单池容量、global miss 插入、Dump/Drop、storage placement/migration、后台 tick、stats 和 canonical trace 保持旧模式语义。
- 使用改造前保留的 28 组 memory/storage 组合结果比较确定性统计和原始 I/O 内容；不通过改写期望结果或重新生成基线消除差异。

这里核验的是功能与源码使用方式，不承诺新增字段后的 C++ 二进制布局或实际 CPU 耗时不变；扩展需要正常重建。

## 假阳性命中检查

逐项检查了 exact prefix/parent 身份、组件类型、endpoint 边界、发布历史与当前副本的区别，以及请求全部组件先判定、后回填的顺序。

在声明的单 namespace、前缀唯一 hash、同步串行事件模型下，未复现有效输入导致的假阳性。新增 21 项公开功能测试，加强以下证据：

- 两个独立对象账本、两个独立 LRU 集合，逐事件预测 FULL/LINEAR 与联合结果；覆盖 5 个种子 × 下刷/丢弃 × 回填/旁路，共 20 组。
- 同时核验每个组件的 DRAM/SSD 状态和字节占用，避免只检查最终命中率。
- 专项检查“过去 FULL 命中过、随后最后副本被丢弃，再发布 LINEAR”不能累积成当前联合命中。
- 现有测试继续覆盖 FULL 区间缺口、错误分支、较深 checkpoint 不能替代较早边界、尚未发布、最后副本丢失，以及不支持的 pending/partial 事件被拒绝。

新增账本不调用 C++ 命中函数，不读取其标志来决定预期结果。策略的只读状态边界已在 C++ 层收紧。

这些结论不覆盖未实现的异步、有限 SSD 回收、真实 payload 数值正确性或外部 hash 碰撞。相关功能尚未开启，不以假定成功的状态参与命中统计。

## 验证结果

重建后的最终结果：

- 完整依赖环境：332 项 Python 测试通过，其中 65 项为混合功能测试。
- 主 `.venv`：330 passed，2 skipped；跳过项来自可选 `datasets` 未安装，完整环境已执行通过。
- Debug CTest：5/5 通过，包含新增的公开初始化、复制与 const 访问契约检查。
- 28 组旧 memory/storage 组合的确定性统计与原始 trace，与改造前快照完全相同；两个 Python 环境均核对。
- Ruff、现有与新增文件的空白检查通过。

结果同时记录于 `PROGRESS.md`。本地证据文件：

- `build/hybrid-review-before-copy.txt`：修复前 C++ 初始化/复制的编译失败。
- `build/hybrid-review-legacy-contract.cpp`：同一公开使用样例，修复后编译成功。
- `build/hybrid-review-pytest.txt`：全量 Python 测试。
- `build/hybrid-review-ctest.txt`：Debug CTest。
- `build/hybrid-review-legacy.json` 与原 `build/hybrid-legacy-before.json`：旧模式回放比较。

没有遗留的已确认功能问题。异步、有限存储和其他后续能力的范围仍见 [使用说明](../docs/hybrid-attention-usage.md)。
