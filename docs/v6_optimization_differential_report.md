# v6 performance differential and pilot re-measurement

This report collects the independent evidence that the optimisation in commit
`2154432` preserves the timed semantics, and re-measures the frozen 288-cell
Pilot. All comparisons use the same frozen task lists, the same 60 s per-cell
cap and the same runner type (GitHub-hosted `ubuntu-22.04` for the Pilot runs,
one local machine for the differential layers).

## 1. Frozen performance differential (`v6-performance-differential-1`)

Baseline commit `10d2772`, optimised commit `2154432`, 48 cells selected
structurally from the frozen Pilot (12 families × `depth_shallow`, `L_mid`,
`L_high_complete`, `WIP_1`). Layer A: 60 s cap. Layer B: every pair censored on
either side re-run at 300 s. Acceptance: exact equality of verdict,
inconclusive reason, COMPLETE_SET certificate set, enumeration completeness,
model hash and every non-runtime workload counter; a censored pair is never a
pass.

| Result | Pairs |
|---|---:|
| Conclusive and exactly identical | **30** |
| Mismatched | **0** |
| Censored after Layer B | 18 |

Paired wall-clock speed-up where both sides completed: median **1.53×**,
P90 16.2×, maximum 74.3×, minimum 0.58× (one small cell within timing noise).

The 18 censored pairs fall into two groups:

* **7 pairs where both versions hit the 300 s cap** — six `COMPLETE_SET`
  `L_high_complete` cells (F06, F07, F08, F09, F11, F12) and F06
  `depth_shallow`. A probe at 900 s also timed out for both versions: their
  temporal expansion (12 436 candidate words for F06 `depth_shallow`) does not
  finish at any cap used here, so **the baseline itself cannot produce a
  verdict** for them.
* **11 pairs where the baseline hits the 300 s cap while the optimised version
  completes** (F06 `L_mid`, F07/F08/F09/F11/F12 cells). These are bounded
  improvement evidence, not comparable verdicts.

## 2. Stage-level equivalence for the censored cells

Because the full verdict of those 18 pairs cannot be produced by the baseline,
the two stages that the optimisation actually modifies were compared exactly:

* **SCG construction** (`work/v6_scg_equivalence_1/scg_equivalence.json`):
  24 models (every family × `depth_shallow` and `L_mid`), exact fingerprint over
  every class marking, enabled set, DBM variable order and exact rational DBM
  entry, the edge list, completion set, initial class and both SCG flags:
  **24/24 identical**, median SCG-build speed-up **2.0×** (mean 1.85×).
* **Synchronizer construction** (`work/v6_sync_equivalence_1/sync_equivalence.json`):
  the exact automaton fingerprint (every `(X_N, X_F)` state, every labelled
  edge, the ordered candidate-word list) for every censored cell, both versions.
  The synchronizer is the stage whose transition memoisation was added.

Together these show that for the censored cells both modified upstream stages
are bit-identical; the only unverified stage is the temporal expansion, which
the baseline cannot complete either.

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

Per experiment (completed / timeout):

| Experiment | Baseline | Optimised |
|---|---:|---:|
| A structural/observation | 63 / 57 | 77 / 43 |
| B WIP | 21 / 51 | 48 / 24 |
| C L-sweep | 41 / 31 | 58 / 14 |
| D difficult | 1 / 23 | **19 / 5** |

The higher `INC` count is expected and is not a regression: cells that used to
be killed before the SCG finished now reach the declared 50 000-class SCG limit
and report `INC` honestly (an inconclusive result, not a timeout).

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

The optimised matrix is feasible on standard GitHub-hosted runners under the
pre-declared envelope (expected compute ≤ 3 000 min, expected wall ≤ 6 h,
worst-case wall ≤ 12 h, per-job worst case 69 min ≤ 150 min).

## 6. What the frozen differential gate says

Under the pre-registered acceptance rule, the frozen differential is **not
passed**: 18 of 48 pairs could not be completed on both sides. The gate is
reported as `DIFFERENTIAL_VALIDATION_PASS = false` and is not reinterpreted.
The supporting evidence in §1–§4 is reported alongside it so the limitation is
auditable rather than hidden.
