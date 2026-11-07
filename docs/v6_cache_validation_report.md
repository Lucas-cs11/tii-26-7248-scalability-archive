# v6 cache-validation audit and budget gate

## Scope

This report covers frozen protocol `v6-cache-validation-1`, the one frozen
answer to "does same-model SCG reuse pay for itself without changing anything
semantic?" It is *not* the formal 8,280-cell experiment. The formal protocol is
not frozen and the formal run is not started.

## Integrity

GitHub Actions run `42339602408` executed commit
`d2e83bf8e5876646e9e8c37889bad083d14a109d` on 12 standard `ubuntu-22.04`
shards, `max-parallel: 6`, plus one protocol-validation job. All 13 jobs
succeeded. The 13 artifacts were downloaded to
`work/v6_cache_validation_42339602408/` (820 KB) and audited from the raw
`raw/shard_XXX.jsonl` files only, with an independent script that recomputes
every number (`scripts/audit_v6_cache_validation_results.py`).

The audit found: 96/96 unique final records, 12/12 shards, no missing, duplicate
or unexpected task, no `ERROR`, every frozen field (task/configuration/bounded
problem identity, `L`, `H`, `mode`, params, model/structure hash, run ID,
numeric job ID, source SHA) matching the frozen CSV, every `raw_record_hash`
recomputing, and Peak RSS present for all completed cells.

## Cache behaviour

* 60 `miss` / 36 `hit`: exactly the pre-registered expectation (5/3 per shard).
* 36/36 hits had an earlier same-shard record with the same `scg_cache_key` in
  `miss` state; every key was recomputed from the recorded key material and
  matched; every provenance block satisfied the frozen schema.
* 0 store failures, 0 degraded cells, 0 disabled cells, 0 integrity errors, and
  `CACHE_INTEGRITY_PASS = true`.
* 18 cells were killed during SCG construction before any provenance existed.
  They are reported as censored cache metadata, not as hits or misses of record.

Per-family outcome (`n`, completed, timeout, hits, median SCG build on miss,
median SCG load on hit):

| Family | n | Completed | Timeout | Hits | SCG build P50 | SCG load P50 |
|---|---:|---:|---:|---:|---:|---:|
| F01 sequential sparse | 8 | 5 | 3 | 3 | 0.060 s | 0.0012 s |
| F02 sequential dense | 8 | 6 | 2 | 3 | 1.955 s | 0.0010 s |
| F03 deep inspection | 8 | 5 | 3 | 3 | 0.148 s | 0.0023 s |
| F04 alternative routes | 8 | 5 | 3 | 3 | 0.042 s | 0.0034 s |
| F05 routes+inspections | 8 | 3 | 5 | 3 | 0.075 s | 0.0037 s |
| F06 parallel parts | 8 | 1 | 7 | 3 | 8.958 s | 2.9436 s |
| F07 shared contention | 8 | 1 | 7 | 3 | 3.182 s | 0.3333 s |
| F08 mixed visibility | 8 | 2 | 6 | 3 | 6.225 s | 0.1337 s |
| F09 deep fault stages | 8 | 1 | 7 | 3 | 2.081 s | 0.2027 s |
| F10 bounded rework | 8 | 4 | 4 | 3 | 0.093 s | 0.0016 s |
| F11 resource cycle | 8 | 4 | 4 | 3 | 2.422 s | 0.0612 s |
| F12 sparse cycle | 8 | 1 | 7 | 3 | 1.479 s | 0.1262 s |

Overall: 38 `COMPLETED`, 58 `TIMEOUT_WALL_CLOCK` (60.4 % right-censored),
0 `ERROR`, with 25 `VER`, 9 `AMB`, 4 `INC` among the completed cells.
Peak RSS P95 is 254.0 MiB and the maximum is 271.4 MiB, i.e. inside the Pilot's
memory envelope.

## Differential evidence

Every pre-registered r0 (cold, `miss`) / r1 (cached, `hit`) pair was compared on
verdict, inconclusive reason, enumeration completeness, certificate count, model
and structure hash, execution status, failure stage, and every non-runtime
counter. There were **0 mismatches**.

But only **7 of 12** pairs could be compared at all: in F06, F07, F08, F09 and
F12 both sides hit the 60 s wall clock (in F11 the cold side timed out while the
cached side completed), so the pair has no conclusive outcome to compare. Under
the pre-registered requirement that at least 10/12 pairs be complete and
identical, the differential gate is therefore **false**:

