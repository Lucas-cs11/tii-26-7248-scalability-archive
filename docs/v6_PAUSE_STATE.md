# v6 execution state — safely paused on user request

Paused at 2026-11-07 18:0x CST. **No Modal containers are running; no Modal app
is active; no further paid work is in flight.** Resume from this file.

## Modal spend so far

* Authorized cap: **$10.00** (user confirmed; `work/v6_modal_budget/budget.json`).
* Metered spend attributed to this work: **$0.02** (`modal billing summary`
  delta vs the $23.58024702 month baseline); remaining ≈ $9.98.
* All apps stopped (`ap-GrOHh6SIqc3N8IcdPpT2Nm` and earlier attempts);
  `modal container list` is empty.

## What is complete

| Stage | State | Evidence |
|---|---|---|
| 1 freeze + report corrections | done | `docs/v6_optimization_differential_report.md` (corrected), commit `dd47a96` |
| 2 archive integrity | done | nested `SHA256SUMS` included and verified in the archive builder |
| 3 differential v2 protocol frozen | done | `protocol/v6-performance-differential-2.yaml/.json` (`dd47a96`) |
| 3 Layer B transition equivalence | **PASS** | 45 checked + 3 censored (with reasons), 726 665 transitions, 0 mismatch, graph reconstruction 45/45 |
| 3 Layer C component audit | **PASS** | SCG index 60 767 classes; marking cache 12 families; trace dedupe 85 710 pairs; DBM/Fraction 13 cases; oracle 15+1; 67 tests OK |
| 4 differential v2 acceptance | **GO** | `docs/v6_differential_v2_acceptance_report.md`, `work/v6_differential_v2_acceptance/acceptance.json` |
| 5 Modal infrastructure | built + locally simulated | `modal_app/`, `scripts/run_v6_modal.py`, `docs/v6_modal_environment.md` |
| 6 Modal environment pilot | **blocked by an orchestrator bug, see below** | one real shard validated end-to-end (12/12 tasks) |
| 7 formal 8,280 grid | generated + validated, **not frozen** | `protocol/v6_formal_manifest.json`, `protocol/v6_formal_tasks/` (120×69), validator PASS |
| Figure 6 pipeline | built + Pilot self-check | `scripts/analyze_v6_formal_results.py`, `scripts/plot_figure6_v6.py`, `docs/v6_figure6_spec.md` |
| historical 3,645 recompute | running locally (no cloud cost), being checkpointed | `work/v6_historical_recompute/` |

## Blocking issue found during the first full pilot (fix on resume)

Three Modal bugs were found and two are fixed:

1. **Fixed (`50d64b1`)**: `Volume.commit()` was called from the local
   orchestrator; Modal 1.6.3 only allows it inside a container.
2. **Fixed (`d5aad4e`)**: the container mounts the entry module at `/root`, so
   the shard worker was looked up at `/root/v6_shard_worker.py` instead of the
   image copy `/root/v6repo/modal_app/v6_shard_worker.py`; every shard
   crash-looped. A call watchdog was added as well.
3. **Open**: `--volume-prefix scalability_v6_pilot` only changes the *local*
   orchestrator paths. The function in the container reads the module-level
   prefix (`scalability_v6_formal`), so it cannot find
   `run_metadata/run_pilot-<sha>-<ts>.json` → infrastructure retry loop.
   Fix: pass volume name and prefix (or the run-metadata path) as explicit
   Function arguments and have the worker use them; then validate with a
   2-shard pilot before the 6-shard pilot.

Validated evidence from the single real shard (prefix
`scalability_v6_formal`): 12/12 tasks executed, 9 completed (2 VER / 2 AMB /
5 INC), 3 `TIMEOUT_WALL_CLOCK`, 0 errors, 439 s wall; records and ledger on the
Volume with commit SHA, image-spec SHA, Modal invocation id and pinned
`numpy 2.3.4 / scipy 1.16.2` provenance (`work/v6_modal_smoke_shard_000/`).

## Resume checklist

```bash
cd /tmp/v6work
export TIIPY="/Users/lucas/Library/Mobile Documents/com~apple~CloudDocs/WorkingFiles/在投文章/TII_TPN/02_experiments/firing_interval_adjustment/.venv/bin/python"
# 1. fix the volume-prefix plumbing, run the local simulation and a 2-shard pilot
"$TIIPY" scripts/run_v6_modal.py simulate --workdir /tmp/v6_sim_resume
"$TIIPY" scripts/run_v6_modal.py pilot --confirm-budget --max-parallel 2 \
    --pilot-manifest protocol/v6_modal_pilot_tasks.json --pilot-shards 2
# 2. full 6-shard pilot + independent audit + reference comparison
"$TIIPY" scripts/run_v6_modal.py pilot --confirm-budget --max-parallel 8 \
    --pilot-manifest protocol/v6_modal_pilot_tasks.json
"$TIIPY" scripts/audit_v6_modal_results.py --download-dir work/v6_modal_pilot_<run_id> ...
# 3. real crash/resume test (low function timeout, then resume)
# 4. formal gates: scripts/evaluate_v6_formal_gates.py
# 5. freeze protocol: scripts/build_v6_frozen_protocol.py  (writes scalability_v6_frozen_protocol.yaml)
# 6. formal run: 120 shards x 69 tasks, max-parallel 20, budget guard at $9.5
#    expected 43.24 CPU-h ≈ $3.43; worst 138 CPU-h ≈ $10.94
# 7. download + independent audit + Figure 6 formal + final ZIP
```

## Planned budget for the formal run (unchanged)

Expected **43.24 CPU-hours ≈ $3.43**, worst case 138 CPU-hours ≈ **$10.94**
(above the $10 cap → the guard stops spawning new shards at $9.5 and reports).
Expected wall clock at `--max-parallel 20` ≈ **2.2 h**; worst ≈ 6.9 h.
