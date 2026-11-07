# v6 Modal Pilot acceptance report (environment portability + checkpoint recovery)

Status: **`MODAL_ENVIRONMENT_PASS = true`** and **`MODAL_CHECKPOINT_RECOVERY_PASS = true`**.
No formal 8,280-cell matrix was started. Total Modal spend for this acceptance round was
**$0.100005** (10.0 cents), measured as the workspace monthly metered-cost difference from
`work/v6_modal_budget/budget.json` (`baseline_metered_cost_usd = 23.58024702`,
`current = 23.68025246`), i.e. ~1.0 % of the authorized $10 cap.

This report covers:

1. the 2-shard pilot data produced by the previous code state (`scalability_v6_pilot3`),
   re-downloaded and audited after the orchestrator fix;
2. the complete frozen 6-shard / 64-cell Pilot on new prefix `scalability_v6_pilot4`;
3. the real crash/checkpoint-recovery test on new prefix `scalability_v6_recovery2`.

## 1. What was fixed in this round

`scripts/run_v6_modal.py` called `vol.reload()` from the **local** orchestrator after a
shard finished and in `status`/`download`. In Modal SDK 1.6.3 `Volume.reload()` (like
`Volume.commit()`) is a container-only operation; the local call raised
`reload() can only be called from within a running function` and aborted the orchestrator
during cleanup — the shard data itself was already complete on the Volume.

Fix (local orchestrator only):

* all six `vol.reload()` calls were removed (`_download_volume`, pilot/run tail,
  resume tail, `status`, `download`);
* the download/status paths now read the latest committed server state directly through
  `Volume.listdir()` / `Volume.read_file()`, which are valid outside a Modal App;
* the only `Volume.commit()` left is the deliberate per-task commit **inside**
  `modal_app/v6_modal_runner.py::run_shard` (container side), which is what makes the
  checkpoint granularity one task;
* no other container-only SDK call was found by the repo-wide search
  (`grep .reload()/.commit()/volume get` over `scripts/`, `modal_app/`, `runner/`, `src/`).

The local self-test passes with the correct interpreter (the Modal client venv also
provides numpy/scipy for `runner/run_cell.py`):

```bash
python scripts/run_v6_modal.py simulate --workdir /tmp/v6_sim_modalpilot
# -> {"PASS": true, "cells": 12, "shards": 2,
#     "classification": {"COMPLETED": 7, "TIMEOUT_WALL_CLOCK": 5},
#     "audit_clean": {"PASS": true}, "tamper_cases": [4/4 detected]}
```

New free report commands were added to the orchestrator so the acceptance can be
regenerated from a download without touching Modal:
`scripts/run_v6_modal.py report --download-dir ...` writes
`reference_comparison.json` + `pilot_summary.json`, and
`scripts/run_v6_modal.py recovery-report --download-dir ... --discovery-dir ...` writes
`recovery_test.json`.

## 2. Environment and execution image

| Item | Value |
|---|---|
| Image spec SHA-256 (primary environment identity) | `f64f51657a4ec43dd7946ebb931fefca7e78524017d7dd34e978c28da183fee7` |
| Modal image object id (secondary) | `im-O631HG8VDiyq7gCt22I3bq` |
| Base image | `modal.Image.debian_slim(python_version='3.13')` |
| Container Python | CPython `3.13.18` (recorded per shard in `metrics/*.json.environment`) |
| numpy / scipy | `2.3.4` / `1.16.2` (exact pins in `requirements-modal.lock`) |
| Modal client (container + orchestration) | `1.6.3` |
| Kernel / sandbox | `Linux-4.19.0-gvisor-x86_64-with-glibc2.41`, machine `x86_64` |
| CPU / memory reservation | `cpu=1.0` physical core, `memory_mib=4096` |
| Cell wall-clock cap | `60.0 s` (frozen per-task `timeout_s`, enforced by the worker) |
| Modal Function timeout | `5400 s` (pilot/resume); `120 s` was used **deliberately** for the crash test |
| BLAS thread pinning | `OPENBLAS/OMP/MKL/NUMEXPR/VECLIB = 1`; `PYTHONHASHSEED=0` |
| Containers | `single_use_containers=True`, one container per shard attempt |

