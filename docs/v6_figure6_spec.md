# Figure 6 — specification, statistics discipline and reproducibility

**Status.** This document specifies the Figure 6 pipeline for the **formal v6
8,280-cell matrix**. The pipeline has been exercised end-to-end on the real
frozen **288-cell Pilot** (`PILOT-SELFCHECK` outputs under
`work/v6_analysis_pilot_selfcheck/`). The formal figure does not exist yet: it
is produced by the same two commands once the Modal run is downloaded. No
formal-data number is written anywhere by this pipeline.

Related artefacts:

| artefact | purpose |
|---|---|
| `scripts/analyze_v6_formal_results.py` | normalize + cross-check + A/B/C/D statistics (CSV/JSON) |
| `scripts/plot_figure6_v6.py` | three-panel Figure 6, four formats, caption, plot data |
| `docs/v6_figure6_caption.md` | single-source caption template (Pilot/Formal filled by the script) |
| `work/v6_analysis_pilot_selfcheck/` | Pilot self-check output (real Pilot data, never labelled formal) |

---

## 1. One-command reproduction

Pilot self-check (defaults are the Pilot paths):

```bash
cd /tmp/v6work
.venv/bin/python scripts/analyze_v6_formal_results.py \
    --reference-audit work/v6_optimized_pilot_audit/pilot_audit.json
.venv/bin/python scripts/plot_figure6_v6.py
```

Formal run (after downloading the Modal raw artifacts):

```bash
cd /tmp/v6work
.venv/bin/python scripts/analyze_v6_formal_results.py \
    --tasks-dir  protocol/v6_formal_tasks \
    --manifest   protocol/v6_formal_manifest.json \
    --raw-dir    work/v6_formal_run_<run-id> \
    --out        work/v6_analysis_formal
.venv/bin/python scripts/plot_figure6_v6.py \
    --analysis-dir work/v6_analysis_formal \
    --out          work/v6_analysis_formal
```

The analysis script refuses to emit statistics when the raw records are
incomplete, duplicated, or disagree with the frozen task table (frozen-field
mismatch, timeout with a verdict, completed cell without a valid verdict);
`--allow-incomplete` is an explicit escape hatch for diagnosis only. The script
also re-applies the "unexecuted stage ⇒ null counter" rule defensively and
records how many raw zeroes had to be repaired (`integrity.counter_gating_repairs`).

---

## 2. Data sources and the normalized table

Inputs:

* frozen task table — `protocol/v6_pilot_tasks/shard_*.csv` (Pilot) or
  `protocol/v6_formal_tasks/shard_*.csv` (Formal), one row per configuration;
* frozen manifest — `protocol/v6_pilot_manifest.json` /
  `protocol/v6_formal_manifest.json` (`tasks`, `shards`, `cell_timeout_seconds`);
* raw cell records — `<raw-dir>/*/raw/shard_*.jsonl`, one JSON object per cell.

Every cell is normalized into exactly one row of `normalized_tasks.csv`, joined
on `task_id`, with the frozen task fields verified against the record. Key
columns:

| group | columns |
|---|---|
| identity / design | `task_id`, `experiment`, `benchmark_family`, `stratum`, `series`, `design_level`, `base_model_id`, `model_id`, `seed`, `replicate`, `protocol_version`, `WIP`, `L`, `H`, `mode`, `timeout_s` |
| outcome / denominators | `execution_status`, `verdict`, `analysis_status`, `inconclusive_reason`, `failure_stage`, `timeout_stage`, `reached_stage`, `reached_stage_rank`, `is_completed`, `is_timeout`, `is_error` |
| model structure | `n_places`, `n_transitions`, `n_arcs`, `size_places_transitions`, `number_of_parts`, `operations_per_part`, `resource_count`, `inspection_locations`, `route_choice_locations`, `rework_locations`, `observable_ratio`, `cycle_presence`, `verified_reachable_concurrency` |
| stage work | `scg_classes`, `scg_edges`, `scg_limit`, `scg_limit_hit`, `sync_states`, `sync_edges`, `candidate_words`, `candidate_words_processed`, `temporal_states`, `trace_records`, `terminal_pairs`, `lp_calls`, `lp_feasible`, `lp_infeasible`, `lp_unknown`, `confirmed_critical_pairs` |
| stage time | `runtime_scg_s` (build, or cache load on a hit), `runtime_scg_load_s`, `runtime_sync_s`, `runtime_unobservable_closure_s`, `runtime_expansion_s`, `runtime_certificate_s`, `runtime_lp_s`, `runtime_lp_build_s`, `runtime_lp_solve_s`, `runtime_total_s` (verify total, SCG build excluded), `runtime_compute_s` (SCG + verify, COMPLETED only) |
| cell level | `wall_s` (end to end, observed), `wall_censored_at_s` (cap for TIMEOUT), `cap_utilization`, `peak_rss_mb`, `scg_cache_status`, `cache_hit`, `partial_workload`, `entered_depth_L_stage` |
| L-sweep flags | `l_rank`, `n_l_levels`, `l_sensitive_candidate`, `l_sensitive_state`, `l_sensitive_verdict` |

