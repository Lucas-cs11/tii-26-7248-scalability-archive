# v6 execution — staged status

Updated after resuming from the safe pause. All non-Modal compute is now routed
to GitHub Actions; the formal matrix runs only on Modal.

## Gates and evidence

| Gate | State | Evidence |
|---|---|---|
| SCG strong-time / soundness / DBM / 12-family | PASS | `protocol/v6_semantics_gate.json` |
| Differential v2 (Layer A/B/C) | **GO** | `docs/v6_differential_v2_acceptance_report.md`, `work/v6_differential_v2_acceptance/acceptance.json` |
| Pilot performance audit | PASS | `work/v6_baseline_pilot_audit/`, `work/v6_optimized_pilot_audit/` |
| Formal grid readiness | PASS | 8,280 tasks / 120×69 / 7,380 bounded problems, `work/v6_formal_protocol_validation/validation.json` |
| Modal environment | in progress | pilot + reference comparison (Modal, background) |
| Modal checkpoint recovery | in progress | crash/resume test (Modal, background) |
| Modal budget policy | PASS pending spend check | cap $10, spent ≈ $0.02, guard at $9.50 |
| Formal protocol frozen | **not yet** | waits for the three Modal gates |

## What changed in this round

* Differential v2 accepted: Layer B 45 checked + 3 censored cells, 726,665
  transition comparisons, 0 mismatches, automaton reconstruction 45/45; Layer C
  five components pass; 67 tests OK.
* Modal SDK misuse 4/4 found: `Volume.commit()` locally, container module
  resolution, volume-prefix plumbing — fixed; local `Volume.reload()` — being
  fixed in the pilot job.
* A real 2-shard Modal pilot did execute end-to-end (12+11 records and ledgers on
  the Volume) before the orchestrator's post-run `reload()` crashed; that data is
  being audited and compared with the frozen reference verdicts.
* Historical 3,645-row recompute: primary verdict pass complete with **0 verdict
  drift**; the COMPLETE_SET second pass is being moved from local computation to
  a sharded GitHub Actions workflow (user requirement).
* Post-formal independent audit + statistics + Figure 6 will also run on GitHub
  Actions: the Modal raw records are published as a GitHub release asset and
  `.github/workflows/scalability_v6_formal_audit.yml` downloads, audits and plots
  them (no computation on this machine).

## Modal cost and plan

* Authorized cap $10; metered so far ≈ $0.02.
* Formal matrix: 120 shards × 69 tasks, 60 s cell cap, `max-parallel 20`,
  expected 43.24 CPU-hours ≈ **$3.43**, worst 138 CPU-hours ≈ $10.94; the guard
  stops spawning at $9.50 and records `BUDGET_STOPPED` rather than overspending.
* Expected wall clock ≈ 2.2 h.

## Exact next steps (automatic)

1. Modal pilot PASS + recovery PASS → `scripts/evaluate_v6_formal_gates.py` GO.
2. Regenerate the shard manifest, freeze
   `protocol/scalability_v6_frozen_protocol.yaml` (`scripts/build_v6_frozen_protocol.py`).
3. Clean the smoke/pilot records from the formal volume prefix, launch
   `scripts/run_v6_modal.py run --confirm-budget --max-parallel 20`.
4. Download raw records, publish them as a GitHub release asset, dispatch the
   audit workflow, collect the audited statistics and Figure 6.
5. Assemble `TII_Scalability_Benchmark_v6_Modal_FullScale_Results.zip` with
   provenance, environment, cost audit, raw 8,280, audit, figures, paper
   material and the SHA-256 manifest.

## Outstanding risks

* Modal network proxy returned HTTP 503 twice; each step retries once and then
  reports rather than looping.
* 3 heavy `COMPLETE_SET` cells have no full synchronizer automaton in either
  version; they are censored in v2 and will be reported as
  `TIMEOUT_WALL_CLOCK`/`INC` in the formal matrix, never as verdicts.
* Historical critical-pair counts use a different enumeration basis from the
  archived reference (documented in `docs/v6_historical_recompute_report.md`).
