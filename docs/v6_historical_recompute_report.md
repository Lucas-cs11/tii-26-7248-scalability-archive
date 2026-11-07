# v6 historical recompute and verdict-drift audit (555 problems / 3,645 evaluations)

Status: **PRIMARY VERDICT PASS FINAL; COMPLETE_SET SECOND PASS RUNS ON GITHUB ACTIONS**
(local snapshot: `LOCAL_PRIMARY_PASS_FINAL_COMPLETE_SET_ON_GITHUB_ACTIONS`).

This is an **add-on audit** of the archived 36,450-evaluation v4-era `rmt_fms`
baseline. It is disjoint from the formal 8,280-cell v6 matrix: separate
workflow, separate artifacts, no shared budget, and its numbers must never be
pooled with the formal matrix results.

## 1. Frozen scope

- `protocol/v6_historical_recompute_tasks.json` — SHA-256
  `e1f6b455285e45cadbec655749b92db3c53b3fdf551dabbd3d7722804bc47a8f`,
  byte-deterministic, **3,645 tasks / 555 bounded problems** (archived
  VER 1,689 / AMB 1,956; 1,644 distinct regenerated v6 models; 27 source
  shards; `L=10`, `H ∈ {33, 36, 39}`), `mode="first"`, `complete_set=false`.
- Selection rule: the 3,645 rows of `historical_rows_recovered.csv` with
  `scg_tier == A_static_urgency_conflict_detected` (static urgency conflict,
  a necessary-condition exposure screen for the shared SCG strong-time repair).
- Freeze evidence: `work/v6_historical_recompute/summary/frozen_manifest.sha256`
  (written before the first recomputation) plus
  `summary/frozen_manifest_build.json`; every `run` re-verifies the SHA.
- Source inputs (SHA-256): recovered rows
  `11bc971bb08459b0024af019fa7b0c1b2264ac2c7f2a7ee56419c22e87caa29f`,
  shard manifest `8842395f91435af9ee8f8d3be1a6d9596d0b26df013cbe28b9aa6c9c98b76583`,
  archived MSCG-LP reference CSV
  `a854f364270f07223641aa3d67da3f45a95b753edb2e59d2cb1f311e8cfbe973`.
- Conservative upper bound: all 36,450 rows / 5,589 bounded problems; the
  32,805 no-conflict rows are **not certified unaffected**.

## 2. Primary verdict pass (FINAL, local, 60 s cap ≤ 8 workers)

`run --phase first`: `complete_set=False` (archived Proposed protocol), one
fresh forked child per task with a hard 60 s SIGKILL cap, per-task peak RSS and
last completed verifier stage.

| metric | value |
|---|---:|
| tasks recorded / planned | **3,645 / 3,645 (100.0 %)** |
| completed / timeout / error / INC | 3,645 / 0 / 0 / 0 |
| wall-clock sum (single-core) | 427.1 s (mean 0.117 s, max 0.945 s) |
| pool wall | 58.1 s at 8 workers |
| peak RSS p50 / p95 / max | 37.0 / 39.5 / 42.3 MB |

Verdict drift (archived → recomputed), denominator 3,645:

| archived \ recomputed | VER | AMB | INC | timeout | not run |
|---|---:|---:|---:|---:|---:|
| VER (1,689) | **1,689** | 0 | 0 | 0 | 0 |
| AMB (1,956) | 0 | **1,956** | 0 | 0 | 0 |

- rows with a verdict change: **0 / 3,645**; problems with any row drift:
  **0 / 555**; not-decidable rows: 0.
- Pipeline validation: all 3,645 regenerated models match the frozen
  `v6_model_hash`; model `(L,H)` matches the archived bounds; the
  archived-semantics replay reproduces the archived verdict on 62 / 62 usable
  sampled cells.
- Mode assumption: the archived shards carry no `mode` column;
  `FIRST_CERTIFICATE` is the documented default and is supported by the
  archived proposed/reference runtime ratio (median 0.108 for tier A — the
  Proposed pass stops at the first certificate while the reference enumerates
  completely).