`stratum` is taken from the Pilot task table when present and otherwise derived
from the frozen `base_model_id`/`params` (the formal task table has no stratum
column): `A1_depth:l00:…`, `B:<family>:s…` + WIP, `C:<family>:s…` + mode,
`D:<family>:l03:s…`.

---

## 3. Denominators

All denominators are derived, never asserted. For every group (overall,
experiment, stratum, WIP, size bin, difficult family/level, L rank) the pipeline
emits `n_total`, `n_completed`, `n_timeout`, `n_error`, `n_inc`, `n_ver`,
`n_amb`, `n_conclusive`, plus the fractions:

* `completion_fraction = n_completed / n_total`
* `timeout_fraction = n_timeout / n_total` (right-censored share)
* `inc_fraction_completed = n_inc / n_completed`
* `conclusive_fraction_completed = (n_ver + n_amb) / n_completed`
* `conclusive_fraction_all = (n_ver + n_amb) / n_total`

The distinction matters: a high `conclusive_fraction_completed` with a large
`timeout_fraction` means "conclusive *among the cells that finished*", not a
high overall decision rate. Both are reported for this reason. INC is a
**completed** cell (`n_inc ⊆ n_completed`), but it is never a conclusive one.

---

## 4. Right censoring and INC — the binding rules

1. **A TIMEOUT is right-censored at the cell cap** (`cell_timeout_seconds`, 60 s
   in Pilot and formal). It is recorded with `execution_status =
   TIMEOUT_WALL_CLOCK`, `verdict = null`, the stopping `failure_stage`
   (`MODEL_VALIDATED`, `SCG_COMPLETE`, `SYNCHRONIZER_COMPLETE`,
   `HISTORY_ANALYSIS_PARTIAL`), and `wall_s ≈ cap`. It is **never** assigned the
   cap as a completion time, and never enters a P50/P90/P95 computation.
2. **All time quantiles use COMPLETED cells only.** Percentiles are
   nearest-rank (`ceil(p·n) − 1`) over `wall_s`, `runtime_total_s` and
   `runtime_compute_s` of completed cells; `n_observed_wall` /
   `n_observed_runtime` give the actual denominator. The method is repeated in
   `statistics.json → quantile_method`.
3. **A timeout contributes only a lower bound** to cost statements (e.g.
   `censored_wall_lower_bound_sum_s`). It also contributes a stopping-stage
   count (`failure_stages.csv`).
4. **Counters of unexecuted stages are null, not zero.** A TIMEOUT during SCG
   construction has null `sync_states`, `candidate_words`, `trace_records`,
   `terminal_pairs`, `lp_calls`; a TIMEOUT after the synchronizer has valid
   state/candidate counts but null history/LP counters. Each metric median is
   accompanied by its observed `_n` so a null is visible as a smaller sample.
5. **INC is "completed but inconclusive"** (e.g. `SCG_STATE_LIMIT`,
   `COMPLETION_UNREACHABLE`, `ZERO_TIME_UNOBSERVABLE_CYCLE`). It is never zero
   work, never a VER/AMB, and always reported as its own count. In the figure it
   is a hollow diamond at its *measured* analysis time.
