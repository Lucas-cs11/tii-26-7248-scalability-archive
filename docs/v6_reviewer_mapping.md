# Reviewer-issue mapping — v6 evidence

| Issue | v6 resolution | Evidence |
|---|---|---|
| Timeout treated as zero work | timeouts are right-censored at the cap and reported as their own denominator | `07_statistical_analysis/statistics.json`, `failure_stage_distribution` |
| INC conflated with timeout | INC is completed-but-inconclusive with `inconclusive_reason` | audit + statistics |
| Missing provenance | every record carries source SHA, image-spec SHA, Modal invocation id, model hash, task id | `04_raw_8280/`, `06_independent_audit/` |
| Selective retention | the frozen manifest has 8280 tasks; the audit checks uniqueness and missing/duplicates | `01_frozen_protocol/`, `06_independent_audit/` |
| Budget transparency | expected 47594.4 s completed wall + 2874 capped cells; Modal billing audit included | `03_modal_cost_and_resource_audit/` |
| Semantic equivalence of the optimised verifier | differential v2 accepted (`DIFFERENTIAL_V2_ACCEPTED = GO`), SCG fingerprints exact | `10_reproducibility/` |