The image spec SHA is identical for all three runs (`f64f51…`): only `scripts/` and
`docs/` changed between them, never `src/`, `runner/` or `modal_app/`.

Provenance note: the working tree was **dirty** at run time (`source_tree_dirty=true`)
because the local `reload()` fix is intentionally not committed in this round. `git HEAD`
differed per run (pilot3 `e077ab51…`, pilot4 `11d8055b…`, recovery `ae97a5b6…`) and the
repository source SHA recorded in each run is the HEAD of that moment; the executed
image source tree hash did not change (image spec SHA stable).

## 3. Runs

| Run | Volume prefix | run_id | Shards / tasks | source SHA (HEAD) | Result |
|---|---|---|---|---|---|
| 2-shard pilot (previous code state) | `scalability_v6_pilot3` | `pilot-e077ab51-20261107T103217Z` | 2 / 23 | `e077ab51` | both shards `COMPLETED`; orchestrator cleanup had crashed, data re-downloaded with the fixed code |
| full Pilot | `scalability_v6_pilot4` | `pilot-11d8055b-20261107T110026Z` | 6 / 64 | `11d8055b` | 6/6 `COMPLETED`, 0 errors, orchestrator exit 0 |
| recovery discovery (crash) | `scalability_v6_recovery2` | `recovery-crash-0002` | 1 / 12 | `ae97a5b6` | attempt 0 killed by the 120 s Function timeout after 2 final records |
| recovery resume | `scalability_v6_recovery2` | `recovery-crash-0002` | 1 / 12 | `ae97a5b6` | attempt 1: 2 skipped, 10 executed, shard `COMPLETED` |

## 4. Verdict consistency against the audited GitHub references

Classification of every frozen Pilot cell whose `source_task_id` is matched to the Modal
record's `task_id` (reference fields: `verdict`, `execution_status`, `wall_s`). Categories
are mutually exclusive and are counted, not merged:

| Category | 2-shard pilot3 (23 cells) | full Pilot4 (64 cells) | recovery shard 000 (12 cells) |
|---|---|---|---|
| `match` (ref COMPLETED + Modal COMPLETED, same verdict) | 19 | **53** | 9 |
| `timeout_match` (both sides `TIMEOUT_WALL_CLOCK`) — **not** a verdict match | 4 | **11** | 3 |
| `boundary_flip` (ref COMPLETED → Modal `TIMEOUT_WALL_CLOCK`) | 0 | **0** | 0 |
| `timeout_improved` (ref TIMEOUT → Modal COMPLETED) | 0 | 0 | 0 |
| `verdict_mismatch` (both COMPLETED, different verdict) | **0** | **0** | **0** |
| `modal_error` (Modal `ERROR`) | 0 | 0 | 0 |
| `missing` / `extra` | 0 / 0 | 0 / 0 | 0 / 0 |

Full Pilot4 verdict distribution: `VER=21, AMB=20, INC=12, TIMEOUT_WALL_CLOCK=11` — exactly
the reference distribution (`21/20/12/11`). Audit `reference_comparison.verdict_mismatches`
is empty. **`verdict_consistency_pass = true`; `verdict_mismatch = 0`, so no stop condition
was triggered.**

`boundary_flip` counting is separate from both `match` and `verdict_mismatch` (the Modal side
has no verdict at a timeout); the "both sides timeout" cells are reported as
`timeout_match` and are deliberately **not** counted as verdict agreement.

### Boundary-flip risk for the formal matrix

Zero boundary flips were observed in 64 cells, but the margin is thin and must be expected
to produce *some* flips in the 8,280-cell matrix:

* matched-cell wall ratio (Modal/reference) is p50 **1.494**, p90 2.213, p95 2.346,
  max 2.512, min 0.672 — Modal is slower on 43/53 matched cells. The high ratios happen on
  cheap 0.3–1.5 s reference cells where the fixed per-cell overhead dominates; the absolute
  penalty is ~0.3–0.7 s.
* among 53 reference-COMPLETED cells, 8 had reference wall > 40 s and 11 of the Modal
  COMPLETED cells exceeded 40 s; the slowest completed Modal cell took **57.237 s = 95.4 %
  of the 60 s cap** (reference 38.310 s, ratio 1.494), and another took 54.518 s.
* therefore reference cells in the 40–58 s band can cross 60 s on Modal. This is a
  hardware-speed boundary effect: it must be reported as `boundary_flip`
  (right-censored, no verdict), never silently scored as a verdict match, and it is not a
  `verdict_mismatch`. A flip whose *reference* wall is < 30 s (i.e. < 50 % of the cap) would
  contradict a pure hardware-speed explanation and is flagged by the report as
  `suspect_environment_regression`; there were **0** such suspects in all three runs.
* the 11 reference-timeout cells all stayed `TIMEOUT_WALL_CLOCK` on Modal (Modal wall
  60.035–60.175 s), which confirms the timeout/null semantics are portable.

## 5. Resource measurements (full Pilot4, 6 shards, 64 tasks)

Every final record carries both `wall_s` and `peak_rss_mb`:
`resource_measurements_recorded = true`, `peak_rss_missing = 0`.

### 5.1 Per-cell wall clock

| Population | n | p50 | p90 | p95 | max |
|---|---|---|---|---|---|
| all final records | 64 | 2.220 s | 60.106 s | 60.116 s | 60.175 s |
| `COMPLETED` only | 53 | 1.414 s | 48.556 s | 53.287 s | 57.237 s |
| `TIMEOUT_WALL_CLOCK` (capped) | 11 | 60.113 s | 60.175 s | 60.175 s | 60.175 s |

Observed task wall sum: **1,420.365 s** (`23.673 min`).

### 5.2 Peak RSS

| Population | n | p50 | p90 | p95 | max |
|---|---|---|---|---|---|
| all final records | 64 | 191.3 MiB | 668.1 MiB | 752.1 MiB | **1,671.8 MiB** |
| `COMPLETED` only | 53 | 179.6 MiB | 366.9 MiB | 435.9 MiB | 1,321.5 MiB |

Maximum observed RSS is **1,671.8 MiB = 40.8 % of the 4,096 MiB reservation** (cell
`cfg6-6d9fb9c2c4dcd5ab9dad`, F09, timeout at the cap; the RSS sampling covers the whole
cell process tree). This is much higher than the ~269 MiB max of the GitHub reference
audit — the difference is attributed to the gVisor container environment and the heavier
SciPy/HiGHS allocations of the timed-out cells. There is no OOM anywhere:
**`oom_events = 0`** and no record/container error mentions a memory failure; the
observed maximum still leaves ~2.4 GiB headroom.

### 5.3 Per-task Volume commit / orchestration overhead

The worker calls `Volume.commit()` after **every** final task (plus one final commit per
shard). The overhead is estimated as `shard wall_s − Σ task wall_s`; it is an *upper
bound* on the commit cost because it also contains the `run_cell.py` subprocess startup,
checkpoint/ledger/hash I/O and record bookkeeping:

| Shard | tasks | shard wall | Σ task wall | overhead | overhead/task |
|---|---|---|---|---|---|
| 000 | 12 | 426.596 s | 411.445 s | 15.151 s | 1.263 s |
| 001 | 11 | 306.645 s | 266.979 s | 39.666 s | 3.606 s |
| 002 | 12 | 210.213 s | 194.658 s | 15.555 s | 1.296 s |
| 003 | 12 | 197.508 s | 186.193 s | 11.315 s | 0.943 s |
| 004 | 12 | 304.254 s | 294.555 s | 9.699 s | 0.808 s |
| 005 | 5 | 75.227 s | 66.535 s | 8.692 s | 1.738 s |
| **total** | **64** | **1,520.443 s** | **1,420.365 s** | **100.078 s** | **1.564 s** |