6. **Paired growth respects censoring.** For WIP and L pairs a ratio is only
   computed when both cells COMPLETED. If the numerator is censored the ratio is
   reported as a lower bound (`cap / reference`) in a separate column and a
   separate aggregate (`median_censored_lower_bound`, `n_censored_pairs`); it is
   never mixed into the measured-ratio median.
7. **No accumulation across environments or versions.** Runtime is reported
   per cell; the only sums are `cost_accounting` entries explicitly labelled as
   budget accounting. No speed-up is computed between the Pilot and the formal
   run, or between runner types.
8. **Cache attribution.** On an SCG cache hit the build cost was paid by an
   earlier cell; the hit cell is attributed the measured cache-load time
   (`runtime_scg_load_s`) instead of a fabricated zero build. Hits are flagged
   (`cache_hit`, `scg_cache_status`) and counted in
   `statistics.json → cache_accounting`. The Pilot optimised run has 3 hits out
   of 288.

---

## 5. Timing model

* `runtime_scg_s` = SCG construction (or cache load on a hit).
* `runtime_total_s` = verifier wall time from the start of `verify()` to its
  return; this includes the synchronizer, history expansion, certificate and LP
  work but **excludes** the SCG build that happened before `verify()`.
* `runtime_compute_s = runtime_scg_s + runtime_total_s` for COMPLETED cells;
  this is the y-axis of the figure. It keeps the dominant SCG cost in the
  measurement instead of hiding it.
* `runtime_compute_s` is null for TIMEOUT cells (censored) — those appear only
  as ceiling markers (below the aggregate thresholds) or in the censored-share
  line (above them).
* Stage timers are additive along the pipeline only. `runtime_lp_build_s` and
  `runtime_lp_solve_s` are sub-timers of `runtime_lp_s`, and
  `runtime_unobservable_closure_s` is a sub-timer of `runtime_sync_s`; they are
  reported but never added on top of their parent.
* `wall_s` is the end-to-end observed cell wall clock and includes a small
  process-overhead floor (~0.3–0.4 s on the Pilot runner); that is why the
  paired ratios are reported for both `compute` (primary) and `wall` (secondary).

---

## 6. Panel definitions

### 6(a) Structural scalability

* **Data**: `experiment == A_structural` (`normalized_tasks.csv`).
* **x**: `size_places_transitions` = places + transitions (linear).
* **y**: `runtime_compute_s` (log), SCG + verify analysis time.
* **Colour**: `series` (Pilot: depth / observation-density; formal: A1 depth,
  A2 parts, A3 resources). Colour is a *design* encoding, not an outcome.
* **Markers**: shape = outcome — filled circle VER, filled square AMB, hollow
  diamond INC, open triangle on the dashed cap line = TIMEOUT. Colour and marker
  are independent so no information is double-coded.
* **Denominators**: the panel subtitle gives `n`, completed, censored and INC.
* **Annotation**: Spearman ρ between size and compute time, computed over
  COMPLETED cells only (`groups.A_structural.spearman_completed_only`). The
  right-censored slow tail is missing, so ρ is a lower bound on the association
  and is not a predictive fit.
* **Censoring**: 43/120 Pilot cells are TIMEOUT and sit on the cap line; they
  are never interpolated into the size–time relationship.

### 6(b) WIP sensitivity

* **Data**: `experiment == B_wip`; each base model has exactly the
  configurations WIP = 1/2/3 (same family/seed/structure, only the WIP bound
  changes).
* **x**: WIP ∈ {1, 2, 3} (categorical).
* **y**: `runtime_compute_s` (log).
* **Individual mode** (≤ `--max-lines` base models, default 160): thin grey
  segments connect only COMPLETED cells of one base model; the black line is the
  median of COMPLETED cells per WIP with the completed count printed next to it.
* **Aggregate mode** (formal scale, 360 triplets): median + IQR of completed
  cells per WIP, with the per-WIP completed/censored counts printed.
* **Paired growth**: printed in the panel from
  `groups.B_wip.paired_growth` — median WIP 2/WIP 1 and WIP 3/WIP 1 compute
  ratios, with the number of uncensored pairs. Censored pairs are reported
  separately as lower bounds. The WIP = 2 and WIP = 3 levels are always present.
