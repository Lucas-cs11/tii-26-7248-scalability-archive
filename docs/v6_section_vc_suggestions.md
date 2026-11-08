# Minimal proposed revision for Section V-C (v6)

Status marker: **FORMAL**. This is a replacement suggestion only; it is not
applied to the v17 manuscript or to any approved blue paragraph.

> **Scalability (v6 execution).** Figure 6 reports the frozen v6 matrix
> (`scalability-v6-frozen-protocol-1`, 8280 cells,
> 60.0 s per-cell wall-clock cap, 50,000
> state-class cap) executed entirely on Modal CPU workers; the independent
> audit reported 8280 audited task records (8280 unique task IDs,
> 120 shards) with 0 missing and
> 0 duplicate records. Points marked at the cap are
> right-censored wall-clock timeouts and are not treated as zero-runtime
> observations or as verdicts. Completed-but-inconclusive cells are reported
> separately from timeouts.

| experiment | total | completed | timeout | INC | VER / AMB |
|---|---:|---:|---:|---:|---:|
| A_structural | 600 | 600 | 0 | 50 | 343 / 207 |
| B_wip | 1080 | 628 | 452 | 30 | 164 / 434 |
| C_Lsweep | 5400 | 3249 | 2151 | 0 | 1195 / 2054 |
| D_difficult | 1200 | 929 | 271 | 569 | 273 / 87 |

Reproduce these numbers from `07_statistical_analysis/statistics.json`; the
denominators are explicit and time quantiles use COMPLETED cells only.

Suggested caption:

> **Fig. 6.** Bounded-verification cost of the frozen v6 matrix. Panels report
> right-censored runtime (triangles at the cap), conclusive/INC/timeout
> fractions with explicit denominators, and the L-sensitive versus plateau
> structure of the observable-word sweep. Timeouts are censored and never
> imputed as completions.
