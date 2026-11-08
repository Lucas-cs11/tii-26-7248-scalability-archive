# Final acceptance report — v6 Modal full-scale (8,280 cells)

**Decision: READY_FOR_IEEE_TII**

## Execution
- run id `run-5038d2a9-20261107T113535Z`, source commit `5038d2a957eafba43bb978cdb9edea00a5c77341`,
  image spec `f64f51657a4ec43dd7946ebb931fefca7e78524017d7dd34e978c28da183fee7`, Modal CPU 1 core / 4096 MiB,
  60 s per-cell cap, `max_parallel` 20,
  volume `tii-v6-scalability` prefix `scalability_v6_formal`.
- raw records: **8280** / 8,280 over 120 shards;
  run manifest declares 8280 tasks in 120 shards.

## Verdict / status accounting
- COMPLETED 5406, TIMEOUT_WALL_CLOCK 2874,
  ERROR 0 (right-censored; never imputed).
- VER 1975, AMB 2782, INC 649 (INC is completed-but-inconclusive).

## Formal gates
```text
ALL_12_FAMILIES_SEMANTIC_PASS = true
DBM_NORMALIZATION_PASS = true
DIFFERENTIAL_V2_ACCEPTED = true
FORMAL_PROTOCOL_READY = true
MODAL_BUDGET_POLICY_PASS = true
MODAL_CHECKPOINT_RECOVERY_PASS = true
MODAL_ENVIRONMENT_PASS = true
PILOT_MEMORY_PASS = true
PILOT_PERFORMANCE_AUDITED = true
SCG_SOUNDNESS_AND_COMPLETENESS_PASS = true
SCG_STRONG_TIME_PASS = true
```

## Independent audit (GitHub Actions)
- PASS = **True**; audited tasks 8280,
  unique task ids 8280, shards 120,
  technical failures 0, findings [].

## Cost and incidents
- Modal metered delta for pilot + formal: $5.53001
  (cap $10, guard $9.50); incidents:
  - local API proxy HTTP 503 on 2026-11-07T22:27+08:00 (data loss 0)
  - budget guard false-positive projection at 3/120 shards (data loss 0)
  - shard 044 ConnectionError Deadline exceeded (data loss 0)

## Deliverable contents
`00_provenance`, `01_frozen_protocol`, `02_modal_environment`,
`03_modal_cost_and_resource_audit`, `04_raw_8280`, `05_checkpoint_and_recovery`,
`06_independent_audit`, `07_statistical_analysis`, `08_figures`, `09_paper_material`,
`10_reproducibility`, `MANIFEST.sha256`.

Machine-readable source: `00_provenance/run_provenance.json`,
`06_independent_audit/audit.json`, `07_statistical_analysis/statistics.json`.