* **Denominators / censoring**: subtitle + the printed per-WIP counts. A WIP-2/3
  timeout is a ceiling marker (or a censored count in aggregate mode) and never a
  measured ratio.

### 6(c) L sensitivity

* **Data**: `experiment == C_Lsweep`; within a (base model, mode) sweep the
  model and H are fixed and only the depth limit L increases.
* **Individual mode** (≤ `--max-points` cells, default 900; Pilot): x = L (log),
  y = `runtime_compute_s` (log). Thin segments join completed cells of the same
  model; a dashed segment marks a change of `mode` inside a sweep.
* **L-sensitive work marking**: the depth-L synchronizer is the only stage that
  depends on L. Filled markers = the cell's synchronizer produced candidate
  words (`candidate_words > 0`), i.e. it actually reached L-sensitive history
  work; open markers = no candidate word. Censored cells that had already found
  candidate words keep a teal triangle edge. The families that triggered
  L-sensitive work are listed inside the panel (Pilot: F01, F03, F04, F05, F07,
  F08, F10, F11).
* **Aggregate mode** (formal scale): x = L *rank inside each sweep* with tick
  labels = median absolute L; left axis = median + IQR of completed cells, right
  axis = share of cells with candidate words (dashed, teal) and share censored
  at the cap (dotted, grey); the first L rank with candidate words is marked.
  Only the dominant mode of the sweep is aggregated (the formal C1
  first-certificate sweep); C2 COMPLETE_SET results stay in
  `C_lsweep_pairs.csv` / `C_lsweep_by_absolute_L`.
* **L-sensitivity statistics** (`groups.C_Lsweep.l_sensitivity`, from
  `C_lsweep_pairs.csv` / `C_lsweep_models.csv`): a sweep/model is L-sensitive
  when synchronizer states grow with L, candidate words appear only at larger L,
  or the verdict changes with L. `sync_states` and `candidate_words` are
  mode-independent; a verdict change is not when a sweep mixes modes, so
  `n_models_verdict_change_confounded_by_mode` is reported separately. A
  high-L TIMEOUT is censored and therefore cannot count as a verdict change.
* **Denominators / censoring**: subtitle; timeouts on the cap line (individual
  mode) or in the censored-share line (aggregate mode).

---

## 7. Supporting statistical tables

All tables are written by `analyze_v6_formal_results.py` next to
`normalized_tasks.csv`; every group row carries the full denominator block and
the `censoring_note` string.

| file | content |
|---|---|
| `denominators.csv` | n_total/completed/timeout/INC/ERROR per overall/experiment/stratum/WIP/D-stratum group |
| `A_structural.csv` | A cells by stratum and by size bin: median size, SCG classes/edges, sync states, completed-only runtime P50/P90/P95, conclusive/INC/TIMEOUT fractions |
| `B_wip.csv` | B cells per WIP with complete censoring-aware summary |
| `B_wip_pairs.csv` | one row per base model: per-WIP status/verdict/wall/compute/SCG/sync + measured and censored paired ratios |
| `C_lsweep.csv` | L aggregates by rank (Pilot: 3 ranks) and by absolute L, incl. candidate-word-positive and sync-state observed counts |
| `C_lsweep_pairs.csv` | one row per (base model, mode) sweep: L range/values, per-sweep denominators, state growth, candidate onset L, verdict change, sensitivity flags, mode-confound note |
| `C_lsweep_models.csv` | model-level sweep across all modes, with `verdicts_by_L` for audit |
| `C_lsweep_levels.csv` | one row per (base model, mode, L) level, the raw material of panel (c) |
| `D_difficult.csv` | D overall / by family / by level / by stratum: SCG-cap hit count and fraction, runtime, timeout, conclusive rate, failure stages |
| `failure_stages.csv` | timeout and INC stopping-stage distribution per scope |
| `statistics.json` | metadata, integrity, denominators, all four experiment groups, cache and cost accounting, caveats |
| `analysis_summary.md` | human-readable digest |

---

## 8. Colour, marker and typography conventions

* Panel (a) series colours: `#04182E` (A1 depth / depth series),
  `#1D5E9E` (A2 parts), `#B5651D` (A3 resources / observation series);
  the formal A series count decides which palette slots are used.
