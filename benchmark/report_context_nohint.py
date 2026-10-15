"""Render matched context-LRU comparisons using the three core metrics."""

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT = Path("build/context-nohint")


def main():
    rows = json.loads((OUT / "comparison.json").read_text())
    assert len(rows) == 24
    fig, axes = plt.subplots(4, 3, figsize=(15, 13), layout="constrained")
    for index, (dataset, capacity) in enumerate(
        [(d, c) for d in ["unscaled", "tracelab"] for c in [128, 256]]
    ):
        rr = [r for r in rows if r["dataset"] == dataset and r["memory_gib"] == capacity]
        for col, (key, title) in enumerate(
            [
                ("hit_pct", "Prefix hit change vs no Drop (pp)"),
                ("storage_write_tib", "Storage writes (TiB)"),
                ("compute_change_pct", "Compute increase vs no Drop (%)"),
            ]
        ):
            ax = axes[index, col]
            base_hit = next(r["hit_pct"] for r in rr if r["variant"] == "dump-all")
            values = [r[key] - base_hit if col == 0 else r[key] for r in rr]
            bars = ax.bar(range(len(rr)), values, color=plt.get_cmap("tab10").colors[: len(rr)])
            ax.bar_label(bars, labels=[f"{x:.3f}" for x in values], fontsize=8, padding=3)
            lo, hi = min(0, min(values)), max(0, max(values))
            padding = max((hi - lo) * 0.2, 0.002 if col == 0 else 0.02)
            ax.set_ylim(lo - padding, hi + padding)
            ax.set(
                title=f"{dataset}, {capacity} GiB\n{title}",
                xticks=range(len(rr)),
                xticklabels=[r["variant"] for r in rr],
            )
            ax.tick_params(axis="x", rotation=45)
            ax.grid(axis="y", alpha=0.2)
    fig.suptitle("No-hint comparison: matched replay window, readmission, alpha and eviction grain")
    for ext in ["png", "svg"]:
        fig.savefig(OUT / f"core_comparison.{ext}", dpi=170, bbox_inches="tight")
    plt.close(fig)
    direct = []
    for r in rows:
        if not r["variant"].startswith("context-"):
            continue
        b = next(
            x
            for x in rows
            if x["dataset"] == r["dataset"]
            and x["memory_gib"] == r["memory_gib"]
            and x["variant"] == "value-60"
        )
        direct.append(
            {
                "dataset": r["dataset"],
                "memory_gib": r["memory_gib"],
                "variant": r["variant"],
                "hit_change_pp": r["hit_pct"] - b["hit_pct"],
                "storage_write_change_pct": 100
                * (r["storage_write_blocks"] / b["storage_write_blocks"] - 1),
                "compute_change_pct": 100 * (r["compute_cost"] / b["compute_cost"] - 1),
            }
        )
    (OUT / "vs_prediction.json").write_text(json.dumps(direct, indent=2) + "\n")
    lines = [
        "# Context LRU 与概率驻留策略：同口径对照",
        "",
        "全部使用同一[6h,24h)评估窗口，空缓存、空session历史起步。128/256 GiB、8 MiB/block、infinite_storage、开启Storage命中回填、alpha=0.1、每次最多回收尾部64 blocks、不向父段回收；finish不刷剩余缓存。策略没有task、用户轮次或agent结束hint。",
        "",
        "旧context-lru-retention报告使用全天数据、关闭回填、alpha=0.01，不能与这里的概率模型结果直接拼表。因此补跑8组context LRU，复用已验证的预测策略与LRU结果。",
        "",
        "- context-b0：按浅上下文优先选择冷候选，基础retention=300s，beta=0。",
        "- context-b20：相同候选选择，retention=300s+20秒×上次已完成请求的非负context增量（block）；动态回收粒度关闭。这是含增长自适应的对照。",
        "- value-60/value-300：返回概率×前缀保留概率×恢复代价，数字是预测窗口，Dump/Drop阈值仍为300s。",
        "- dump-all：LRU选择、不Drop，作为下表写入和重算变化的共同基线；lru-300：LRU选择、固定300s。",
        "",
        "命中率为Memory+Storage连续前缀block命中率。写入是Storage逻辑写入，不含Memory回填或SSD内部写放大。compute_cost为包含冷miss计算的block²代理；相对同容量dump-all报告变化。",
        "",
        "| 数据 | GiB | 策略 | 命中率 % | Storage写入 TiB | Storage写入变化 % | 重算代价变化 % |",
        "|---|---:|---|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['dataset']} | {r['memory_gib']:.0f} | {r['variant']} | {r['hit_pct']:.4f} | {r['storage_write_tib']:.3f} | {r['storage_write_change_pct']:+.2f} | {r['compute_change_pct']:+.2f} |"
        )
    lines += [
        "",
        "## 直接相对value-60的变化",
        "",
        "命中率单位为百分点；Storage写入和重算变化为负表示减少。",
        "",
        "| 数据 | GiB | 策略 | 命中变化 pp | Storage写入变化 % | 重算变化 % |",
        "|---|---:|---|---:|---:|---:|",
    ]
    for r in direct:
        lines.append(
            f"| {r['dataset']} | {r['memory_gib']:.0f} | {r['variant']} | {r['hit_change_pp']:+.4f} | {r['storage_write_change_pct']:+.2f} | {r['compute_change_pct']:+.2f} |"
        )
    lines += [
        "",
        "![核心指标](../build/context-nohint/core_comparison.png)",
        "",
        "[CSV](../build/context-nohint/comparison.csv) · [JSON](../build/context-nohint/comparison.json) · [配置与输入校验](../build/context-nohint/provenance.json)",
        "",
        "本次只比较预先指定的beta=0/20，未扫描其他beta或alpha；不宣称得到全局最优参数。完整配置和逐请求统计在build/context-nohint内。生成产物不提交。",
        "",
        "```sh",
        "build/test-venv/bin/python benchmark/context_nohint_comparison.py run --workers 8",
        "python benchmark/report_context_nohint.py",
        "```",
        "",
    ]
    Path("report/context-nohint-comparison.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