## 3. COMPLETE_SET second pass (GitHub Actions)

Purpose: per-row critical-pair count and certificate time-overlap interval under
`complete_set=True`, on the 1,956 archived-AMB rows. VER rows are already fully
enumerated by the first pass (no early exit) and need no second pass.

Execution is moved to GitHub Actions (no Modal, no local long run):
`.github/workflows/scalability_v6_historical_complete.yml`

- `validate`: checkout, Python 3.13 + `pip install --require-hashes -r
  requirements-v5.lock`, unit tests (67), frozen-manifest SHA/invariant check
  (`verify-manifest`), committed primary-pass summary check, shard matrix.
- `run`: matrix `0..N-1` (default 20 shards, parameterisable), ubuntu-22.04,
  `timeout-minutes: 180`, `max-parallel: 20`; each shard runs
  `run --phase complete --shard I --num-shards N --out results/hist`,
  deterministic assignment `stable_index % N == I` over AMB tasks sorted by
  `task_id` (98/97 tasks per shard), per-task 60 s wall cap, 2 pool workers,
  thread limits pinned (`OMP/BLAS/MKL=1`, `PYTHONHASHSEED=0`), artifact
  `v6-hist-complete-shard-<I>-<run_id>` uploaded always.
- `aggregate`: needs `run` (runs even on partial failure), downloads all shard
  artifacts, runs `aggregate --shards-dir artifacts --out results/hist_aggregate`,
  validates each AMB task appears exactly once, and uploads
  `v6-hist-complete-aggregate-<run_id>` (`summary.json`, `drift_detail.csv`,
  `coverage.json`, `shard_manifest.json`, `aggregate_inputs.json`).
  `snapshot_status` is `FINAL` only when all 1,956 AMB rows are recorded exactly
  once with status `COMPLETED|TIMEOUT_WALL_CLOCK` and the primary summary is
  supplied; otherwise `FINAL_PARTIAL` with the explicit denominator.

Local smoke results (no full run): shard 3 with `--limit 2` produced one
completed AMB row (1,920 certificates, overlap `[8,12]`, SCG 130 classes/213
edges) and one honest `TIMEOUT_WALL_CLOCK`; `aggregate` on that single shard
correctly reported `FINAL_PARTIAL` (recorded 2/1,956, 19 missing shard files)
while still combining the 1,689 VER rows; the 67 unit tests pass.

Expected GitHub run: 20 parallel shard jobs × ~98 tasks × ~37 s mean complete
wall / 2 workers ≈ **30–45 min** per shard, ~40–60 min total wall including
aggregation. Public repository: free. If private, ~900–1,000 job-minutes on a
standard 2-core Linux runner. Per-task timeouts are recorded as
`TIMEOUT_WALL_CLOCK` and never converted into a verdict.

**Final COMPLETE_SET numbers placeholders (fill after downloading the aggregate
artifact):**

| field | value (from `results/hist_aggregate/summary/summary.json`) |
|---|---|
| snapshot_status | `<<FINAL or FINAL_PARTIAL>>` |
| complete-pass recorded / completed / timeout | `<<x / 1,956>>` |
| critical pairs equal / increased / decreased | `<<...>>` |
| mean/median archived vs recomputed pairs | `<<...>>` |
| feasible pairs positive / zero | `<<...>>` |
| overlap inside archived window | `<<...>>` |
| SCG classes smaller/equal/larger | `<<...>>` |

Pre-GH local snapshot retained as fallback
(`raw/recompute_complete.jsonl`, 431 / 1,956 recorded, 217 completed,
214 `TIMEOUT_WALL_CLOCK`); it is **superseded** by the GH aggregate for the final
COMPLETE_SET statement. On that snapshot the AMB subset shows 0 verdict changes,
no AMB row with zero feasible certificates, all certificate overlaps inside the
archived `[L,H]` window (width p50 2.5 s, p95 5.5 s), and a critical-pair
comparison of 217 rows: 165 decreased / 52 increased / 0 equal (mean archived
52,408 pairs vs mean recomputed 1,424 pairs).