* Panel (c) teal `#2F6B6E` = candidate-word (L-sensitive) encoding, grey
  `#6E6E6E` = no candidate words / censored share, blue `#1D5E9E` = aggregate
  median, pale fill `#DCE8F2` = IQR.
* Outcome encoding is shared by all panels: VER circle, AMB square, INC hollow
  diamond, TIMEOUT open triangle on the dashed cap line. The shared legend at the
  figure bottom states this once.
* Typography: STIXGeneral / STIX math, 7.2 pt base, 6.6 pt ticks, figure width
  7.16 in (IEEE TII double column), 300 dpi PNG, vector PDF/SVG/EPS with fonts
  embedded as Type 42. White background, no alpha transparency (EPS-safe).

---

## 9. Reviewer-reproducible checklist

1. **Integrity first.** `statistics.json.integrity.pass` must be `true`, with
   `missing_tasks = 0`, `duplicate_records = 0`, `frozen_field_mismatches = 0`,
   `status_violations = 0`; the run ID set and commit SHA set must each contain a
   single value (no mixed environments/versions).
2. **Denominators vs raw.** Count `execution_status` and `verdict` directly in
   the raw JSONL; compare with `denominators.overall` and per experiment. For the
   Pilot this gives 288 / 202 / 86 / 80 / 87 / 35 and matches the independent
   audit (`work/v6_optimized_pilot_audit/pilot_audit.json`; the analysis script
   can assert this with `--reference-audit`).
3. **No timeout imputation.** In `normalized_tasks.csv`, every row with
   `execution_status = TIMEOUT_WALL_CLOCK` has `runtime_compute_s` empty and
   `wall_censored_at_s = cap`; no P50/P90/P95 column counts it.
4. **Quantiles.** Recompute P50/P90/P95 on `wall_s` of COMPLETED rows with
   nearest-rank; they must equal `*_p50/p90/p95_completed_s` in the group CSV.
5. **INC.** Every INC row is COMPLETED, appears in `n_inc`, and is excluded from
   `n_ver + n_amb`; its runtime is present and non-zero.
6. **B ratios.** Recompute `B_wip_pairs.csv` ratios from the per-WIP columns;
   confirm `B_wip.csv` median ratios only use rows with
   `ratio_compute_k_1_censored = 0`, and that censored lower bounds are reported
   separately.
7. **L sensitivity.** Recompute `l_sensitive_state/candidate/verdict` in
   `C_lsweep_pairs.csv` from `C_lsweep_levels.csv`; confirm the reported
   candidate-onset L and the mode-confound flag.
8. **Figure.** `figure6_data.csv` contains every plotted point with its
   `plotted_y_s`; compare against `normalized_tasks.csv`. Non-empty, readable,
   four formats present, and for a Pilot run the file name/annotation carries
   `PILOT-SELFCHECK`.
9. **Caption.** Recompute each number in `figure6_caption.md` from
   `statistics.json`; the formal caption must not contain `PILOT-SELFCHECK`.

---

## 10. Pilot self-check result (real Pilot data, not formal)

Input: `work/v6_optimized_pilot_run_42348132809` + `protocol/v6_pilot_tasks`
(24 shards, 288 records, run `42348132809`, commit `835e4bd`).

| scope | total | completed | timeout | INC | VER | AMB |
|---|---:|---:|---:|---:|---:|---:|
| all | 288 | 202 | 86 | 35 | 80 | 87 |
| A_structural | 120 | 77 | 43 | 2 | 23 | 52 |
| B_wip | 72 | 48 | 24 | 14 | 5 | 29 |
| C_Lsweep | 72 | 58 | 14 | 0 | 52 | 6 |
| D_difficult | 24 | 19 | 5 | 19 | 0 | 0 |

These match the independent Pilot audit exactly (execution-status and verdict
counts, `--reference-audit` cross-check true).

Selected Pilot findings, with their limitations:

* **A**: Spearman ρ(size, compute time) = 0.67 on 77 completed cells;
  ρ(SCG classes, compute) = 0.93 on 77; ρ(SCG classes, sync states) = 0.97 on
  75. 43/120 cells are censored at 60 s, so the relationship is a lower bound at
  the large-size end.
