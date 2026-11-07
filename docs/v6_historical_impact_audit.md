# v6 impact audit of the 36,450 historical proposed evaluations

## What was audited

The v5 manuscript's background baseline is 36,450 archived *Proposed bounded
evaluations* (v4-era `rmt_fms` full-scale dataset, `protocol_version = 4`),
delivered as 255 raw shard CSVs and 5,589 distinct bounded problems. The
question is not "are these numbers reproducible" but "does the v6 semantic
repair invalidate them, and if so, how much and at what cost".

Reproduction (read-only on the archived artifacts):

```bash
cd <repository root>
.venv/bin/python scripts/audit_v6_historical_impact.py
# outputs: work/v6_historical_impact/
```

## 1. Recovery

36,450/36,450 rows recovered from 255 shard CSVs (143 rows × 254 shards + 128
rows × 1), 5,589 distinct historical model identities and 5,589 distinct
bounded problems. Proposed outcomes are VER 20,412 / AMB 16,038, and every
identity matches the archived MSCG-LP reference CSV exactly. The recovery
reproduces the archived v5 recovery CSV row-by-row with zero differences. All
255 shard CSVs and the reference CSV are SHA-256 fingerprinted in
`source_artifacts_manifest.csv`.

## 2. Generator fix: zero row-level impact on this baseline

The v6 generator repair (immediate route choice via
`t_choose_skip`/`t_choose_inspect`/`t_choose_continue`/`t_choose_rework`) does
not touch this baseline: all 36,450 rows were generated with
`n_configs = 2, inspection_locations = 0`, so the repaired branch never
executes. Regenerating every historical identity with the shared base generator
at the v6 HEAD gives **36,450 matches, 0 divergent, 0 unclassifiable**. A
separate mechanism check on the 12 v6 families (2 seeds each) shows 12/12
family models *do* change under the repair, so the generator fix is real — it
simply does not reach this dataset.

## 3. Shared SCG strong-time fix: the only impact channel

The repaired `src/scg/builder.py::_compute_successor()` changes successors
wherever a selected transition can be outrun by a stronger-time competitor.
The audit therefore cannot certify the baseline: the conservative upper bound
is **all 36,450 rows / 5,589 bounded problems need recomputation**.

A structural screen on the untimed marking graph isolates the
evidence-driven minimum set: a static urgency conflict (`t.earliest >
u.latest` for two simultaneously enabled transitions) exists in **555 bounded
problems / 3,645 rows**; the other 5,034 problems / 32,805 rows pass the screen
but are **not certified unaffected**.

A pre-registered, outcome-blind 200-cell sample (100 conflict-tier, 100
no-conflict-tier; models ≤31 transitions; ordering by
`sha256(rule_version|model_hash|L|H)`; cells over 10 s censored — none were)
found:

| Tier | SCG fingerprint identical | SCG fingerprint different | Verdict drift |
|---|---:|---:|---:|
| A: static urgency conflict (100) | 0 | 100 | 0 |
| B: no static urgency conflict (100) | 100 | 0 | 0 |

Conflict-tier SCG size changed from mean 286.57 classes / 534.2 edges to
143.51 / 240.77 (94/100 became smaller), so the archived SCG workload counters
for those 555 problems are not reusable. Verdict drift is 0/200, but this is
small-model sample evidence (Wilson 95 % per stratum [0.963, 1.0]) and is **not**
a certificate for the whole baseline.

## 4. Cost to recompute

| Recompute target | Single pass | 3 repeats |
|---|---:|---:|
| Full baseline (36,450 rows) | 5,130.7 s ≈ 1.43 h | 15,392.1 s ≈ 4.28 h |
| Minimum conflict set (3,645 rows) | 4,685.6 s ≈ 1.30 h | 14,056.7 s ≈ 3.90 h |
| MSCG-LP reference (independent re-run) | 97,962.6 s ≈ 27.21 h | – |

Costs use the historical GitHub Actions wall times with an explicit local
correction ratio (this host projects ≈1,344 s for a full pass, range
694–4,937 s under host load); 725 reference cells exceed the 60 s cell cap and
would need a larger cap or explicit censoring. These are planning figures, not
measured runs.

## 5. MSCG-LP reference is not independent

`src/baselines/mscg_lp_complete.py` (and the `adapted` variant) imports
`build_scg` from `src/scg/builder.py` (line 39) and enumerates bounded complete
executions from that class graph (line 181). The file is unchanged since the v4
commit, so the archived reference CSV was produced with the **pre-repair
successor semantics**. Only the LP confirmation step
(`src/exact_confirmation/lp_solver.py`) is SCG-independent; the path set it
confirms is not. There is no full independent-oracle validation of the
reference under v6 — that is missing evidence, not a passed check.

## 6. Documentation discrepancies found

* The baseline is a v4-era `rmt_fms` dataset, not the v5/v6 "12 synthetic TPN
  families" matrix; `generate_v6_model` rejects `rmt_fms`, so the audit uses the
  shared base generator `generate_rmt_fms` at v6 HEAD and runs the v6-family
  mechanism check separately.
* The recovered Proposed CSV has no `mode` column; the documented default
  `FIRST_CERTIFICATE` is assumed (consistent with every archived reference
  record).
* The archived reference CSV lives under
  `work/v5_final_build_rebuilt/TII_Scalability_Benchmark_v5/06_fullscale_baseline/original_reference_records/`
  (SHA-256 `a854f364…`), not under `work/v5_baseline_audit/`.
* The 36,450 rows are evaluation-weighted (repeated evaluations of 5,589
  bounded problems); per-row wall sums are evaluation-weighted.
* Source run IDs are `41781437233` (main) and `41781103322`/`41789970846`.

## Status

`FRAMEWORK_COMPLETE_PARTIAL_EVIDENCE`. The framework and evidence are complete
enough to bound the problem, but the audit does **not** certify that historical
Proposed verdicts remain valid under v6. The next step, if the historic
baseline is to be reused in the paper, is to re-run the 3,645-row minimum set
(or the conservative 36,450 rows) and the MSCG-LP reference under the repaired
semantics. Do not reuse the archived reference values as an independent
baseline without that re-run.