## 4. Critical-pair and time-domain semantics (definitions and caveats)

- Archived critical-pair count = `terminal_pairs` of the paired archived
  MSCG-LP reference record (same evaluation id; complete enumeration, v4
  semantics). The archived Proposed shards store no verifier counters.
- Recomputed critical-pair universe = `terminal_pairs +
  enclosure_rejected_pairs` of the COMPLETE_SET pass; recomputed feasible
  critical pairs = `len(certificates)`.
- The archived reference counter is **not definitionally identical**: its
  `terminal_pairs` counts all normal×fault path pairs of its own complete path
  enumeration, while the verifier counts the pair universe of its
  synchronizer/expansion pipeline. The apples-to-apples semantics comparison is
  the archived-semantics replay: the pre-repair `_compute_successor` is
  monkeypatched back from git history and rerun. On the 61 usable sampled
  cells the old and new critical-pair universes and overlap endpoints are
  identical (61/61), i.e. the repair changes the SCG shape but not the
  certificate universe of those cells.
- Time-feasible domain: archived certificate overlap endpoints were never
  persisted, so archived-vs-v6 endpoint deltas are only available on the
  legacy replay sample; the COMPLETE_SET pass supplies the recomputed endpoints
  and the containment check against the archived bounded window `[L,H]`.

## 5. SCG fingerprint sample (120 models, archived vs v6 semantics)

Deterministic sample of 120 distinct regenerated v6 models from the conflict
tier; the pre-repair successor is loaded from git history (`f9bac23^`) and both
SCGs are compared by class structure, canonical DBM signature per class and
edges.

| comparison | equal | different |
|---|---:|---:|
| class structure | 0 | 120 |
| canonical DBM (full class) | 0 | 120 |
| edges | 0 | 120 |

Mean shape: archived-semantics **517.5 classes / 1,013.9 edges** → v6
**175.4 / 304.3**; 117 sampled models smaller, 3 larger. Across all 3,645 rows
the recomputed SCG class count is smaller in 3,537 rows, larger in 108 (median
−338 classes / −710 edges). The archived SCG workload counters for the conflict
tier are therefore not reusable under v6 — while the verdicts themselves are
unchanged on this set.

## 6. MSCG-LP reference recompute: plan and budget (planning only, not executed)

The archived MSCG-LP reference is **not independent** of the repaired shared
SCG (it imports the same `build_scg`), so it must eventually be regenerated
under v6; this deliverable only plans it.

- Estimate review: sum of `runtime_total_median` over the 36,450 archived
  reference records = **97,962.629 s**, matching the published 97,962.6 s
  (single pass: `n_repeats=1`, `n_warmup=0`; orchestration excluded).
- Affected rows: evidence-driven minimum **3,645 rows / 555 bounded problems**
  (1:1 with this recompute set); conservative upper bound 36,450 rows (all
  reference records call the same shared SCG).
- Cost/size profile (tier-A minimum set): 88,444.3 s single-core (90.3 % of the
  full reference wall), 725 cells > 60 s, 16 > 120 s, 0 > 300 s, peak RSS
  p95 457 MB / max 478 MB. Full 36,450-row set: 97,962.6 s.
- Modal plan (public rates $0.0473 /core-h and $0.008 /GiB-h, 1 core per cell,
  4 GiB per worker, 1.15 overhead, 0.85 parallel efficiency): minimum set with
  300 s cap ≈ 0.5–2.1 h wall and **≈ $1.7–3.8** total at 16–64 workers; 60 s
  cap would censor 725 cells (not recommended); full 36,450 rows ≈ 0.6 h at
  64 workers, **≈ $1.9–4.3**. These are planning estimates, no Modal run was
  executed.