* **B**: WIP=1 all 24 completed (wall P50 0.60 s); WIP=2 16/24 censored; WIP=3
  8/24 censored and 14 INC (SCG state limit). Only 8 WIP-2 and 16 WIP-3 pairs
  have both cells completed, so the measured medians (compute ratios ×255 and
  ×136) rest on those pairs; the other 16 WIP-2 and 8 WIP-3 pairs only provide
  lower bounds (median lower bound ×226 and ×609). This is the dominant Pilot
  limitation: threshold effects are measured on a censored minority.
* **C**: 24 model/H sweeps × 3 L. 66/72 cells entered the depth-L synchronizer
  stage; 14/24 sweeps produced candidate words, all only at the largest L
  (14 of 24 cells). Verdict changes VER→AMB were observed for 6 sweeps whose
  largest-L cell completed; the other 8 candidate-onset sweeps were censored at
  the largest L. The clean same-mode contrast (L_low → L_mid, both
  `mode=first`) shows synchronizer-state growth in 24/24 sweeps but **no**
  candidate onset, so in the Pilot L-sensitive *verdicts* appear only at the
  largest L. **This Pilot sweep conflates the largest L with `mode=complete`**
  (`sync_states`/`candidate_words` are mode-independent; verdicts are not). The
  formal C design runs two fixed-mode 15-level sweeps (C1 first, C2 complete),
  which removes this confound; the analysis keeps both the mode-split sweep view
  (`C_lsweep_pairs.csv`) and the model-level view (`C_lsweep_models.csv`).
* **D**: 19/24 completed INC at the 50,000-class SCG cap (79.2 %), 5 TIMEOUT
  (4 during SCG construction, 1 before SCG), 0 conclusive. Panel (a–c) does not
  plot D; its statistics are table-only (`D_difficult.csv`).
* Cache: 3/288 SCG cache hits, all in C_Lsweep; handled by the load-time
  attribution rule above.

Other Pilot limitations carried into the spec:

* one 60 s cap and one runner type; no cross-environment speed-up is computed;
* `wall_s` carries a ~0.3–0.4 s process-overhead floor, so paired ratios are
  reported for compute time (primary) as well as wall time;
* `peak_rss_mb` is the OS `ru_maxrss` measure of the cell process; it is not a
  model-intrinsic memory bound and is not comparable across runners;
* the Pilot has only 24 WIP triplets and 24 L sweeps, so its B/C panels are
  illustrative of the pipeline, not final evidence.

---

## 11. Environment

| item | version / note |
|---|---|
| Python | 3.13.5 (`/tmp/v6work/.venv/bin/python`) |
| numpy / scipy | 2.5.4 / 1.18.1 |
| matplotlib | **3.11.2** — installed into the venv from the Aliyun PyPI mirror (`--index-url https://mirrors.aliyun.com/pypi/simple/`), because direct PyPI access was blocked in this environment; version is recorded in `figure6_metadata.json` |
| fonts | STIXGeneral (bundled with matplotlib); `pdf.fonttype/ps.fonttype = 42`, `svg.fonttype = none` |
| renderer | `Agg` (headless) |

---

## 12. 中文要点（供团队核对）

* 删失纪律：TIMEOUT 一律按 60 s 上限右删失处理，绝不当作完成时间；P50/P90/P95
  只在 COMPLETED 上算，并在每张表/图给出 total / completed / timeout / INC。
* INC 单独计数：它是“完成但不确定”，既不是零工作量，也不并入 VER/AMB；图上用空心
  菱形画在它真实的耗时位置。
* 面板：6(a) 模型规模 vs SCG+verify 时间（颜色=结构系列）；6(b) 同 base model 的
  WIP 1/2/3 配对增长（必须含 WIP=2/3，配对比值区分已测与删失下界）；6(c) 固定模型/H
  下 L 递增，填充标记=真正进入 L-sensitive（找到 candidate words）的模型。
* 正式 8,280 数据未生成；本流水线只在 Pilot 上以 `PILOT-SELFCHECK` 标识自测，正式
  数据到位后两条命令重跑即可，图与 caption 由脚本自动更新。