So the per-task commit+bookkeeping cost is ~**1.56 s/task on average** (0.81–3.61 s/task
per shard), i.e. ~6.6 % of total shard wall time. Additional per-shard overhead measured
from the orchestrator side (spawn → result, includes container start/queueing and result
transfer) is 7.6–12.4 s per shard:

| Shard | attempt wall (orchestrator) | shard wall (worker) | container+queue |
|---|---|---|---|
| 000 | 438.542 s | 426.596 s | 11.946 s |
| 001 | 318.715 s | 306.645 s | 12.070 s |
| 002 | 222.644 s | 210.213 s | 12.431 s |
| 003 | 205.087 s | 197.508 s | 7.579 s |
| 004 | 313.057 s | 304.254 s | 8.803 s |
| 005 | 86.089 s | 75.227 s | 10.862 s |

### 5.4 SCG cache

64 tasks, 59 distinct model hashes: 5 cache hits / 59 misses / 0 disabled. The 5 hits
(3.9 % of completed cells) come from the frozen model-colocated shard plan; this is a
pilot-only signal because the Pilot's 64 cells have almost one distinct model each. It is
not evidence about the formal cache-hit rate.

## 6. Real crash / checkpoint-recovery test

Run: prefix `scalability_v6_recovery2`, run_id `recovery-crash-0002`, one frozen shard
(12 tasks), frozen cell cap 60 s.

1. **Discovery run** (`pilot --pilot-shards 1 --function-timeout 120 --max-parallel 1
   --max-infra-retries 0`): the Function was killed by Modal at its 120 s timeout
   (orchestrator observed `FunctionTimeoutError ... hit its timeout of 120s` after
   129.077 s). Because the worker commits after every task, the Volume held **2 final
   records** (1 `COMPLETED` 54.107 s, 1 `TIMEOUT_WALL_CLOCK` 60.143 s) and a 2-line
   ledger; the shard had no `metrics/` document, i.e. it was unambiguously incomplete.
   `--max-infra-retries 0` is used only for this discovery step so that the run stops
   after the kill instead of silently retrying (default retries would have resumed inside
   the same command); the retry/resume semantics is exactly what the next step tests.
2. **Resume run** (`resume --run-id recovery-crash-0002 --only-shards 000`, normal 5400 s
   Function timeout): the orchestrator saw 1 shard needing work, assigned attempt 1, and
   the worker read its ledger/raw state: **2 tasks skipped**, **10 tasks executed**,
   shard `COMPLETED` (9 COMPLETED + 3 TIMEOUT_WALL_CLOCK, 0 errors, 0 unfinished).
3. **Audit** (`scripts/audit_v6_modal_results.py --allow-partial`): PASS — exactly
   **12 unique task ids**, one final record each, one final ledger entry each, ledger
   SHA-256 equals the raw file SHA for every task, no duplicate final entries, hashes and
   frozen fields match, 9/3 verdict split consistent with the audited reference
   (0 verdict mismatches, 0 boundary flips).

Evidence (`work/v6_modal_recovery/recovery_test.json`):

| Field | Value |
|---|---|
| `PASS` / `MODAL_CHECKPOINT_RECOVERY_PASS` | **true** / **true** |
| `resume_skipped_completed` | **true** (2 completed tasks skipped by attempt 1) |
| `no_duplicate_records` | **true** |
| `tasks_recovered` | **10** |
| `tasks_missing_after_discovery` | 10 (recovered == missing: only the missing tasks were re-executed) |
| `attempts` | `[{attempt:0, invocation fc-01M6TVP5VN…, tasks_total:12}, {attempt:1, invocation fc-01M6TW0RGS…, tasks_total:12}]` |
| ledger | `work/v6_modal_recovery/resume/completion_ledger/shard_000.jsonl` (12 final entries) |
| audit | `work/v6_modal_recovery/resume/audit/audit.json` |
| discovery manifest | `work/v6_modal_recovery/discovery/run_manifest.json` (`INFRA_FAILURE`, `tasks_unfinished` state) |

