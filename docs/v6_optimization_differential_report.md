# v6 performance differential and pilot re-measurement

This report collects the independent evidence that the optimisation in commit
`2154432` preserves the timed semantics, and re-measures the frozen 288-cell
Pilot. All comparisons use the same frozen task lists, the same 60 s per-cell
cap and the same runner type (GitHub-hosted `ubuntu-22.04` for the Pilot runs;
one local machine for the differential layers).

> **Corrections added in this revision (2026-11-07, after review).**
> (1) Only 1 of the 18 censored differential cells has a complete
> Synchronizer fingerprint comparison; the other 17 could not be compared
> because the *baseline* synchronizer itself does not finish within 300 s on
> those models. Earlier text that read as if the difficult cells had full
> synchronizer equivalence was wrong and is corrected below.
> (2) The 30 conclusive pairs and the 18 censored pairs are reported
> separately; the censored pairs are further split into 11 one-sided
> (baseline timeout, optimised completed) and 7 both-timeout pairs.
> (3) The wall-clock speed-up statistics of the 48-cell differential and of
> the 288-cell Pilot are different samples and are now reported separately;
> they must not be pooled.
> (4) `differential_pairs.csv` previously printed Layer A status for Layer B
> rows; the comparison tool now records the layer actually used, both side
> statuses and the censoring category per row.
> (5) Of the 76 cells that flipped timeout→completed, 45 are new conclusive
> `VER`/`AMB` results and 31 are `INC` (state-class limit) results.
> (6) The difficult family D obtained **0** conclusive `VER`/`AMB` results in
> the 24-cell Pilot: 19 `INC`, 5 timeouts.

## 1. Frozen performance differential (`v6-performance-differential-1`)

Baseline commit `10d27726c085076fe790d574eabd15d7423ac86d`, optimised commit
`215443219c524878f7af4cef0e4b8d8caf790531`, 48 cells selected structurally
from the frozen Pilot (12 families × `depth_shallow`, `L_mid`,
`L_high_complete`, `WIP_1`). Layer A: 60 s cap. Layer B: every pair censored on
either side re-run at 300 s. Acceptance: exact equality of verdict,
inconclusive reason, COMPLETE_SET certificate set, enumeration completeness,
model hash and every non-runtime workload counter; a censored pair is never a
pass.

| Result | Pairs |
|---|---:|
| Conclusive and exactly identical | **30** (27 in Layer A, 3 in Layer B) |
| Mismatched | **0** |
| Censored after Layer B | 18 |
| — both sides timed out | 7 |
| — baseline timed out, optimised completed | 11 |
| — optimised timed out, baseline completed | 0 |

The 7 both-timeout pairs are six `COMPLETE_SET` `L_high_complete` cells (F06,
F07, F08, F09, F11, F12) and F06 `depth_shallow`. A probe at 900 s also timed
out for both versions: their temporal expansion does not finish at any cap
used here, so **the baseline itself cannot produce a verdict** for these cells.
The 11 one-sided pairs are bounded-improvement evidence, not comparable
verdicts.

**Paired wall-clock speed-up, differential sample (30 pairs):** median
**16.62×**, minimum 0.78× (one small cell within timing noise), maximum 361×.
This is the differential sample only; it is not the Pilot statistic.

## 2. Stage-level equivalence for the censored cells

The full verdict of the censored pairs cannot be produced by the baseline, so
the stages modified by the optimisation were compared exactly, where the
baseline can still finish them:

* **SCG construction** (`work/v6_scg_equivalence_1/scg_equivalence.json`):
  24 models (every family × `depth_shallow` and `L_mid`), exact fingerprint
  over every class marking, enabled set, DBM variable order and exact rational
  DBM entry, the edge list, completion set, initial class and both SCG flags:
  **24/24 identical**, median SCG-build speed-up **2.0×** (mean 1.85×).
  The `L_high_complete` cells reuse the same `L_mid` model (only `L` and mode
  differ), so their SCG is covered by the same fingerprint; `WIP_1` models are
  not all covered.
* **Synchronizer construction** (`work/v6_sync_equivalence_1/sync_equivalence.json`):
  the exact automaton fingerprint (every `(X_N, X_F)` state, every labelled
  edge, the ordered candidate-word list) was attempted for all 18 censored
  cells with both versions. **1 of 18 matched** (F05 `L_high_complete`, 53
  states / 992 candidate words). The other **17 are censored**: the baseline
  synchronizer itself does not finish within the 300 s cap on those models, so
  no full synchronizer comparison exists for them. This is a real limitation
  and is corrected here. The transition-level equivalence work for these
  cells is carried out inside the optimised tree in the Differential v2
  protocol (`v6-performance-differential-2`, Layer B), which re-computes every
  transition without the memo cache and compares it with the memoised result.

