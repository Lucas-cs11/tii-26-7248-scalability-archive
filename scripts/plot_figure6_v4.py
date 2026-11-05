#!/usr/bin/env python3
"""Figure 6 v4, inheriting the v3 IEEE/STIX visual system."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap
import numpy as np


matplotlib.rcParams.update({
    "mathtext.fontset": "stix", "font.family": "STIXGeneral",
    "font.size": 7.5, "axes.titlesize": 8, "axes.labelsize": 7.5,
    "xtick.labelsize": 6.8, "ytick.labelsize": 6.8, "legend.fontsize": 6.0,
    "figure.dpi": 300, "savefig.dpi": 300, "savefig.bbox": "tight",
    "savefig.pad_inches": 0.05, "axes.linewidth": 0.6,
    "axes.spines.top": False, "axes.spines.right": False,
    "xtick.major.width": 0.5, "ytick.major.width": 0.5,
    "xtick.major.size": 3, "ytick.major.size": 3,
    "lines.linewidth": 1.0, "lines.markersize": 3.0, "patch.linewidth": 0.5,
    "pdf.fonttype": 42, "ps.fonttype": 42, "svg.fonttype": "none",
})
C_DARK, C_BLUE, C_MID, C_WARN = "#04182E", "#1D5E9E", "#8FB8DC", "#B5651D"
C_GREEN = "#4E7A4E"
LIGHT = {1: "#D9E0E6", 2: "#D8E7F5", 3: "#DCE8DC"}


def load_records(path):
    return [json.loads(line) for line in Path(path).open()]


def pct(values, q):
    return float(np.percentile(values, q))


def panel_label(ax, tag):
    ax.text(-0.18, 1.07, f"({tag})", transform=ax.transAxes,
            fontweight="bold", fontsize=8, va="bottom")


def panel_a(ax, records):
    rows = [r for r in records if r["experiment"] == "wip"]
    size_by_ops = {}
    for r in rows:
        ops = int(r["params"]["ops_per_part"])
        if r.get("n_places") is not None:
            size_by_ops[ops] = r["n_places"] + r["n_transitions"]
    colors = {1: C_DARK, 2: C_BLUE, 3: C_GREEN}
    for wip in (1, 2, 3):
        xs, medians, lows, highs, censored = [], [], [], [], []
        for ops in (1, 2, 3):
            group = [r for r in rows if int(r["params"]["ops_per_part"]) == ops
                     and int(r["params"]["wip_bound"]) == wip]
            xs.append(size_by_ops[ops])
            if any(r["execution_status"] == "TIMEOUT" for r in group):
                medians.append(30.0); lows.append(np.nan); highs.append(np.nan); censored.append(True)
            else:
                values = [r["runtime_s"] for r in group]
                medians.append(float(np.median(values)))
                lows.append(pct(values, 50)); highs.append(pct(values, 95)); censored.append(False)
        ax.plot(xs, medians, "o--" if any(censored) else "o-", color=colors[wip],
                label=f"WIP = {wip}", zorder=3)
        valid = [i for i, flag in enumerate(censored) if not flag]
        for i in valid:
            ax.errorbar(
                xs[i], medians[i],
                yerr=[[0], [highs[i] - medians[i]]],
                color=colors[wip], capsize=1.8, capthick=0.6,
                linewidth=0.6, zorder=4,
            )
        if len(valid) >= 2:
            vx = [xs[i] for i in valid]
            ax.fill_between(vx, [lows[i] for i in valid], [highs[i] for i in valid],
                            color=LIGHT[wip], linewidth=0)
        for i, flag in enumerate(censored):
            if flag:
                ax.plot(xs[i], 30, marker="^", color=C_WARN, markersize=3.8, zorder=5)
                ax.annotate(r"$\geq$30 s", (xs[i], 30), xytext=(0, 4),
                            textcoords="offset points", ha="center", fontsize=5.2,
                            color=C_WARN)
    ax.set_yscale("log")
    ax.set_xticks([size_by_ops[o] for o in (1, 2, 3)])
    ax.set_xlabel(r"TPN size $|P|+|T|$")
    ax.set_ylabel("Runtime (s); median--P95")
    ax.set_title("Runtime by size and WIP", loc="left")
    ax.legend(frameon=False, loc="upper left", handlelength=1.4)
    panel_label(ax, "a")


def panel_b(ax, records):
    rows = [r for r in records if r["experiment"] == "wip"]
    matrix = np.zeros((3, 3))
    labels = [["" for _ in range(3)] for _ in range(3)]
    for oi, ops in enumerate((1, 2, 3)):
        for wi, wip in enumerate((1, 2, 3)):
            group = [r for r in rows if int(r["params"]["ops_per_part"]) == ops
                     and int(r["params"]["wip_bound"]) == wip]
            conclusive = sum(r.get("analysis_status") == "CONCLUSIVE" for r in group)
            timeout = sum(r["execution_status"] == "TIMEOUT" for r in group)
            inc = sum(r.get("analysis_status") == "INCONCLUSIVE" for r in group)
            matrix[oi, wi] = 100 * conclusive / len(group)
            detail = f"{conclusive}/{len(group)} conclusive"
            if timeout or inc:
                detail += f"\n{timeout} TO" + (f" + {inc} INC" if inc else "")
            labels[oi][wi] = detail
    cmap = LinearSegmentedColormap.from_list("tii_blue", ["#F4F7FA", C_MID, C_DARK])
    ax.imshow(matrix, vmin=0, vmax=100, cmap=cmap, aspect="auto")
    for i in range(3):
        for j in range(3):
            color = "white" if matrix[i, j] >= 65 else (C_WARN if matrix[i, j] == 0 else C_DARK)
            ax.text(j, i, f"{matrix[i,j]:.0f}%\n{labels[i][j]}", ha="center", va="center",
                    fontsize=5.4, color=color)
    ax.set_xticks(range(3), ["WIP = 1", "WIP = 2", "WIP = 3"])
    ax.set_yticks(range(3), ["1 op/part", "2 ops/part", "3 ops/part"])
    ax.set_xlabel("Concurrency bound")
    ax.set_ylabel("Route depth")
    ax.set_title("Conclusive verification rate", loc="left")
    ax.tick_params(length=0)
    for spine in ax.spines.values():
        spine.set_visible(False)
    panel_label(ax, "b")


def panel_c(ax, records):
    # Workload counters are deterministic across repeats; use one repeat per
    # fixed TPN, then aggregate all five preregistered seeds without filtering.
    rows = [r for r in records if r["experiment"] == "Lsweep" and r["replicate"] == 0]
    styles = {
        "L1": (C_DARK, "#D9E0E6", "L1: 1 part × 4 ops"),
        "L2": (C_BLUE, "#D8E7F5", "L2: 2 parts × 2 ops"),
    }
    for model in ("L1", "L2"):
        grouped = defaultdict(list)
        for r in rows:
            if r["model"] == model:
                grouped[int(r["L"])].append(r["counters"]["trace_records"])
        xs = sorted(grouped)
        med = [float(np.median(grouped[x])) for x in xs]
        lo = [min(grouped[x]) for x in xs]
        hi = [max(grouped[x]) for x in xs]
        color, fill, label = styles[model]
        ax.fill_between(xs, lo, hi, color=fill, linewidth=0)
        ax.plot(xs, med, "o-", color=color, label=label)
    ax.set_xlim(5.7, 13.3)
    ax.set_ylim(-5, 230)
    ax.set_xticks(range(6, 14))
    ax.set_xlabel("Observable-word bound $L$")
    ax.set_ylabel("Retained trace records\n(COMPLETE_SET)")
    ax.set_title("Bounded-history workload", loc="left")
    ax.legend(frameon=False, loc="upper left", handlelength=1.2)
    panel_label(ax, "c")


def save_all(fig, base, png_dpi=600):
    fig.savefig(f"{base}.pdf")
    fig.savefig(f"{base}.svg")
    fig.savefig(f"{base}.eps")
    fig.savefig(f"{base}.png", dpi=png_dpi)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--records", required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    records = load_records(args.records)
    args.out.mkdir(parents=True, exist_ok=True)
    panels = args.out / "panels"
    panels.mkdir(exist_ok=True)
    specs = [("a", panel_a), ("b", panel_b), ("c", panel_c)]
    for tag, fn in specs:
        fig, ax = plt.subplots(figsize=(2.30, 1.78))
        fn(ax, records)
        save_all(fig, panels / f"fig06{tag}", png_dpi=600)
        plt.close(fig)
    fig = plt.figure(figsize=(7.2, 2.35))
    w, gap, y, h = 0.275, 0.075, 0.19, 0.70
    for i, (_, fn) in enumerate(specs):
        ax = fig.add_axes([0.055 + i * (w + gap), y, w, h])
        fn(ax, records)
    save_all(fig, args.out / "fig06_bounded_verification_scalability", png_dpi=600)
    plt.close(fig)
    print(f"Figure 6 written to {args.out}")


if __name__ == "__main__":
    main()
