# Reviewer-issue mapping — v5 evidence

| Issue | v5 resolution | Evidence in final archive |
|---|---|---|
| Unreachable or decorative structure | Every declared model has a constructive completion/fault/route/cycle witness; formal semantic validation passed. | `01_generator_and_models/semantic_validation/` |
| WIP did not represent real concurrency | All B models have at least three parts; actual releases witness WIP=1/2/3, with paired static hashes. | `03_wip_scaling/`, `09_independent_validation/` |
| Early L plateau / too few models | C has 300 fixed TPNs, 15 consecutive levels each, all 12 families, and a preregistered complete subset. | `04_dense_L_sweep/` |
| L, H, or topology drifted | C keeps model hash and H fixed within each sweep; audit rejects drift. | frozen task list and integrity validation |
| Timeout incorrectly plotted as zero work | TIMEOUT has no verdict/runtime; partial counters remain marked partial and are not treated as complete workload. | raw records and audit script |
| INC conflated with timeout | `INC_STATE_CLASS_LIMIT` and `TIMEOUT_WALL_CLOCK` use distinct statuses and analyses. | merged statistics and validation |
| Insufficient provenance | Each record includes source commit, numeric job ID, run, shard, artifact, configuration hash, and raw hash. | `07_github_actions/`, raw records |
| Incomplete historical baseline | 255 historical proposed artifacts recover all 36,450 rows and match reference identities exactly. | `06_fullscale_baseline/` |
| Results selectively retained | Frozen family/seed grids, all declared records, timeouts, INC, and failed-attempt history are retained. | protocol, selection manifest, raw artifacts |
