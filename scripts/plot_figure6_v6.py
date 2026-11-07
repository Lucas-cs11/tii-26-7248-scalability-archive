#!/usr/bin/env python3
"""Figure 6 (a-c) for the v6 structural-scalability experiment.

Panels
------
6(a) structural scalability : model size vs cell analysis time (A_structural)
6(b) WIP sensitivity        : paired WIP = 1/2/3 cells on the same base model
6(c) L sensitivity          : fixed model/H, increasing depth limit L, with
                              explicit marking of the models that triggered
                              L-sensitive work (candidate words).

The script consumes the *normalized* tables written by
``scripts/analyze_v6_formal_results.py`` and never reads raw artifacts, so the
figure is exactly reproducible from the CSVs/JSON that ship with it.

Censoring rules drawn on the figure
-----------------------------------
* A TIMEOUT cell is a right-censored observation at the cell cap.  With few
  cells it is drawn as an open triangle sitting on the dashed ceiling line and
  never at its wall-clock value as if it were a completion time.  With large
  formal samples the panels switch to median + IQR of COMPLETED cells plus a
  clearly labelled censored-share line, so censored cells still contribute to
  the story without being imputed.
* INC ("completed but inconclusive") is plotted at its real, non-zero analysis
  time with a hollow diamond; it is not folded into VER/AMB and not zeroed.
* Log time axes; the shared outcome legend states each rule.

Outputs (into ``--out``): SVG, PDF, EPS, PNG, ``figure6_caption.md``,
``figure6_data.csv`` (every plotted point) and ``figure6_metadata.json``.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.ticker import LogLocator, NullFormatter  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MATPLOTLIB_VERSION = matplotlib.__version__

matplotlib.rcParams.update({
    "mathtext.fontset": "stix", "font.family": "STIXGeneral",
    "font.size": 7.2, "axes.titlesize": 7.8, "axes.labelsize": 7.2,
    "xtick.labelsize": 6.6, "ytick.labelsize": 6.6, "legend.fontsize": 5.4,
    "figure.dpi": 300, "savefig.dpi": 300, "savefig.bbox": "tight",
    "savefig.pad_inches": 0.04, "axes.linewidth": 0.6,
    "axes.spines.top": False, "axes.spines.right": False,
    "xtick.major.width": 0.5, "ytick.major.width": 0.5,
    "xtick.major.size": 3, "ytick.major.size": 3,
    "xtick.minor.size": 1.6, "ytick.minor.size": 1.6,
    "lines.linewidth": 0.9, "lines.markersize": 3.4, "patch.linewidth": 0.5,
    "pdf.fonttype": 42, "ps.fonttype": 42, "svg.fonttype": "none",
})

C_DARK = "#04182E"
C_BLUE = "#1D5E9E"
C_LIGHT = "#8FB8DC"
C_PALE = "#DCE8F2"
C_WARN = "#B5651D"
C_ORANGE = "#D08A3E"
C_ORANGE_LIGHT = "#E8C08A"
C_GREEN = "#4E7A4E"
C_TEAL = "#2F6B6E"
C_GREY = "#6E6E6E"
C_CAP = "#444444"
PALETTE = [C_DARK, C_BLUE, C_LIGHT, C_WARN, C_ORANGE, C_ORANGE_LIGHT, C_GREEN]


# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------
def as_float(value):
    if value in (None, "", "None", "nan"):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def as_int(value):
    number = as_float(value)
    return None if number is None else int(number)


def load_csv(path):
    with path.open() as handle:
        return list(csv.DictReader(handle))


def normalize(row):
    row = dict(row)
    row["_compute"] = as_float(row.get("runtime_compute_s"))
    row["_wall"] = as_float(row.get("wall_s"))
    row["_completed"] = row.get("execution_status") == "COMPLETED"
    row["_timeout"] = row.get("execution_status") == "TIMEOUT_WALL_CLOCK"
    row["_inc"] = row.get("verdict") == "INC"
    row["_verdict"] = row.get("verdict")
    row["_size"] = as_float(row.get("size_places_transitions"))
    row["_wip"] = as_int(row.get("WIP"))
    row["_L"] = as_int(row.get("L"))
    row["_candidate"] = as_float(row.get("candidate_words"))
    row["_candidate_positive"] = row["_candidate"] not in (None, 0.0)
    return row


def observed(values):
    return [value for value in values if value is not None]


def median(values):
    values = sorted(observed(values))
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return 0.5 * (values[middle - 1] + values[middle])


def percentile(values, probability):
    values = sorted(observed(values))
    if not values:
        return None
    index = max(0, min(len(values) - 1, math.ceil(probability * len(values)) - 1))
    return values[index]


def outcome(row):
    if row["_timeout"]:
        return "timeout"
    if row["_inc"]:
        return "inc"
    if row["_verdict"] == "AMB":
        return "amb"
    if row["_verdict"] == "VER":
        return "ver"
    return "other"


OUTCOME_MARKER = {"ver": "o", "amb": "s", "inc": "D", "timeout": "^",
                  "other": "x"}
OUTCOME_LABEL = {
    "ver": "VER (conclusive)",
    "amb": "AMB (conclusive)",
    "inc": "INC (completed, inconclusive)",
    "timeout": "TIMEOUT (right-censored at cap)",
}


def panel_label(ax, tag):
    ax.text(-0.155, 1.045, f"({tag})", transform=ax.transAxes,
            fontweight="bold", fontsize=8.0, va="bottom", ha="left")


def y_value(row, cap):
    """Plotted y: real analysis time, or the censoring ceiling for timeouts."""
    if row["_timeout"]:
        return cap
    if row["_compute"] is not None:
        return max(row["_compute"], 1e-5)
    return None


def color_map_for(rows, field):
    keys = sorted({row.get(field) or "unknown" for row in rows})
    return {key: PALETTE[index % len(PALETTE)] for index, key in enumerate(keys)}, keys


def log_ticks(values, ratio=1.45, max_ticks=7):
    values = sorted(set(values))
    if not values:
        return []
    if values[-1] / values[0] < 2.0:
        step = max(1, len(values) // (max_ticks - 1))
        return values[::step][:max_ticks]
    kept = [values[0]]
    for value in values[1:]:
        if value >= kept[-1] * ratio:
            kept.append(value)
    if kept[-1] != values[-1]:
        kept[-1] = values[-1]
    return kept[:max_ticks]


def short_family(family):
    return (family or "?").split("_")[0]


def add_cap_line(ax, cap):
    ax.axhline(cap, color=C_CAP, linewidth=0.65, linestyle=(0, (3.2, 2.2)),
               zorder=1)
    ax.annotate("cap (censored)", xy=(1.004, cap),
                xycoords=("axes fraction", "data"), xytext=(0.6, 0),
                textcoords="offset points", ha="left", va="center",
                rotation=90, fontsize=4.4, color=C_CAP,
                annotation_clip=False)


def add_subtitle(ax, rows):
    status = Counter(row["execution_status"] for row in rows)
    verdicts = Counter(row["_verdict"] for row in rows if row["_completed"])
    text = (f"$n={len(rows)}$, {status.get('COMPLETED', 0)} done, "
            f"{status.get('TIMEOUT_WALL_CLOCK', 0)} cens., "
            f"{verdicts.get('INC', 0)} INC")
    ax.text(0.995, 1.012, text, transform=ax.transAxes, ha="right",
            va="bottom", fontsize=4.7, color=C_DARK)


def style_time_axis(ax, rows, cap):
    values = [y_value(row, cap) for row in rows]
    values = [value for value in values if value is not None]
    low = min(values) if values else 1e-3
    ax.set_yscale("log")
    ax.set_ylim(max(1e-7, low * 0.12), cap * 2.4)
    ax.yaxis.set_minor_locator(LogLocator(base=10.0, subs=tuple(range(2, 10))))
    ax.yaxis.set_minor_formatter(NullFormatter())
    ax.set_ylabel("cell analysis time, SCG + verify (s, log)")


def plot_points(ax, rows, cap, color_map=None, color_key=None,
                fill_key=None, marker_size=15.0, color_override=None,
                open_edge=None, candidate_edge=None):
    """One marker per cell: shape = outcome, fill = candidate-word stage."""
    for row in rows:
        y = y_value(row, cap)
        if y is None:
            continue
        kind = outcome(row)
        if color_override is not None:
            color = color_override
        elif color_map:
            color = color_map.get(row.get(color_key or ""), C_BLUE)
        else:
            color = C_BLUE
        face, edge, zorder = color, color, 3
        if kind == "timeout":
            face = "none"
            edge = (candidate_edge
                    if fill_key is not None and row.get(fill_key)
                    else C_CAP)
            zorder = 4
        elif kind == "inc":
            face = "none"
        elif fill_key is not None and not row.get(fill_key):
            face = "none"
            edge = open_edge or color
        ax.plot(row["_x"], y, linestyle="none", marker=OUTCOME_MARKER[kind],
                markersize=math.sqrt(marker_size) * 1.05,
                markerfacecolor=face, markeredgecolor=edge,
                markeredgewidth=0.55, zorder=zorder)


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------
def panel_a(ax, rows, cap, stats):
    rows = [row for row in rows if row.get("experiment") == "A_structural"]
    if not rows:
        ax.text(0.5, 0.5, "no A_structural cells", ha="center", va="center",
                transform=ax.transAxes)
        return {}, []
    color_map, keys = color_map_for(rows, "series")
    for row in rows:
        row["_x"] = row["_size"]
    plot_points(ax, rows, cap, color_map=color_map, color_key="series")
    add_cap_line(ax, cap)
    ax.set_xlabel("model size (places + transitions)")
    style_time_axis(ax, rows, cap)
    sizes = [row["_x"] for row in rows if row["_x"] is not None]
    if sizes:
        ax.set_xlim(min(sizes) * 0.9, max(sizes) * 1.07)
    rho = (stats.get("groups", {}).get("A_structural", {})
           .get("spearman_completed_only", {})
           .get("size_vs_runtime_compute", {}).get("rho"))
    note = "Spearman $\\rho$ = " + (f"{rho:.2f}" if rho is not None else "n/a") \
        + " (completed only)"
    ax.text(0.985, 0.03, note, transform=ax.transAxes, ha="right",
            va="bottom", fontsize=4.6, color=C_DARK)
    add_subtitle(ax, rows)
    panel_label(ax, "a")
    return color_map, keys


def panel_b(ax, rows, cap, stats, max_lines):
    rows = [row for row in rows if row.get("experiment") == "B_wip"
            and row["_wip"] in (1, 2, 3)]
    if not rows:
        ax.text(0.5, 0.5, "no B_wip cells", ha="center", va="center",
                transform=ax.transAxes)
        return
    groups = defaultdict(dict)
    for row in rows:
        groups[row.get("base_model_id")][row["_wip"]] = row
    aggregate = len(groups) > max_lines
    if not aggregate:
        for base in groups:
            ordered = sorted((wip, groups[base][wip]) for wip in groups[base])
            completed = [(wip, cell["_compute"]) for wip, cell in ordered
                         if cell["_completed"] and cell["_compute"] is not None]
            if len(completed) >= 2:
                ax.plot([wip for wip, _ in completed],
                        [value for _, value in completed], color="#B9B9B9",
                        linewidth=0.45, zorder=1)
        for row in rows:
            row["_x"] = row["_wip"]
        plot_points(ax, rows, cap)
    else:
        # Aggregate mode: median + IQR of COMPLETED cells only, plus explicit
        # censored denominators.  No individual cell is imputed.
        for wip in (1, 2, 3):
            cells = [row for row in rows if row["_wip"] == wip]
            values = [row["_compute"] for row in cells
                      if row["_completed"] and row["_compute"] is not None]
            if not values:
                continue
            lo, hi = percentile(values, 0.25), percentile(values, 0.75)
            med = median(values)
            ax.vlines(wip, lo, hi, color=C_BLUE, linewidth=1.4, zorder=3)
            ax.plot(wip, med, marker="o", color=C_DARK, markersize=3.2,
                    zorder=5)
            censored = len(cells) - len(values)
            ax.annotate(f"$n$={len(values)}, {censored} cens.",
                        xy=(wip, hi), xytext=(0, 4), textcoords="offset points",
                        ha="center", fontsize=4.5, color=C_DARK)
    # Median of COMPLETED cells (censoring biases it low: the per-WIP count
    # is printed next to each point in aggregate mode and in the caption).
    medians = []
    for wip in (1, 2, 3):
        cells = [row for row in rows if row["_wip"] == wip]
        values = [row["_compute"] for row in cells
                  if row["_completed"] and row["_compute"] is not None]
        if values:
            medians.append((wip, median(values), len(values)))
    if medians:
        ax.plot([wip for wip, _, _ in medians],
                [value for _, value, _ in medians], color=C_DARK,
                marker="o", markersize=3.0, linewidth=0.9, zorder=5)
        if not aggregate:
            for wip, value, count in medians:
                ax.annotate(f"$n$={count}", xy=(wip, value), xytext=(4, 4),
                            textcoords="offset points", fontsize=4.6,
                            color=C_DARK)
    add_cap_line(ax, cap)
    ax.set_xlabel("WIP bound")
    ax.set_xticks([1, 2, 3])
    ax.set_xticklabels(["WIP 1", "WIP 2", "WIP 3"])
    style_time_axis(ax, rows, cap)
    ax.set_xlim(0.72, 3.28)
    ratios = (stats.get("groups", {}).get("B_wip", {})
              .get("paired_growth", {}))

    def ratio_text(wip):
        entry = ratios.get(f"wip{wip}_vs_wip1_compute_ratio", {})
        value = entry.get("median_ratio")
        count = entry.get("n_uncensored_pairs", 0)
        censored = entry.get("n_censored_pairs", 0)
        if value is None:
            return f"WIP {wip}/1: n/a"
        return f"WIP {wip}/1 $\\times${value:,.0f} ($n$={count})" \
            + (f", {censored} cens." if censored else "")
    ax.text(0.03, 0.025, ratio_text(2) + "\n" + ratio_text(3),
            transform=ax.transAxes, ha="left", va="bottom", fontsize=4.6,
            color=C_DARK, linespacing=1.45)
    add_subtitle(ax, rows)
    panel_label(ax, "b")


def panel_c(ax, rows, cap, stats, max_points):
    rows = [row for row in rows if row.get("experiment") == "C_Lsweep"]
    if not rows:
        ax.text(0.5, 0.5, "no C_Lsweep cells", ha="center", va="center",
                transform=ax.transAxes)
        return
    aggregate = len(rows) > max_points
    l_values = [row["_L"] for row in rows if row["_L"]]
    if not aggregate:
        groups = defaultdict(list)
        for row in rows:
            groups[row.get("base_model_id")].append(row)
        for base in groups:
            cells = sorted(groups[base], key=lambda r: (r["_L"], r.get("mode")))
            completed = [cell for cell in cells
                         if cell["_completed"] and cell["_compute"] is not None]
            for left, right in zip(completed, completed[1:]):
                ax.plot([left["_L"], right["_L"]],
                        [left["_compute"], right["_compute"]],
                        color="#C3C3C3", linewidth=0.4, zorder=1,
                        linestyle=(0, (2.4, 1.8))
                        if left.get("mode") != right.get("mode") else "-")
        for row in rows:
            row["_x"] = row["_L"]
        plot_points(ax, rows, cap, color_override=C_TEAL,
                    fill_key="_candidate_positive", marker_size=16.0,
                    open_edge=C_LIGHT, candidate_edge=C_TEAL)
        handles = [
            Line2D([], [], marker="o", linestyle="none", markersize=3.4,
                   markerfacecolor=C_TEAL, markeredgecolor=C_TEAL,
                   label="candidate words found"),
            Line2D([], [], marker="o", linestyle="none", markersize=3.4,
                   markerfacecolor="none", markeredgecolor=C_GREY,
                   label="no candidate words"),
            Line2D([], [], marker="^", linestyle="none", markersize=3.6,
                   markerfacecolor="none", markeredgecolor=C_TEAL,
                   label="candidate words, then capped"),
        ]
        ax.legend(handles=handles, loc="upper left", frameon=False,
                  handletextpad=0.15, borderpad=0.1, labelspacing=0.3,
                  fontsize=4.8)
        # Name the models that actually triggered L-sensitive work.
        triggered = defaultdict(int)
        for row in rows:
            if row["_candidate_positive"]:
                triggered[row["benchmark_family"]] += 1
        if triggered:
            items = [f"{short_family(family)}$\\times${count}"
                     for family, count in sorted(triggered.items())]
            shown, rest = items[:4], items[4:]
            label = "L-sensitive in " + ", ".join(shown)
            if rest:
                label += "\n" + ", ".join(rest[:5])
                if len(rest) > 5:
                    label += f", +{len(rest) - 5} more"
        else:
            label = "no candidate words at any L"
        ax.text(0.025, 0.03, label, transform=ax.transAxes, ha="left",
                va="bottom", fontsize=4.2, color=C_TEAL, linespacing=1.35)
    else:
        # Aggregate mode: per-L median + IQR of COMPLETED cells (left axis),
        # candidate-word and censoring shares (right axis, 0-1).  Censored
        # cells are excluded from the time median and appear only as a share.
        # At formal scale one mode (C1) dominates; restricting the aggregate
        # to it keeps the L contrast free of the C1/C2 mode change.
        mode_counts = Counter(row.get("mode") for row in rows)
        plot_mode = mode_counts.most_common(1)[0][0] if mode_counts else None
        by_rank = defaultdict(list)
        for row in rows:
            if (row.get("mode") == plot_mode
                    and row["_L"] is not None
                    and row.get("l_rank") not in (None, "")):
                by_rank[int(row["l_rank"])].append(row)
        xs, meds, lows, highs, cand_share, cens_share = [], [], [], [], [], []
        for rank in sorted(by_rank):
            cells = by_rank[rank]
            times = [row["_compute"] for row in cells
                     if row["_completed"] and row["_compute"] is not None]
            if not times:
                continue
            xs.append(rank)
            meds.append(median(times))
            lows.append(percentile(times, 0.25))
            highs.append(percentile(times, 0.75))
            observed_candidates = [row for row in cells
                                   if row["_candidate"] is not None]
            cand_share.append(
                sum(1 for row in observed_candidates
                    if row["_candidate_positive"]) / len(observed_candidates)
                if observed_candidates else 0.0)
            cens_share.append(
                sum(row["_timeout"] for row in cells) / len(cells))
        ax.fill_between(xs, lows, highs, color=C_PALE, linewidth=0, zorder=1)
        ax.plot(xs, meds, color=C_BLUE, marker="o", markersize=2.6,
                linewidth=1.0, zorder=3)
        twin = ax.twinx()
        twin.plot(xs, cand_share, color=C_TEAL, linestyle="--", linewidth=0.9,
                  marker="^", markersize=2.4, zorder=4)
        twin.plot(xs, cens_share, color=C_GREY, linestyle=":", linewidth=0.8,
                  zorder=4)
        twin.set_ylim(0, 1.0)
        twin.set_ylabel("share of cells (0-1)", fontsize=6.4, color=C_DARK)
        twin.tick_params(labelsize=5.8, width=0.5)
        twin.spines["top"].set_visible(False)
        onset = next((value for value, share in zip(xs, cand_share) if share > 0),
                     None)
        if onset is not None:
            ax.axvline(onset, color=C_TEAL, linewidth=0.6,
                       linestyle=(0, (1.6, 1.6)), zorder=2)
            ax.annotate("candidate-word onset", xy=(onset, 0.985),
                        xycoords=("data", "axes fraction"), xytext=(2, -1),
                        textcoords="offset points", ha="left", va="top",
                        fontsize=4.3, color=C_TEAL, rotation=90)
        handles = [
            Line2D([], [], color=C_BLUE, marker="o", markersize=2.6,
                   label="median, IQR (completed)"),
            Line2D([], [], color=C_TEAL, linestyle="--", marker="^",
                   markersize=2.4, label="share with candidate words"),
            Line2D([], [], color=C_GREY, linestyle=":", label="share censored"),
        ]
        handles[0] = Line2D([], [], color=C_BLUE, marker="o", markersize=2.6,
                            label=f"median, IQR (completed, {plot_mode})")
        ax.legend(handles=handles, loc="upper left", frameon=False,
                  handletextpad=0.2, borderpad=0.1, labelspacing=0.3,
                  fontsize=4.6)
        ax.text(0.985, 0.905, f"mode = {plot_mode}",
                transform=ax.transAxes, ha="right", va="top", fontsize=4.2,
                color=C_DARK)
        lagging = sorted({row["benchmark_family"] for row in rows
                          if row["_candidate_positive"]})
        if lagging:
            ax.text(0.025, 0.03,
                    "candidate words in: "
                    + ", ".join(short_family(family) for family in lagging),
                    transform=ax.transAxes, ha="left", va="bottom",
                    fontsize=4.0, color=C_TEAL)
    add_cap_line(ax, cap)
    if aggregate:
        rank_to_L = defaultdict(list)
        for row in rows:
            if row.get("l_rank") not in (None, ""):
                rank_to_L[int(row["l_rank"])].append(row["_L"])
        rank_values = sorted(rank_to_L)
        ax.set_xlim(min(rank_values) - 0.5, max(rank_values) + 0.5)
        show = rank_values if len(rank_values) <= 10 else \
            rank_values[::max(1, len(rank_values) // 9)]
        ax.set_xticks(show)
        ax.set_xticklabels(
            [f"{int(round(median(rank_to_L[rank])))}" for rank in show],
            fontsize=5.6)
        ax.set_xlabel("depth limit $L$ (rank within sweep; ticks = median $L$)")
    else:
        ax.set_xscale("log")
        ax.set_xlim(min(l_values) * 0.82, max(l_values) * 1.2)
        tick_pool = log_ticks(set(l_values))
        ax.set_xticks(tick_pool)
        ax.set_xticklabels([str(value) for value in tick_pool], fontsize=5.6)
        ax.xaxis.set_minor_formatter(NullFormatter())
        ax.set_xlabel("depth limit $L$")
    style_time_axis(ax, rows, cap)
    add_subtitle(ax, rows)
    panel_label(ax, "c")


# ---------------------------------------------------------------------------
# Caption rendering
# ---------------------------------------------------------------------------
def render_caption(template_path, out_path, values, is_formal):
    if not template_path or not Path(template_path).is_file():
        text = (
            "**Figure 6.** Structural scalability of the bounded analyser on the "
            "v6 matrix. (a) cell analysis time versus model size; (b) paired WIP "
            "1/2/3 growth; (c) depth-limit L sweep. Open triangles on the dashed "
            "ceiling line are TIMEOUT cells, right-censored at "
            f"{values['cap']} s and never treated as completion times. Hollow "
            "diamonds are INC (completed but inconclusive) plotted at their real "
            "analysis time. Denominators are printed in each panel.")
    else:
        text = Path(template_path).read_text()
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S).strip() + "\n"
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    if not is_formal:
        banner = (
            "> **PILOT-SELFCHECK — NOT FORMAL DATA.**\n"
            "> This caption and figure were generated from the frozen 288-cell "
            "Pilot self-check to validate the pipeline. The formal 8,280-cell "
            "result must be regenerated with the same command once the Modal "
            "run is downloaded.\n\n")
        text = banner + text
    Path(out_path).write_text(text)
    return text


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--analysis-dir", type=Path,
                        default=ROOT / "work/v6_analysis_pilot_selfcheck")
    parser.add_argument("--out", type=Path, default=None,
                        help="output directory (default: same as analysis dir)")
    parser.add_argument("--formats", default="svg,pdf,eps,png")
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--caption-template", type=Path,
                        default=ROOT / "docs/v6_figure6_caption.md")
    parser.add_argument("--stem", default="figure6")
    parser.add_argument("--width", type=float, default=7.16)
    parser.add_argument("--height", type=float, default=2.92)
    parser.add_argument("--max-lines", type=int, default=160,
                        help="individual B_wip trajectories before switching "
                             "to the aggregate view")
    parser.add_argument("--max-points", type=int, default=900,
                        help="individual L-sweep cells before switching to the "
                             "aggregate view")
    args = parser.parse_args()

    analysis_dir = args.analysis_dir if args.analysis_dir.is_absolute() \
        else ROOT / args.analysis_dir
    out_dir = args.out or analysis_dir
    out_dir = out_dir if out_dir.is_absolute() else ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = [normalize(row) for row in load_csv(analysis_dir / "normalized_tasks.csv")]
    stats = json.loads((analysis_dir / "statistics.json").read_text())
    dataset = stats.get("dataset", {})
    cap = float(dataset.get("cell_timeout_s") or 60.0)
    is_formal = bool(dataset.get("is_formal"))
    dataset_label = dataset.get("label") or ("FORMAL" if is_formal
                                             else "PILOT-SELFCHECK")

    fig, axes = plt.subplots(1, 3, figsize=(args.width, args.height))
    fig.subplots_adjust(left=0.062, right=0.995, bottom=0.235, top=0.945,
                        wspace=0.40)
    series_map, series_keys = panel_a(axes[0], rows, cap, stats)
    panel_b(axes[1], rows, cap, stats, args.max_lines)
    panel_c(axes[2], rows, cap, stats, args.max_points)

    # Shared legends: one row of outcome coding, one row of A-series colours.
    outcome_handles = [
        Line2D([], [], marker=OUTCOME_MARKER[kind], linestyle="none",
               markersize=3.6,
               markerfacecolor=(C_DARK if kind in ("ver", "amb") else "none"),
               markeredgecolor=(C_CAP if kind == "timeout" else C_DARK),
               label=OUTCOME_LABEL[kind])
        for kind in ("ver", "amb", "inc", "timeout")]
    fig.legend(handles=outcome_handles, loc="lower center", ncol=4,
               frameon=False, bbox_to_anchor=(0.5, 0.083),
               handletextpad=0.2, columnspacing=1.0, fontsize=5.2)
    if series_keys:
        series_handles = [
            Line2D([], [], marker="o", linestyle="none", markersize=3.2,
                   markerfacecolor=series_map[key], markeredgecolor=series_map[key],
                   label=f"panel (a) {str(key).replace('_', ' ')} series")
            for key in series_keys]
        fig.legend(handles=series_handles, loc="lower center",
                   ncol=min(3, len(series_keys)), frameon=False,
                   bbox_to_anchor=(0.5, 0.045), handletextpad=0.2,
                   columnspacing=1.2, fontsize=5.2)
    fig.text(0.5, 0.004, dataset_label + (" — NOT FORMAL DATA"
                                          if not is_formal else ""),
             ha="center", va="bottom", fontsize=5.2,
             color=(C_WARN if not is_formal else C_DARK),
             fontweight="bold" if not is_formal else "normal")

    written = []
    for fmt in [item.strip() for item in args.formats.split(",") if item.strip()]:
        path = out_dir / f"{args.stem}.{fmt}"
        kwargs = {"dpi": args.dpi} if fmt == "png" else {}
        fig.savefig(path, format=fmt, **kwargs)
        written.append(path)
    plt.close(fig)

    # Every plotted point, for recomputability.
    data_rows = []
    for panel, experiment in (("a", "A_structural"), ("b", "B_wip"),
                              ("c", "C_Lsweep")):
        for row in rows:
            if row.get("experiment") != experiment:
                continue
            data_rows.append({
                "panel": panel,
                "task_id": row.get("task_id"),
                "base_model_id": row.get("base_model_id"),
                "benchmark_family": row.get("benchmark_family"),
                "stratum": row.get("stratum"),
                "series": row.get("series"),
                "mode": row.get("mode"),
                "L": row.get("_L"),
                "WIP": row.get("_wip"),
                "model_size": row.get("_size"),
                "execution_status": row.get("execution_status"),
                "verdict": row.get("_verdict"),
                "outcome": outcome(row),
                "candidate_words": row.get("_candidate"),
                "candidate_positive": int(row["_candidate_positive"]),
                "runtime_compute_s": row.get("_compute"),
                "wall_s": row.get("_wall"),
                "plotted_y_s": y_value(row, cap),
                "censored_at_s": cap if row["_timeout"] else None,
            })
    data_path = out_dir / "figure6_data.csv"
    with data_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(data_rows[0]))
        writer.writeheader()
        writer.writerows(data_rows)

    denominators = stats.get("denominators", {})
    overall = denominators.get("overall", {})
    by_exp = denominators.get("by_experiment", {})
    l_sensitivity = (stats.get("groups", {}).get("C_Lsweep", {})
                     .get("l_sensitivity", {}))
    paired = (stats.get("groups", {}).get("B_wip", {})
              .get("paired_growth", {}))

    def experiment_clause(name):
        entry = by_exp.get(name, {})
        return (f"{entry.get('n_total', 0)} cells: "
                f"{entry.get('n_completed', 0)} completed, "
                f"{entry.get('n_timeout', 0)} right-censored, "
                f"{entry.get('n_inc', 0)} INC")

    caption_values = {
        "dataset_label": dataset_label,
        "cap": f"{cap:g}",
        "n_tasks": overall.get("n_total", 0),
        "n_completed": overall.get("n_completed", 0),
        "n_timeout": overall.get("n_timeout", 0),
        "n_inc": overall.get("n_inc", 0),
        "n_ver": overall.get("n_ver", 0),
        "n_amb": overall.get("n_amb", 0),
        "protocol": dataset.get("protocol_version", ""),
        "a_clause": experiment_clause("A_structural"),
        "b_clause": experiment_clause("B_wip"),
        "c_clause": experiment_clause("C_Lsweep"),
        "n_l_models": l_sensitivity.get("n_base_models", 0),
        "n_l_candidate_models": l_sensitivity.get(
            "n_models_l_candidate_onset", 0),
        "n_b_models": stats.get("groups", {}).get("B_wip", {})
        .get("n_base_models", 0),
        "wip2_ratio": paired.get("wip2_vs_wip1_compute_ratio", {})
        .get("median_ratio"),
        "wip3_ratio": paired.get("wip3_vs_wip1_compute_ratio", {})
        .get("median_ratio"),
        "wip2_pairs": paired.get("wip2_vs_wip1_compute_ratio", {})
        .get("n_uncensored_pairs", 0),
        "wip3_pairs": paired.get("wip3_vs_wip1_compute_ratio", {})
        .get("n_uncensored_pairs", 0),
        "rho_size_time": (stats.get("groups", {}).get("A_structural", {})
                          .get("spearman_completed_only", {})
                          .get("size_vs_runtime_compute", {}).get("rho")),
    }
    for key in ("wip2_ratio", "wip3_ratio"):
        value = caption_values[key]
        caption_values[key] = "n/a" if value is None else f"{value:,.0f}"
    rho = caption_values["rho_size_time"]
    caption_values["rho_size_time"] = "n/a" if rho is None else f"{rho:.2f}"
    render_caption(args.caption_template, out_dir / f"{args.stem}_caption.md",
                   caption_values, is_formal)

    metadata = {
        "figure": f"Figure 6 ({dataset_label})",
        "is_formal": is_formal,
        "analysis_dir": str(analysis_dir),
        "dataset_protocol": dataset.get("protocol_version"),
        "cell_timeout_s": cap,
        "files": {path.suffix.lstrip("."): {"path": str(path),
                                            "bytes": path.stat().st_size}
                  for path in written},
        "caption": str(out_dir / f"{args.stem}_caption.md"),
        "plot_data": str(data_path),
        "matplotlib_version": MATPLOTLIB_VERSION,
        "python": sys.version.split()[0],
        "rendering": {
            "panel_b_aggregate": len({row.get("base_model_id") for row in rows
                                      if row.get("experiment") == "B_wip"
                                      and row["_wip"] in (1, 2, 3)})
            > args.max_lines,
            "panel_c_aggregate": sum(1 for row in rows
                                     if row.get("experiment") == "C_Lsweep")
            > args.max_points,
        },
        "panels": {
            name: {"experiment": experiment,
                   "x": x_label, "y": "SCG + verify analysis time (s, log)",
                   **{key: by_exp.get(experiment, {}).get(key)
                      for key in ("n_total", "n_completed", "n_timeout",
                                  "n_inc", "n_ver", "n_amb")}}
            for name, experiment, x_label in (
                ("a", "A_structural", "model size"),
                ("b", "B_wip", "WIP bound"),
                ("c", "C_Lsweep", "depth limit L"))
        },
        "censoring": {
            "timeout_marker": "open upward triangle on the dashed cap line",
            "timeout_value": "plotted at the cap, never at the wall clock",
            "inc_marker": "hollow diamond at the real analysis time",
            "inc_is_not_zero": True,
            "aggregate_mode_note": (
                "With large samples panels (b)/(c) show median + IQR of "
                "COMPLETED cells only plus an explicit censored share; a "
                "censored cell is never imputed and never enters the median."),
        },
    }
    (out_dir / f"{args.stem}_metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n")

    print(json.dumps({
        "dataset": dataset_label,
        "files": {path.suffix.lstrip("."): path.stat().st_size for path in written},
        "caption": str(out_dir / f"{args.stem}_caption.md"),
        "plot_data_rows": len(data_rows),
        "aggregate": metadata["rendering"],
        "matplotlib": MATPLOTLIB_VERSION,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