One aborted first recovery attempt (prefix `scalability_v6_recovery`) was discarded: the
resume step was started without `--only-shards`, which would also have executed the five
never-started shards of the frozen plan, so it did not test single-shard recovery
cleanly. Its local artifact directories were removed; a fresh prefix/run id was used for
the reported test. The residual spend of that aborted attempt is included in the
cumulative cost below.

## 7. Cost observation

All values are the official `modal billing summary --for 'this month' --json` metered-cost
difference; the monthly meter is not a per-run invoice.

| Measurement | Value |
|---|---|
| Authorized cap | $10.00 |
| Baseline metered cost (`budget.json`) | $23.58024702 |
| Cumulative spend after this round | **$0.100005** (10.0 cents, 1.0 % of cap) |
| Remaining | **$9.90** |
| 2-shard pilot3 + first images (before this round) | ~$0.04 |
| Full Pilot4 (64 cells, 6 containers) delta | **$0.05** |
| Recovery round (crash + resume + aborted first attempt) | ~$0.01 |
| Compute-time estimate, full Pilot4 | 0.3945 CPU-h + 1.578 GiB-h = **$0.0313** (rates $0.0473/CPU-h, $0.008/GiB-h) |
| Pilot4 volume + network egress | `volumes $0.000237`, `network_egress $0.000015` (monthly, unchanged) |

Formal-matrix projections from the Pilot (64 cells, 22.19 s/cell task wall):

| Basis | CPU-hours | Estimate | Wall at `--max-parallel 8` |
|---|---|---|---|
| Pilot per-cell task wall (22.19 s/cell) | 51.0 | **$4.05** | 6.4 h |
| incl. 1.56 s/task commit+bookkeeping (23.75 s/cell) | 54.6 | **$4.33** | 6.8 h |
| gate-decision expectation (18.8 s/cell) | 43.2 | $3.43 | 5.4 h |
| billing-delta based ($0.05 / 64 cells) | — | $6.47 (upper bound, coarse meter) | — |
| worst case (all cells hit the 60 s cap) | 138.0 | $10.94 | 17.3 h |

All credible estimates stay below the $9.5 guard threshold; the worst case is above the
cap by design (the guard stops the formal run before the cap is crossed). The
budgetguard state projects $3.43 for the formal matrix.

## 8. Limitations and open points

* The Pilot is 64 pre-registered cells in 6 shards, not a sample of the 8,280-cell formal
  mix (formal quotas A=600, B=1,080, C=5,400, D=1,200). The per-cell cost/verdict
  statistics are indicative, not a mathematical guarantee of formal portability.
* Boundary flips are expected in the formal matrix (see §4): one completed cell already
  reached 95.4 % of the cap and paired ratios ranged 0.87–1.49 near the cap. The formal
  analysis must keep counting them as right-censored timeouts, never as verdict matches.
  Any flip with a reference wall < 30 s should be escalated as environment regression.
* Modal peak RSS (max 1,671.8 MiB) is ~6× the GitHub reference maximum. It is within the
  4 GiB reservation but shrinks the memory headroom; if the formal run's heavier cells
  approach 4 GiB, the reservation must be raised (or cells profiled) before the formal run.
* The recovery prefix's frozen `run_metadata` records `function_timeout_s = 120` (from the
  discovery run) while the resume invocation actually used the 5400 s default; the
  resume manifest therefore reports 120 s in `resources.function_timeout_s`. This is a
  provenance/documentation wrinkle only — the resume's actual behavior (attempt 1 under a
  5400 s timeout) is evidenced by its 328.5 s worker wall and the attempt marker.
* The monthly metered meter has cent-level granularity, so the $0.05 delta is a rounded
  estimate; the compute-time estimate is the sharper per-cell signal.
