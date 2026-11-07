# v6 status report: transparent limitation report (no formal pass)

## Status

The v6 Scalability Benchmark is **not** submission-ready. The formal 8,280-cell
protocol was **not frozen** and the formal run was **not started**, because the
pre-registered gates are not all satisfied:

```text
SCG_STRONG_TIME_PASS                = true
SCG_SOUNDNESS_AND_COMPLETENESS_PASS = true
DBM_NORMALIZATION_PASS              = true
ALL_12_FAMILIES_SEMANTIC_PASS       = true
CACHE_INTEGRITY_PASS                = true
CACHE_DIFFERENTIAL_PASS             = false   (7/12 complete pairs; 0 mismatches)
PILOT_BUDGET_AND_MEMORY_PASS        = false   (60.4 % censoring; 84.3 h expected /
                                                138 h worst-case compute)
GITHUB_RESOURCE_POLICY_PASS         = not requested
MODAL_BUDGET_POLICY_PASS            = not requested (balance/cap not verifiable
                                                from the CLI)
```

This is a deliberate stop before spending the formal budget, as the execution
handoff requires. Nothing below is a claim that the paper's v6 results are
valid; it is the opposite — a quantified statement of what blocks them.

## What is established

1. **Strong-time semantics are fixed and independently checked.** The
   `_compute_successor()` urgency repair, the Fraction oracle and the 12-family
   witnesses are unchanged; the semantics gate (`protocol/v6_semantics_gate.json`,
   run `42322708113`) is still PASS, and the current HEAD passes the 48-test
   suite (35 original + 13 new cache tests).
2. **The Pilot is audited and preserved.** 288/288 unique records, 128
   `COMPLETED`, 160 `TIMEOUT_WALL_CLOCK`, 8 `INC`, 58 `VER`, 62 `AMB`, 24 shards,
   peak RSS 269.4 MiB; the 126 zero-filled `SCG_COMPLETE` checkpoints remain
   raw and are nulled only in the audited view.
3. **A semantics-preserving same-model SCG cache exists and is audited.**
   Content-addressed key (model / source / builder / semantics / Python /
   serialization), streaming JSON v2 with an integrity trailer, atomic writes,
   every failure a miss. `verify()` gained an optional prebuilt-SCG path without
   touching the default path. Frozen protocol `v6-cache-validation-1`
   (96 cells, 12 families × 8, 60 miss / 36 hit) ran as GitHub Actions
   `42339602408` on commit `d2e83bf`: 96/96 records, 0 ERROR, 0 store failures,
   0 degraded cells, 36/36 hits with a prior same-key miss, all keys and
   provenance verified, peak RSS 254.0 MiB (P95) / 271.4 MiB (max).
4. **The historical baseline has a quantified impact audit.** 36,450 archived
   proposed evaluations recovered (255 shards, 5,589 bounded problems, VER
   20,412 / AMB 16,038, identity-exact with the archived reference). The v6
   generator repair has **zero row-level impact** on that v4-era `rmt_fms`
   baseline; the shared SCG repair does. Conservative bound: all 36,450 rows
   need recomputation; evidence-driven minimum: 555 bounded problems / 3,645
   rows. The archived MSCG-LP reference is **not independent** of the shared
   SCG core (`build_scg` is imported and walked), so its archived values also
   predate the repair.

## Why the formal matrix is blocked

* **The cache is correct but not the bottleneck.** It saved about 32 s of
  compute across an entire formal matrix (< 0.01 %). In the validation run,
  cache-hit cells of heavy families still hit the 60 s wall in the
  synchronizer: 60.4 % of cells were right-censored, and the completed
  L-sweep cells are cheap. Fixing construction cannot fix censoring.
* **The differential gate cannot be met at a 60 s cell cap on heavy families.**
  7 of 12 cold/cached pairs were comparable and all 7 were identical; the other
  5 were censored because both sides timed out. Editing the frozen protocol to
  make that pass would be post-hoc, so the gate stays false.
* **The formal budget is unchanged in order of magnitude.** Censoring-aware
  expectation is ≈5,058 runner-minutes (84.3 h) compute, worst case 8,280
  runner-minutes (138 h); at 20-way concurrency that is ≈4.2 h expected / ≈6.9 h
  worst of pure compute (one to two waves under a 6 h job cap). Spending that
  requires an explicit resource-policy decision that has not been given.

## What is not delivered (and must not be claimed)

* `protocol/scalability_v6_frozen_protocol.yaml` and the immutable 8,280-task
  list: **not created**.
* Formal 8,280 raw records, formal independent audit, Figure 6 (SVG/PDF/EPS/PNG),
  Section V-C / Supplementary S7-B minimal-change suggestions, the final
  `TII_Scalability_Benchmark_v6_Corrected_FullScale_Results.zip`, and the
  package self-test: **not produced**.
* No statement that v5/v6 historical Proposed verdicts remain valid under the
  repaired semantics.

## Options to unblock

1. **Fix the dominant stage.** A proof-carrying, differential-tested optimization
   of the synchronizer / temporal expansion / LP path is the only change that
   can reduce the 60 % censoring. Any such change must ship with semantic
   equivalence evidence before the formal protocol is frozen.
2. **Authorize the formal budget.** ~84 h expected / ~138 h worst-case compute.
   On free GitHub public-repo runners this is ~4–7 h wall at 20 concurrency and
   needs explicit authorization; on Modal, current published rates
   ($0.0473/CPU-hour, $0.008/GiB-hour) imply roughly **$4–6** for 84 h at one
   core and ≤1 GiB per cell before egress, but the remaining credit balance and
   a workspace budget cap must be confirmed in the Modal console first (this
   month's $23.58 metered usage was fully covered by credits).
3. **Re-design the differential validation** for censored heavy families as a
   new pre-registered protocol version (not an edit to `v6-cache-validation-1`).
4. **Re-run the affected historical subset** (minimum 3,645 rows; conservative
   36,450) and the MSCG-LP reference (~27.2 h) before reusing the background
   baseline.

## Evidence inventory

| Item | Location |
|---|---|
| Cache design and correctness evidence | `docs/v6_scg_cache.md` |
| Cache-validation audit and gate decision | `docs/v6_cache_validation_report.md` |
| Historical impact audit | `docs/v6_historical_impact_audit.md` |
| Validation raw artifacts + SHA-256 | `work/v6_cache_validation_42339602408/` |
| Independent audit outputs + SHA-256 | `work/v6_cache_validation_audit_42339602408/` |
| Historical audit outputs + SHA-256 | `work/v6_historical_impact/` |
| Pilot artifacts and audit (unchanged) | `work/v6_pilot_run_42323424751/`, `work/v6_pilot_audit_42323424751/` |

Run IDs: semantics `42322708113`; pilot `42323424751`; cache-validation
`42339602408`. Code HEAD at report time: `1a795c3`.
