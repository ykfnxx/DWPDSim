"""Plot no-hint residency ablations and completed Memory residency spells."""

import argparse
import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import report_reuse_policy


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, default=Path("build/residency-nohint"))
    a = p.parse_args()
    report_reuse_policy.main(a.output)
    rows = json.loads((a.output / "results.json").read_text())
    fig, axes = plt.subplots(2, 2, figsize=(13, 8), layout="constrained")
    for ax, (dataset, capacity) in zip(
        axes.flat, [(d, c) for d in ["unscaled", "tracelab"] for c in [128, 256]]
    ):
        for row in rows:
            if row["dataset"] != dataset or row["memory_gib"] != capacity:
                continue
            r = json.loads(
                (a.output / dataset / f"{capacity * 128}-{row['variant']}/result.json").read_text()
            )
            counts = np.asarray(r["work"]["evicted_residency_bins"])
            assert int(counts.sum()) == r["stats"]["memory"]["evicted_blocks"]
            ax.plot(range(8), np.cumsum(counts) / counts.sum(), marker="o", label=row["variant"])
        ax.set(
            title=f"{dataset}, {capacity} GiB",
            ylabel="CDF of completed block residency spells",
            xticks=range(8),
            xticklabels=["1s", "4s", "16s", "60s", "5m", "30m", "2h", "all"],
            xlabel="Time from Memory insertion to eviction (< upper edge)",
            ylim=(0, 1.02),
        )
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside lower center", ncol=3)
    fig.suptitle(
        "Actual residency, not retention_ns\nBlock-weighted completed spells; surviving blocks are excluded"
    )
    for ext in ["png", "svg"]:
        fig.savefig(a.output / f"residency_cdf.{ext}", dpi=170, bbox_inches="tight")
    plt.close(fig)
    forecasts = json.loads((a.output / "forecast_check.json").read_text())
    fig, axes = plt.subplots(1, 2, figsize=(11, 4), layout="constrained")
    for ax, dataset in zip(axes, ["unscaled", "tracelab"]):
        points = [x for x in forecasts[dataset] if x["horizon_s"] == 60]
        ages = [x["age_s"] for x in points]
        for field, label, style in [
            ("return_predicted", "Session return: predicted", "o--"),
            ("return_observed", "Session return: observed", "o-"),
            ("block_reuse_predicted", "Block reuse: predicted", "s--"),
            ("block_reuse_observed", "Block reuse: observed", "s-"),
        ]:
            ax.plot(ages, [x[field] for x in points], style, label=label)
        ax.set(
            title=dataset,
            xlabel="Already waiting (seconds)",
            ylabel="Probability within the next 60 seconds",
            ylim=(0, 1),
        )
    axes[0].legend(fontsize=8)
    fig.suptitle("Held-out calibration; only fully observed future windows")
    for ext in ["png", "svg"]:
        fig.savefig(a.output / f"forecast_calibration.{ext}", dpi=170, bbox_inches="tight")
    plt.close(fig)
    baseline = {(r["dataset"], r["memory_gib"]): r for r in rows if r["variant"] == "lru-300"}
    comparisons = []
    for r in rows:
        b = baseline[r["dataset"], r["memory_gib"]]
        comparisons.append(
            {
                **r,
                "hit_change_vs_lru_pp": r["hit_pct"] - b["hit_pct"],
                "read_change_vs_lru_pct": 100 * (r["read_blocks"] / b["read_blocks"] - 1),
                "write_change_vs_lru_pct": 100 * (r["write_blocks"] / b["write_blocks"] - 1),
                "compute_change_vs_lru_pct": 100
                * ((100 + r["cost_increase_pct"]) / (100 + b["cost_increase_pct"]) - 1),
            }
        )
    (a.output / "comparisons.json").write_text(json.dumps(comparisons, indent=2) + "\n")
    core_rows = []
    for r in comparisons:
        raw = json.loads(
            (
                a.output / r["dataset"] / f"{int(r['memory_gib'] * 128)}-{r['variant']}/result.json"
            ).read_text()
        )
        core_rows.append(
            {
                key: r[key]
                for key in [
                    "dataset",
                    "memory_gib",
                    "variant",
                    "hit_pct",
                    "hit_change_vs_lru_pp",
                    "write_blocks",
                    "write_change_vs_lru_pct",
                    "compute_change_vs_lru_pct",
                ]
            }
            | {
                "write_tib": r["write_blocks"] / 131072,
                "compute_cost": raw["stats"]["accesses"]["compute_cost"],
            }
        )
    with (a.output / "core_metrics.csv").open("w") as f:
        writer = csv.DictWriter(f, fieldnames=list(core_rows[0]))
        writer.writeheader()
        writer.writerows(core_rows)

    fig, axes = plt.subplots(4, 3, figsize=(15, 14), layout="constrained")
    for row_index, (dataset, capacity) in enumerate(
        [(d, c) for d in ["unscaled", "tracelab"] for c in [128, 256]]
    ):
        selected = [
            r for r in comparisons if r["dataset"] == dataset and r["memory_gib"] == capacity
        ]
        for col, (field, scale, title) in enumerate(
            [
                ("hit_change_vs_lru_pp", 1, "Prefix hit-rate gain (pp)"),
                ("write_change_vs_lru_pct", -1, "Logical write reduction (%)"),
                ("compute_change_vs_lru_pct", -1, "Compute-cost reduction (%)"),
            ]
        ):
            ax = axes[row_index, col]
            values = [r[field] * scale for r in selected]
            bars = ax.bar(
                range(len(selected)), values, color=plt.get_cmap("tab10").colors[: len(selected)]
            )
            ax.axhline(0, color="black", linewidth=0.7)
            ax.bar_label(
                bars,
                labels=[
                    f"{round(x, 4) + 0.0:.4f}" if col == 0 else f"{round(x, 3) + 0.0:.3f}"
                    for x in values
                ],
                fontsize=8,
                padding=3,
            )
            lo, hi = min(0, min(values)), max(0, max(values))
            padding = max((hi - lo) * 0.2, 0.001 if col == 0 else 0.01)
            ax.set_ylim(lo - padding, hi + padding)
            ax.set(
                title=f"{dataset}, {capacity} GiB\n{title}",
                xticks=range(len(selected)),
                xticklabels=[r["variant"] for r in selected],
            )
            ax.tick_params(axis="x", rotation=45)
    fig.suptitle("No-hint core metrics vs LRU/300s: higher is better in every panel", fontsize=13)
    for ext in ["png", "svg"]:
        fig.savefig(a.output / f"core_metrics.{ext}", dpi=170, bbox_inches="tight")
    plt.close(fig)


if __name__ == "__main__":
    main()
