# v6 differential v2 acceptance report

Decision: **`DIFFERENTIAL_V2_ACCEPTED = GO`** (see
`work/v6_differential_v2_acceptance/acceptance.json`). The v1 protocol is
untouched; its 18 censored pairs stay on record exactly as reported.

## Frozen protocol

`protocol/v6-performance-differential-2.yaml` + JSON mirror, frozen before the
formal run (commit `dd47a96`). Baseline source `10d27726c085…`, optimized source
`215443219c52…`, semantics `v6-strong-time-1`, serialization
`json-fraction-v2`, Python 3.13.

## Layer A — end-to-end equivalence

* Frozen v1 differential: **30/48 pairs conclusive and exactly identical**
  (0 mismatches; 27 in Layer A at 60 s, 3 in Layer B at 300 s). 18 pairs
  censored: **11 baseline-timeout / optimized-completed**, **7 both-timeout**,
  0 optimized-timeout / baseline-completed.
* 288-cell Pilot cross-version agreement (frozen tasks, same runner type, same
  60 s cap; runs `42348140150` vs `42348132809`): **126/126 cells completed on
  both sides agree on verdict, inconclusive reason and every non-runtime
  workload counter** (scg_classes, scg_edges, sync_states, candidate_words,
  temporal_states, trace_records, terminal_pairs, lp_calls) — 0 mismatches.
  76 cells flipped timeout→completed (45 new VER/AMB, 31 INC), 0 reverse
  regressions.
* `OPTIMIZED_COMPLETE_SET_EQUIVALENCE_PASS` is backed by the certificate-set
  comparison in the 30 conclusive differential pairs (canonical φ, σ_N, σ_F,
  overlap) plus the 48 tests.

## Layer B — synchronizer transition equivalence

`work/v6_differential_v2_component/sync_transition_equivalence.json`:
**PASS**.

* 48 cells total: **45 checked + 3 censored**, coverage complete by rule
  (checked + censored = 48; every censored cell carries a wall-budget reason).
* 31 374 reachable synchronizer states × every observable label =
  **726 665 transition comparisons** between the memoised result and a fresh
  recomputation without the memo cache; `fresh_equals_memo = 627 833`,
  **mismatch = 0**. The remaining pairs are depth-limit states that the
  production BFS never expands (no memo exists; counted explicitly).
* Fresh-transition BFS reconstruction of the ordered states, edge multiset and
  candidate-word list matches the production automaton for **45/45** checked
  cells (`graph_reconstruction_pass = true`).
* Independent no-memo full-automaton comparison: **39/45 match**, 6 skipped by
  a 60 s budget (listed individually), 0 mismatches.
* The 3 censored cells (F06/F09/F12 `L_high_complete`) cannot have their
  synchronizer constructed within 900 s in either version; they are also
  censored in the frozen v1 differential at 300 s. They are **not** counted as
  passes.

## Layer C — per-optimization component audit

`work/v6_differential_v2_component/component_equivalence.json`: **PASS**
(coverage minima pre-registered; per-component `coverage` object).

| Component | Evidence | Mismatch |
|---|---|---|
| SCG adjacency index | 24/24 models, 60 767 classes vs original edge-list projection (order, multiplicity, labels, empty successors, truncated graphs) | 0 |
| Marking cache | 12/12 families, 25 words, 1 891 nodes vs a cache-free reference implementation | 0 |
| TraceRecord dedupe | 25 samples, 85 710 critical pairs, 1 354 multi-record nodes; dedupe is defensively inert because (node, sigma) is deterministic — duplicates never arise | 0 |
| DBM / Fraction boundary | 13 cases: half-integers, arbitrary denominators, negatives, zero, infinity, boundary equality, empty zone, canonicalization, persistent projection, newly-enabled reset | 0 |
| Independent oracle | Fraction timed-semantics oracle 15 tests + 12-family semantic validation | 0 |

Full test suite: **67 tests OK** (48 original + 19 added; no original assertion
relaxed).

## Gates

```text
DIFFERENTIAL_V2_COMPONENT_PASS     = true   (Layer C, 5/5 components)
DIFFERENTIAL_V2_COMPLETE_CASES_PASS= true   (30/30 conclusive pairs, 126/126 Pilot pairs, 45+3/48 transition cells)
DIFFERENTIAL_V2_ORACLE_PASS        = true   (Fraction oracle + 12 families)
DIFFERENTIAL_V2_CENSORING_ACCOUNTED= true   (18 v1 censored + 3 transition censored, all with reasons, none removed from the denominator)
DIFFERENTIAL_V2_NO_OBSERVED_MISMATCH=true
DIFFERENTIAL_V2_ACCEPTED           = GO
```

## Limits (kept explicit)

1. Three heavy `COMPLETE_SET` cells have no full synchronizer automaton in
   either version; their evidence is limited to the SCG stage (exact SCG
   fingerprints) and they are censored, not passed.
2. The no-memo full-automaton comparison covers 39/45 cells; 6 heavy cells are
   skipped by budget.
3. Component expansion checks use small/medium representatives (F06/F08/F12
   with reduced `n_parts`/`ops`); parameters are recorded in the evidence JSON.
4. Dedupe never sees a duplicate sigma in the audited samples; the proof is
   that disabling it changes neither the distinct record set nor the
   COMPLETE_SET critical-pair set on those samples.

## Consequence

The formal 8,280-cell matrix may proceed **once the Modal environment, budget
and recovery gates pass**; this report does not itself authorise the run.