* The full Pilot ran with a dirty worktree (uncommitted local `reload()` fix in
  `scripts/run_v6_modal.py`); the executed image sources (`src/`, `runner/`,
  `modal_app/`) did not change (image spec SHA `f64f51…` identical across runs). The fix
  must be committed before the formal run so the formal provenance is clean.
* The local simulation validates the infrastructure using the Modal venv's numpy/scipy,
  not the pinned image; container-level numerical portability is covered by the Pilot's
  reference-verdict comparison (0 mismatches) and the recorded image spec SHA.
* No token/secret/`.modal.toml` was read, printed or stored in any artifact; only
  metered-cost fields were kept.

## 9. Artifact index

| Path | Content |
|---|---|
| `work/v6_modal_pilot_2shard/` | re-download of the 2-shard pilot3 Volume (23 tasks, 2 shards), `SHA256SUMS`, `audit/` |
| `work/v6_modal_pilot_2shard/reference_comparison.json` | per-cell match/timeout_match/boundary_flip/verdict_mismatch classification (2 shards) |
| `work/v6_modal_pilot_2shard/pilot_summary.json` | resource/verdict summary for the 2-shard subset |
| `work/v6_modal_pilot/run_manifest.json` | full Pilot4 run manifest (shards, attempts, billing, budget guard) |
| `work/v6_modal_pilot/audit.json` | independent audit result (copy of `audit/audit.json`) |
| `work/v6_modal_pilot/reference_comparison.json` | per-cell reference comparison (64 cells) with counts |
| `work/v6_modal_pilot/pilot_summary.json` | required summary: `tasks_expected/tasks_recorded/tasks_completed/missing/duplicates/verdict_consistency_pass/boundary_flips/oom_events/resource_measurements_recorded/infra_failures`, wall/RSS stats, compute estimate, `MODAL_ENVIRONMENT_PASS` |
| `work/v6_modal_pilot/audit/` | `audited_records.jsonl`, `records.csv`, `group_statistics.csv`, `technical_failures.json` |
| `work/v6_modal_recovery/discovery/` | killed discovery run download (2 final records, attempt-0 marker, INFRA_FAILURE manifest) |
| `work/v6_modal_recovery/resume/` | resumed run download (12 tasks, audit, reference comparison, summary) |
| `work/v6_modal_recovery/recovery_test.json` | required recovery verdict: `PASS`, `resume_skipped_completed`, `no_duplicate_records`, `tasks_recovered`, `attempts`, `evidence` |

Reproduce the paid runs (each with `--confirm-budget`; none of these is the formal run):

```bash
python scripts/run_v6_modal.py pilot  --confirm-budget --volume-prefix scalability_v6_pilot4 \
       --max-parallel 6 --download-dir work/v6_modal_pilot
python scripts/audit_v6_modal_results.py --download-dir work/v6_modal_pilot --out work/v6_modal_pilot/audit
python scripts/run_v6_modal.py report --download-dir work/v6_modal_pilot

python scripts/run_v6_modal.py pilot  --confirm-budget --volume-prefix scalability_v6_recovery2 \
       --pilot-shards 1 --function-timeout 120 --max-parallel 1 --max-infra-retries 0 \
       --run-id recovery-crash-0002 --download-dir work/v6_modal_recovery/discovery
python scripts/run_v6_modal.py resume --confirm-budget --volume-prefix scalability_v6_recovery2 \
       --run-id recovery-crash-0002 --only-shards 000 --max-parallel 1 \
       --download-dir work/v6_modal_recovery/resume
python scripts/audit_v6_modal_results.py --download-dir work/v6_modal_recovery/resume \
       --allow-partial --out work/v6_modal_recovery/resume/audit
python scripts/run_v6_modal.py recovery-report \
       --download-dir work/v6_modal_recovery/resume \
       --discovery-dir work/v6_modal_recovery/discovery --shard-ids 000 \
       --recovery-out work/v6_modal_recovery/recovery_test.json
```
