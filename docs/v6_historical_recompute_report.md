# v6 historical 36,450-evaluation impact recompute (PAUSED snapshot)

This is an **add-on** audit of the archived historical baseline; it is disjoint from
the formal 8,280-cell v6 matrix, does not consume its budget and must never be pooled
with its results.

## Scope and frozen manifest

- frozen task manifest: `protocol/v6_historical_recompute_tasks.json` (SHA-256 `e1f6b455285e45cadbec655749b92db3c53b3fdf551dabbd3d7722804bc47a8f`), 3,645 evaluations over 555 bounded problems
- archived verdicts: VER 1,689, AMB 1,956
- conservative upper bound if the whole 36,450-row baseline were recomputed: 36,450 rows

## Primary verdict pass (complete)

- 3,645/3,645 rows recorded (100.0 %), 3,645 completed, 0 wall-clock timeouts, 0 errors.

### Verdict drift

| archived | recomputed | rows |
|---|---|---:|
| VER | VER | 1,689 |
| AMB | AMB | 1,956 |

- rows with a verdict change: **0** of 3,645
- problems with any row drift: **0** of 555
- INC / not-decidable rows: 0 / 0

## COMPLETE_SET critical-pair pass (paused)

- scope: archived AMB rows; recorded 429/1,956 (21.9 %), 215 completed, 214 timeouts
- status: **paused at a task boundary** (safe pause), resumable by task id; see `resume_commands` in `work/v6_historical_recompute/summary/summary.json`

Critical-pair comparison (comparable rows: 1,904):

| archived verdict | rows | equal | increased | decreased |
|---|---:|---:|---:|---:|
| VER | 1,689 | 1,689 | 0 | 0 |
| AMB | 215 | 0 | 51 | 164 |

The decrease for AMB rows reflects the documented change of the terminal-pair universe: the
archived count is a complete v4 enumeration, the recomputed value is the v6 first-pass
counter plus the enclosure-rejected pairs; the LP-feasible certificate count is reported
separately in the complete pass (mean 160.16 feasible pairs on AMB rows, 0 on VER rows).

## SCG fingerprint sample

- sample: 120 distinct regenerated v6 models in conflict tier A; legacy semantics reconstructed from pre-repair commit `97b5d6db3227`
- classes_structure_equal=0, classes_full_dbm_equal=0, edges_equal=0, smaller_v6_class_count=117, larger_v6_class_count=3, equal_class_count=0

## MSCG-LP reference recomputation plan (planning only, not executed)

- archived reference rows: 36,450; method ['mscg_lp_complete'], mode ['FIRST_CERTIFICATE']
- published estimate 97962.6 s; recomputed from the archived CSV 97962.6 s (match: True)
- the reference imports the shared `build_scg`, so it is not an independent SCG oracle; a re-run must be budgeted separately from the formal 8,280 matrix

## Limitations

- the archived Proposed shards carry no mode column; FIRST_CERTIFICATE is the documented assumption, supported by the 0.108 median proposed/reference runtime ratio
- the archived Proposed shards carry no verifier counters, so the archived critical-pair count is taken from the paired archived MSCG-LP reference record (complete enumeration, different baseline implementation)
- archived certificate overlap endpoints were never persisted, so archived-vs-v6 overlap endpoint deltas are only available on the legacy-semantics replay sample
- the conflict tier is a necessary-condition exposure screen; the 32,805 no-conflict rows remain inside the conservative upper bound and are not certified unaffected
- the complete pass is censored at 60 s for the slowest AMB rows; censored rows keep their first-pass verdict but have no complete critical-pair count
- verdict drift measured here is for the historical rmt_fms baseline only and does not transfer to the 8,280-cell v6 formal matrix

## Status

`PAUSED_FOR_SAFE_SNAPSHOT` — Primary verdict pass is complete (3,645/3,645). The auxiliary complete-set pass on archived-AMB rows was suspended at a task boundary by an explicit safe-pause request; it is resumable by task_id. No verdict, counter or file in the primary pass depends on the suspended pass.