```text
CACHE_DIFFERENTIAL_PASS = false   (7/12 complete pairs, 0 mismatches)
```

This is a limitation of validating semantic equivalence at a 60 s cell cap on
heavy families, not an observed semantic difference. The unit-level differential
tests (cold vs in-memory prebuilt vs serialized load, both analysis modes,
`tests/test_scg_cache.py`) pass on the validated families and are part of the
48-test suite; they do not substitute for the missing end-to-end pairs.

## Budget

The Pilot budget model (138 h worst-case compute) assumed every cell could use
the full 60 s. A censoring-aware expectation must use the observed timeout
fraction per bucket, `E = p_timeout · 60 s + (1 - p_timeout) · wall_P50(completed)`.

Measured bucket values (right-censored, replicate=1 excluded): structural
`p_timeout = 0.417`, `wall_P50 = 0.302 s`; WIP `0.694`, `28.608 s`; L-sweep
`0.500`, `0.302 s`; difficult `0.917`, `50.941 s`.

| Scenario | Compute | Wall at 20-way concurrency |
|---|---:|---:|
| Censoring-aware expectation | 5 058 min (84.3 h) | 4.2 h |
| Worst case (all cells 60 s) | 8 280 min (138 h) | 6.9 h |

Against a 6 h per-job cap this is 15 jobs expected and 23 jobs worst-case, i.e.
one to two waves of 20 concurrent standard runners. Total cache saving in the
L-sweep bucket is about **32 s across the whole formal matrix (< 0.01 %)**: the
large construction times belong to cells whose synchronizer work exceeds the cap
anyway, and completed L-sweep cells are cheap.

The audit tool's `measured_cache` scenario (1 145 min) is *not* used for the
gate: it replaces the WIP bucket's measured wall P50 with a sum of separately
computed medians (overhead 2.95 s + build 2.42 s versus a wall P50 of 28.6 s),
which is not a conservative substitution. The censoring-aware table above is the
one this report stands behind.

Storage: mean stored cache entry 6.08 MiB, maximum 30.5 MiB, per-shard peak
70 MiB in this run. Entries are per-job and ephemeral, so an aggregate
"all entries at once" figure is not a runner requirement.

## Gate decision

```text
SCG_STRONG_TIME_PASS              = true    (semantics gate, unchanged)
SCG_SOUNDNESS_AND_COMPLETENESS_PASS = true  (semantics gate, unchanged)
DBM_NORMALIZATION_PASS            = true    (semantics gate, unchanged)
ALL_12_FAMILIES_SEMANTIC_PASS     = true    (semantics gate, unchanged)
CACHE_INTEGRITY_PASS              = true
CACHE_DIFFERENTIAL_PASS           = false   (7/12 complete pairs, 0 mismatches)
PILOT_BUDGET_AND_MEMORY_PASS      = false   (60.4 % censoring; 84.3 h expected,
                                             138 h worst; cache saving < 0.01 %)
```

`protocol/scalability_v6_frozen_protocol.yaml` is therefore **not created**, the
8 280-cell immutable task list is **not frozen**, and no formal run is started.
This is a deliberate stop before spending the formal budget, exactly as the
handoff requires. The cache is retained because it is correct, instrumented and
cheap in memory; it is not the bottleneck fix.

## What would unblock the formal matrix

1. A semantics-preserving optimization of the *dominant* stage (synchronizer /
   temporal expansion / LP), with a proof-carrying rule and differential
   evidence — this is the only path that changes the 60.4 % censoring.
2. An explicit user decision on the resource envelope: ~84 h expected / 138 h
   worst-case compute on free GitHub public-repo runners (about 4-7 h wall at 20
   concurrency), or the equivalent on Modal (current published CPU rate
   $0.0473/core-hour and memory $0.008/GiB-hour imply roughly $4-6 for 84 h at
   one core and ≤1 GiB per cell, before any egress; this month's $23.58 metered
   usage was fully covered by credits, but the remaining credit balance and a
   workspace budget cap cannot be read from the CLI and must be confirmed in
   the Modal console).
3. A re-designed differential validation for heavy families that can compare
   censored cells (new pre-registered protocol version; the frozen
   `v6-cache-validation-1` is not edited after seeing its results).