- **Budget separation:** the reference regeneration must use a separate Modal
  app/environment, artifact prefix and cost line; it must never share or be
  pooled with the formal **8,280-cell** v6 matrix budget. The historical
  add-on is disjoint from that matrix and does not consume its budget.

## 7. Conclusions and limits

- Under the v6 strong-time semantics, **all 3,645 conflict-tier historical
  Proposed verdicts are unchanged** (VER 1,689 → VER, AMB 1,956 → AMB; 0 INC,
  0 timeouts in the primary pass); no historical verdict in the conflict tier
  is invalidated by the repair, while its archived SCG workload counters are
  invalidated (~3,537/3,645 rows change SCG class count; 120/120 sampled
  canonical DBM fingerprints differ).
- Still undecidable / not certified: (a) the 32,805 no-conflict rows are only
  inside the conservative bound, not certified unaffected; (b) COMPLETE_SET
  critical-pair counts are subject to the 60 s cap and to the GH aggregate
  coverage; (c) the MSCG-LP reference has not been regenerated; (d) the
  archived certificate overlap endpoints are unrecoverable.
- Full limitations list: `work/v6_historical_recompute/summary/summary.json`
  (`limitations`), repeated in the GH aggregate summary.

## 8. Reproduce

```bash
# frozen manifest (already committed): verify only
.venv/bin/python scripts/recompute_v6_historical_impact.py verify-manifest \
  --expect-sha e1f6b455285e45cadbec655749b92db3c53b3fdf551dabbd3d7722804bc47a8f

# primary verdict pass (local, final)
.venv/bin/python scripts/recompute_v6_historical_impact.py run --phase first
.venv/bin/python scripts/recompute_v6_historical_impact.py audit

# COMPLETE_SET pass on GitHub Actions (20 shards, 60 s cap)
gh workflow run scalability_v6_historical_complete.yml -f num_shards=20
# download the aggregate artifact -> results/hist_aggregate/summary/summary.json

# local smoke / fallback
.venv/bin/python scripts/recompute_v6_historical_impact.py run --phase complete \
  --shard 0 --num-shards 20 --out results/hist_smoke --limit 3
.venv/bin/python scripts/recompute_v6_historical_impact.py aggregate \
  --shards-dir results/hist_smoke --out results/hist_smoke_aggregate --num-shards 20
```

## 9. Deliverables

| path | contents |
|---|---|
| `protocol/v6_historical_recompute_tasks.json` | frozen 3,645-task manifest (SHA `e1f6b455…`) |
| `scripts/recompute_v6_historical_impact.py` | manifest / sharded run / aggregate / audit CLI |
| `.github/workflows/scalability_v6_historical_complete.yml` | GH Actions validate → shards → aggregate |
| `work/v6_historical_recompute/raw/recompute_first.jsonl` | primary pass raw (3,645 rows) |
| `work/v6_historical_recompute/raw/recompute_complete.jsonl` | pre-GH COMPLETE_SET local snapshot (fallback) |
| `work/v6_historical_recompute/raw/recompute_legacy_complete.jsonl` | archived-semantics replay sample |
| `work/v6_historical_recompute/summary/summary.json` | aggregate local evidence + limitations |
| `work/v6_historical_recompute/summary/drift_detail.csv` | per-row drift table |
| `work/v6_historical_recompute/summary/primary_pass_summary.json` | compact primary-pass export consumed by the GH aggregate job |
| `work/v6_historical_recompute/summary/scg_fingerprint_sample.csv` / `scg_fingerprint_summary.json` | 120-model SCG fingerprint evidence |
| `work/v6_historical_recompute/summary/mscg_lp_reference_plan.json` | reference regeneration plan + Modal budget |

Boundary with the formal matrix: this report covers only the historical
`rmt_fms` add-on. The formal **8,280-cell** v6 matrix is a separate experiment
with its own manifest, workflow, artifacts and budget; nothing here is pooled
with it.
