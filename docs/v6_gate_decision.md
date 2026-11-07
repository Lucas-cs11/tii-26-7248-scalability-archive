# v6 gate decision: NO-GO for the formal 8,280-cell matrix

Decision date: 2026-11-07. Optimised commit `2154432` (branch
`scalability-v6-synchronizer-optimization`). Gate criteria are declared in
`protocol/v6_gate_criteria_1.json` and evaluated mechanically by
`scripts/evaluate_v6_gates.py`; the full machine-readable result is
`work/v6_gate_decision/gate_decision.json`.

## Decision

```text
DECISION = NO-GO
```

The formal protocol is **not frozen**, the immutable 8,280-cell task list is
**not created**, and the formal run is **not started**. Every other gate
passes; the single failure is the pre-registered performance differential,
which could not certify 18 of its 48 pairs (see below). Per the task rules a
censored pair is never a pass, so the formal matrix stays blocked.

## Gate table

| Gate | Result | Evidence |
|---|---|---|
| `SCG_STRONG_TIME_PASS` | PASS | `protocol/v6_semantics_gate.json` |
| `SCG_SOUNDNESS_AND_COMPLETENESS_PASS` | PASS | same gate |
| `ALL_12_FAMILIES_SEMANTIC_PASS` | PASS | `work/v6_optimized_semantics_validation/` (12/12) |
| `OPTIMIZED_VERIFIER_SEMANTIC_PASS` | PASS | 48/48 tests; 12/12 family witnesses on `2154432` |
| `SCG_BUILD_EQUIVALENCE_PASS` | PASS | 24/24 exact SCG fingerprints, median 2.0× faster |
| `OPTIMIZED_COMPLETE_SET_EQUIVALENCE_PASS` | PASS | 30/30 conclusive differential pairs identical incl. certificate sets |
| `DIFFERENTIAL_VALIDATION_PASS` | **FAIL** | 30/48 conclusive; 18 censored (0 mismatches observed) |
| `PILOT_PERFORMANCE_AUDITED` | PASS | two independent 288-cell audits, no findings |
| `PILOT_BUDGET_AND_MEMORY_PASS` | PASS | conclusive 58.0 % ≥ 55 %; timeout 29.9 % ≤ 40 %; 2 596 min ≤ 3 000; RSS P95 1 653 MiB ≤ 2 048 |
| `GITHUB_RESOURCE_POLICY_PASS` | PASS | 120 shards × 69 tasks, 60 s cap, 20 parallel, 150 min job cap; expected wall 2.16 h, worst 6.9 h |

## Why the differential is the blocker

The frozen protocol re-runs 48 structurally selected Pilot cells at 60 s and
every censored pair at 300 s. **30 pairs completed on both sides and matched
exactly** (verdict, COMPLETE_SET certificate set, enumeration completeness and
every workload counter; 0 mismatches). **18 pairs stayed censored**:

* 7 pairs are intractable in *both* versions — six `COMPLETE_SET`
  `L_high_complete` cells and F06 `depth_shallow`; a 900 s probe also timed out.
  The baseline cannot produce a verdict for these cells at all.
* 11 pairs have the baseline timing out at 300 s while the optimised version
  completes; that is bounded improvement evidence, not a comparable verdict.

The stages the optimisation modifies were additionally compared exactly on the
censored cells: SCG fingerprints 24/24 identical (every marking, enabled set and
exact DBM entry) and the synchronizer automaton fingerprint (every state, edge
and ordered candidate word) is computed for the same cells. On the full frozen
Pilot, the 126 cells completed under both versions agree on verdict, reason and
every workload counter (0 mismatches), and 76 cells flipped
timeout→completed with 0 regressions.

That evidence is strong but it is **not** the pre-registered acceptance rule,
so it cannot turn `DIFFERENTIAL_VALIDATION_PASS` into a pass. The gate stays
false and the formal matrix stays blocked.

## What would unblock the formal matrix

1. **A v2 differential protocol** frozen *before* its execution that (a) puts
   only tractable cells in Layer A, (b) uses longer caps in Layer B for the
   heavy families the task names, and (c) declares in advance that cells
   intractable in *both* versions are certified at the level of the completed
   stages (SCG and synchronizer fingerprints) rather than by a full verdict.
   With the current optimised code the Layer A set of 126 Pilot cells already
   matches 126/126, so this is a practical path.
2. **Or** an explicit user decision to accept the stage-level evidence in
   place of the frozen 48-cell rule. That is a protocol change, so it must be
   recorded as a new frozen version before the formal run is started.

Given the current plan (120 shards × 69 tasks, 60 s cap, max-parallel 20), the
formal matrix itself is feasible: expected compute 43.26 h (2.16 h wall at 20
concurrency), worst case 138 h (6.9 h wall), RSS P95 1.65 GiB on 16 GiB
runners. Nothing in the resource envelope blocks it — only the pre-registered
differential acceptance does.

## What is delivered regardless of GO/NO-GO

`TII_Scalability_v6_Bottleneck_Optimization_Audit.zip` contains the profiling
sample and raw records, the root-cause report, the full source diff, the
semantic and stage-equivalence evidence, both 288-cell Pilot runs with
independent audits and the paired comparison, the frozen differential protocol,
the re-run 96-cell difficulty sample, the budget projection, the historical
36,450-evaluation impact audit status, the GitHub run records, this decision
and a SHA-256 manifest with an archive self-check.