## 3. Cross-version agreement on the full 288-cell Pilot

The frozen 288-cell Pilot was executed twice with the same tasks, cap and
runner type: instrumented baseline `92c540f` (run `42348140150`) and optimised
`835e4bd` (run `42348132809`). Both passes were audited independently and both
audits are `PASS` with no findings (no missing/duplicate record, no hash
mismatch, no ERROR, no zero-filled unexecuted stage counters).

| Measure | Baseline | Optimised |
|---|---:|---:|
| Completed | 126 (43.8 %) | **202 (70.1 %)** |
| `TIMEOUT_WALL_CLOCK` | 162 (56.3 %) | **86 (29.9 %)** |
| `INC` | 4 | 35 |
| `VER` / `AMB` | 59 / 63 | 80 / 87 |
| Conclusive (VER+AMB) | 122 (42.4 %) | **167 (58.0 %)** |
| Compute-only runner minutes | 175.7 | **121.1 (−31 %)** |
| Peak RSS P95 / max | 239 / 329 MiB | 1 653 / 2 330 MiB |

Cells completed under both versions: **126/126 identical** verdict,
inconclusive reason and **all** workload counters (`scg_classes`, `scg_edges`,
`sync_states`, `candidate_words`, `temporal_states`, `trace_records`,
`terminal_pairs`, `lp_calls`). **0 verdict mismatches, 0 counter mismatches,
0 completed→timeout regressions; 76 cells flipped timeout→completed.**

Of those 76 flips, **45 produce a new conclusive verdict (24 `AMB` + 21
`VER`)** and **31 produce `INC`** (the cell now reaches the declared
50 000-class SCG limit instead of being killed by the wall clock). An `INC` is
an inconclusive result, not a diagnosis, and is counted as such.

**Paired wall-clock speed-up, Pilot sample (126 pairs completed on both
sides):** median **1.53×**, P90 16.18×, maximum 74.28×, minimum 0.58×. Do not
pool this with the 30-pair differential statistic above.

Per experiment (completed / timeout):

| Experiment | Baseline | Optimised |
|---|---:|---:|
| A structural/observation | 63 / 57 | 77 / 43 |
| B WIP | 21 / 51 | 48 / 24 |
| C L-sweep | 41 / 31 | 58 / 14 |
| D difficult | 1 / 23 | **19 / 5** |

**Difficult family D (24 Pilot cells): the optimised run completes 19 cells and
times out on 5, but all 19 completions are `INC` (SCG state limit). D obtains
0 conclusive `VER`/`AMB` results in either version.** D is retained unchanged
in the formal protocol; its boundary is reported, not hidden.

## 4. The frozen 96-cell difficulty sample (Target A)

`v6-cache-validation-1` was re-executed unchanged on the optimised code
(run `42347953385`, per-shard artifacts downloaded and audited):

| Measure | Frozen baseline `42339602408` | Optimised `42347953385` |
|---|---:|---:|
| Completed | 38 (39.6 %) | **71 (74.0 %)** |
| `TIMEOUT_WALL_CLOCK` | 58 (**60.4 %**) | **25 (26.0 %)** |
| `VER` / `AMB` / `INC` | 25 / 9 / 4 | 40 / 13 / 18 |

Target A of the task ("96-cell sample timeout fraction from 60.4 % to below
35 %") is met with a wide margin (26.0 %), on exactly the same frozen cells.

## 5. Budget

Censoring-aware expectation for the formal 8,280-cell matrix, using the
optimised Pilot's per-bucket timeout fractions and completed-cell wall P50s
(right-censored cells cost the full 60 s, never a fabricated completion time):

| | Baseline | Optimised |
|---|---:|---:|
| Expected compute | 77.13 h | **43.26 h (2 596 runner-minutes)** |
| Worst case (all cells 60 s) | 138 h | 138 h |
| Expected wall at 20 concurrent | 3.9 h | **2.16 h** |
| Worst-case wall (120 shards / 20) | 6.9 h | 6.9 h |

These are planning figures from GitHub-hosted runners. They are **not** Modal
billing figures; the Modal environment and budget are audited separately in
`docs/v6_modal_environment.md`.

## 6. What the frozen differential gate says

Under the pre-registered acceptance rule, the frozen differential is **not
passed**: 18 of 48 pairs could not be completed on both sides. The gate is
reported as `DIFFERENTIAL_VALIDATION_PASS = false` and is not reinterpreted.
The supporting evidence in §1–§4 is reported alongside it so the limitation is
auditable rather than hidden. Differential v2
(`protocol/v6-performance-differential-2.yaml`) closes the gap with
transition-level and component-level equivalence and is reported separately.
