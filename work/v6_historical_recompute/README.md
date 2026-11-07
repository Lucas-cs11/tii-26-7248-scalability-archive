# v6 historical recompute - 555 conflict-tier bounded problems / 3,645 evaluations

Execution-side deliverable that follows `work/v6_historical_impact/`. It
recomputes, under the v6 (strong-time corrected) semantics, exactly the
archived Proposed evaluations that the static urgency-conflict screen exposes
to the shared SCG repair, and audits the verdict drift against the archived
verdicts.

The archived 36,450-evaluation `rmt_fms` baseline and this recompute are an
add-on dataset: they are disjoint from the planned formal 8,280-cell v6 matrix
and never share its budget, protocol or results.

## Reproduce

```bash
.venv/bin/python scripts/recompute_v6_historical_impact.py manifest
.venv/bin/python scripts/recompute_v6_historical_impact.py run --phase first
.venv/bin/python scripts/recompute_v6_historical_impact.py run --phase complete --limit 700
.venv/bin/python scripts/recompute_v6_historical_impact.py run --phase legacy_complete
.venv/bin/python scripts/recompute_v6_historical_impact.py audit
```

All runs are local; no Modal access, no git commit/push.

## Frozen scope

- `protocol/v6_historical_recompute_tasks.json` - 3,645 tasks / 555 bounded
  problems, SHA-256 in `summary/frozen_manifest.sha256`. The manifest is
  byte-deterministic (no timestamps) and is written before any recomputation.
- Per-task wall cap: 60 s, enforced by SIGKILL on a dedicated forked child.
- Parallelism: at most 8 process-pool workers.
- Resume: by `task_id` from the raw JSONL; finished tasks are never re-run.

## Outputs

| Path | Contents |
|---|---|
| `raw/recompute_first.jsonl` | primary pass: `complete_set=False` (archived Proposed protocol), all 3,645 tasks |
| `raw/recompute_complete.jsonl` | auxiliary pass: `complete_set=True` on archived-AMB rows, for the critical-pair count and certificate overlap endpoints |
| `raw/recompute_legacy_complete.jsonl` | archived-semantics replay sample (pre-repair `_compute_successor` from git history) |
| `checkpoints/<phase>_progress.json` | progress + coverage checkpoints |
| `logs/<phase>.log` | run logs |
| `summary/frozen_manifest.sha256` | frozen-scope evidence |
| `summary/run_<phase>.json` | per-phase run summaries |
| `summary/drift_detail.csv` | one row per task: archived vs recomputed verdict, status, SCG counters, critical pairs, overlap endpoints |
| `summary/summary.json` | aggregate drift matrix, coverage, critical-pair and time-domain analysis, limitations |
| `summary/scg_fingerprint_sample.csv` / `_summary.json` | archived-semantics vs v6 canonical SCG fingerprints for 120 sampled models |
| `summary/mscg_lp_reference_plan.json` | MSCG-LP reference regeneration plan and Modal budget (planning only, not executed) |

## Raw record schema (one JSON object per line)

`task_id, phase, evaluation_id, historical_model_hash, v6_model_hash, L, H,
mode, complete_set, semantics, execution_status, verdict, inconclusive_reason,
enumeration_complete, certificates_count, overlap_min, overlap_max,
overlap_width, overlap_inside_window, counters (full verifier stats incl.
scg/sync/runtime phases), stage, stage_history, wall_s, peak_rss_mb,
generated_L, generated_H, model_LH_matches_archived, regenerated_v6_model_hash,
regenerated_structure_hash, error`

`execution_status` is one of `COMPLETED`, `TIMEOUT_WALL_CLOCK`,
`TIMEOUT_HARD_KILL`, `MODEL_IDENTITY_MISMATCH`, `ERROR`. A timed-out task never
carries a verdict; its last completed verifier stage and the counters observed
up to the kill are preserved (`stage`, `counters`).

## GitHub Actions COMPLETE_SET second pass (current execution path)

The full COMPLETE_SET pass (1,956 archived-AMB rows) runs on GitHub Actions,
not on the local host: `.github/workflows/scalability_v6_historical_complete.yml`.

```bash
# 1. sharded run (workflow does this in a 20-way matrix)
.venv/bin/python scripts/recompute_v6_historical_impact.py run \
  --phase complete --shard 0 --num-shards 20 --out results/hist
#    -> results/hist/raw/recompute_complete_shard_0.jsonl
#    -> results/hist/run_shard_0.json

# 2. aggregate all shards (workflow aggregate job)
.venv/bin/python scripts/recompute_v6_historical_impact.py aggregate \
  --shards-dir artifacts --out results/hist_aggregate --num-shards 20 \
  --primary-summary work/v6_historical_recompute/summary/primary_pass_summary.json
#    -> results/hist_aggregate/summary/summary.json  (snapshot_status FINAL/FINAL_PARTIAL)
#    -> results/hist_aggregate/summary/drift_detail.csv

# 3. frozen-manifest validation (workflow validate job)
.venv/bin/python scripts/recompute_v6_historical_impact.py verify-manifest \
  --expect-sha e1f6b455285e45cadbec655749b92db3c53b3fdf551dabbd3d7722804bc47a8f
```

The aggregate job needs the compact primary-pass export
`summary/primary_pass_summary.json` committed (it carries the 3,645-row
primary verdict results, the 1,689 VER critical-pair rows, the legacy replay
and the SCG fingerprint evidence), because the GH shards only cover the AMB
COMPLETE_SET pass.

The local `raw/recompute_complete.jsonl` (431 / 1,956 recorded before the move)
and `summary/summary.json` are kept as a **pre-GH snapshot / fallback**; the
authoritative COMPLETE_SET numbers are those of the GH aggregate artifact.
