"""Plot the completed chronological reuse-policy experiment without modifying traces."""

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


def main(output=None):
    if output is None:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--output", type=Path, default=Path("build/reuse-policy-experiment"))
        output = parser.parse_args().output
    out = Path(output)
    rows = json.loads((out / "results.json").read_text())
    names = list(dict.fromkeys(r["variant"] for r in rows))
    colors = dict(zip(names, plt.get_cmap("tab10").colors))
    markers = dict(zip(names, ["o", "s", "^", "v", "D", "P", "X", "*", "<", ">"]))
    plt.rcParams.update({"font.size": 9, "axes.grid": True, "grid.alpha": 0.2})
    fig, axes = plt.subplots(2, 2, figsize=(13, 9), layout="constrained")
    for ax, (data, cap) in zip(
        axes.flat, [(d, c) for d in ["unscaled", "tracelab"] for c in [128, 256]]
    ):
        for r in rows:
            if r["dataset"] != data or r["memory_gib"] != cap:
                continue
            ax.scatter(
                r["write_reduction_pct"],
                r["cost_increase_pct"],
                color=colors[r["variant"]],
                label=r["variant"],
                marker=markers[r["variant"]],
                s=80,
            )
        ax.set(
            title=f"{data}, {cap} GiB",
            xlabel="Logical write reduction vs dump-all (%)",
            ylabel="Compute-cost increase (%)",
        )
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside lower center", ncol=5, fontsize=9)
    fig.suptitle(
        "Chronological holdout: fit 0–6h, cold replay 6–24h\nLower compute cost and higher write reduction are preferable"
    )
    for ext in ["png", "svg"]:
        fig.savefig(out / f"write_compute_tradeoff.{ext}", dpi=170, bbox_inches="tight")
    plt.close(fig)
    fig, axes = plt.subplots(2, 2, figsize=(13, 8), layout="constrained", sharex="col")
    for col, cap in enumerate([128, 256]):
        rr = [r for r in rows if r["dataset"] == "tracelab" and r["memory_gib"] == cap]
        for row, (field, title) in enumerate(
            [
                ("loop_memory_missing_pct", "Previously seen block absent from Memory"),
                ("loop_missing_pct", "Previously seen block absent from all tiers"),
            ]
        ):
            ax = axes[row, col]
            ax.bar(range(len(rr)), [r[field] for r in rr], color=[colors[r["variant"]] for r in rr])
            ax.set(
                xticks=list(range(len(rr))),
                xticklabels=[r["variant"] for r in rr],
                ylabel="Within-loop requests affected (%)",
                title=f"{cap} GiB: {title}",
            )
            if row:
                ax.set_yscale("symlog", linthresh=0.01)
                ax.tick_params(axis="x", rotation=65)
    fig.suptitle(
        "TraceLab coding: within-loop historical-block misses\nBottom row: symlog scale above 0.01%; new blocks excluded"
    )
    for ext in ["png", "svg"]:
        fig.savefig(out / f"loop_missing.{ext}", dpi=170, bbox_inches="tight")
    plt.close(fig)
    fig, axes = plt.subplots(2, 2, figsize=(13, 9), layout="constrained")
    for ax, (data, cap) in zip(
        axes.flat, [(d, c) for d in ["unscaled", "tracelab"] for c in [128, 256]]
    ):
        rr = [r for r in rows if r["dataset"] == data and r["memory_gib"] == cap]
        ax.bar(
            range(len(rr)),
            [r["read_blocks"] / 1e6 for r in rr],
            color=[colors[r["variant"]] for r in rr],
        )
        ax.set(
            xticks=list(range(len(rr))),
            xticklabels=[r["variant"] for r in rr],
            ylabel="Storage READ blocks (million)",
            title=f"{data}, {cap} GiB",
        )
        ax.tick_params(axis="x", rotation=65)
    for ext in ["png", "svg"]:
        fig.savefig(out / f"storage_reads.{ext}", dpi=170, bbox_inches="tight")
    plt.close(fig)
    lines = [
        "# 两份合成 trace 的 reuse_lru 实验",
        "",
        "训练 [0,6h)，评估 [6h,24h) 冷启动；128/256 GiB、8 MiB/block、无限 Storage。",
        "标签只用于离线分析。写入与 compute cost 相对同数据/容量的 bounded-LRU dump-all。",
        "compute cost 是 block² 代理。历史 miss 排除评估窗口内首次出现的 block。",
        "",
        "| 数据 | GiB | 变体 | 总命中 % | 物理内存命中 % | 写入减少 % | cost增加 % | READ百万blocks | loop历史全局miss请求 % | loop历史内存miss请求 % |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        fm = lambda x: "—" if x is None else f"{x:.4f}"
        lines.append(
            f"| {r['dataset']} | {r['memory_gib']:.0f} | {r['variant']} | {r['hit_pct']:.4f} | "
            f"{r['memory_hit_pct']:.4f} | {r['write_reduction_pct']:.2f} | {r['cost_increase_pct']:.2f} | "
            f"{r['read_blocks'] / 1e6:.3f} | {fm(r['loop_missing_pct'])} | {fm(r['loop_memory_missing_pct'])} |"
        )
    lines += [
        "",
        "![写入与计算代价](write_compute_tradeoff.png)",
        "",
        "![轮内缺失](loop_missing.png)",
        "",
        "![Storage读取](storage_reads.png)",
        "",
    ]
    (out / "report.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
