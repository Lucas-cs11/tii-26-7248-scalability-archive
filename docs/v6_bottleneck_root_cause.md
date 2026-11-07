# v6 bottleneck root cause

This report profiles the bounded-verification pipeline on a frozen, outcome-blind
sample and names the stages that actually consume the 60 s budget. It is the
basis for the optimisation in commit `2154432` and for the differential
protocol `v6-performance-differential-1`.

## Method

* Sample: `protocol/v6_profile_sample_1.json` — 72 cells, one per family per
  stratum (12 families × 6 pre-registered strata: `depth_shallow`, `L_mid`,
  `L_high_complete`, `WIP_2`, `WIP_3`, `difficult_first`). Selection is
  structural; no verdict or runtime is read.
* Instrumentation: `src/profiling.py` plus stage timers/counters threaded
  through the verifier, synchronizer, temporal expansion and LP solver. All
  hooks are no-ops when no profile dict is passed, so the unprofiled path is
  unchanged.
* Execution: `scripts/run_v6_profile_sample.py`, per-cell subprocess, 60 s
  external wall-clock cap (the Pilot cap), per-cell checkpoint, six families in
  parallel, local macOS runner. Raw records are kept in
  `work/v6_profile_20261107_134729/`.
* Function-level profiling with `cProfile` on two representative cells.

## What the sample shows

Execution status of the 72 cells: **20 `COMPLETED`, 52 `TIMEOUT_WALL_CLOCK`,
0 `ERROR`** (72.2 % right-censored). The cells that hit the cap stopped at:

| Last completed stage | Cells | Meaning |
|---|---:|---|
| `MODEL_VALIDATED` | 18 | killed while constructing the SCG |
| `SCG_COMPLETE` | 33 | killed inside the observation synchronizer |
| `HISTORY_ANALYSIS_PARTIAL` | 1 | killed during history expansion / critical pairs |

Attributing each cell's spent time to the stage it was in (completed cells use
the measured stage timers; censored cells are attributed to the stage they were
executing at the cap) gives:

| Stage | Seconds over 72 cells | Share |
|---|---:|---:|
| Observation synchronizer (`runtime_sync`) | 1 781.8 | **53.7 %** |
| SCG construction (`runtime_scg`) | 1 461.9 | **44.0 %** |
| LP confirmation (`runtime_lp`) | 47.9 | 1.4 % |
| Temporal expansion (`runtime_expansion`) | 7.5 | 0.2 % |
| Other (model generation, validation, record I/O) | 19.9 | 0.6 % |

The split is family dependent. For the heavy families the synchronizer is the
larger term even before accounting for the censored cells' remaining time:

| Family | SCG build (s) | Synchronizer (s) | Expansion (s) | LP (s) |
|---|---:|---:|---:|---:|
| F01 sequential sparse | 124.0 | 56.1 | 0.0 | 0.0 |
| F02 sequential dense | 126.4 | 53.7 | 0.0 | 0.0 |
| F03 deep inspection | 143.6 | 37.2 | 1.1 | 7.7 |
| F04 alternative routes | 124.8 | 56.1 | 0.4 | 0.7 |
| F05 routes+inspections | 127.4 | 72.0 | 6.1 | 39.4 |
| F06 parallel parts | 163.3 | **196.7** | 0.0 | 0.0 |
| F07 shared contention | 74.9 | **275.1** | 0.0 | 0.0 |
| F08 mixed visibility | 106.6 | **253.4** | 0.0 | 0.0 |
| F09 deep fault stages | 135.1 | **224.9** | 0.0 | 0.0 |
| F10 bounded rework | 137.2 | 43.8 | 0.0 | 0.0 |
| F11 resource cycle | 74.9 | **285.1** | 0.0 | 0.0 |
| F12 sparse cycle | 123.7 | **227.9** | 0.0 | 0.0 |

## Function-level evidence

**Synchronizer (F12 `L_mid`; SCG = 1 630 classes / 3 263 edges).** In a 25 s
`cProfile` window: `build_synchronizer` 21.2 s (85 %), of which
`SCG.successors()` is **20.6 s (82 %) over 334 913 calls**, `reach_N`/`reach_F`
13.5 s / 8.6 s and the unobservable closures 6.5 s. The construction performed
2 830 synchronizer transitions while the automaton only has **53 states** —
the same transition is recomputed for every word prefix that reaches a state.

`SCG.successors()` was implemented as
`[(t, dst) for (src, t, dst) in self.edges if src == class_id]`: a full edge-list
scan per lookup. Every closure BFS step pays it, so a closure is
O(visited × |edges|) instead of O(visited × out-degree).

**SCG construction (F02 `WIP_3`; the model reaches the 50 001-class cap).** In a
25 s window: `_compute_successor` 22.7 s (91 %), of which
`DBM.normalize()` 17.5 s. The Floyd-Warshall inner loop called `math.isinf` on
`Fraction` values, which forces a `numbers.__float__` conversion: that path cost
7.8 s + 5.6 s, more than the rational arithmetic itself. `tpn.enabled_set` was
recomputed 39 491 times (3.5 s), and `persistent_enabled` / `newly_enabled` each
re-fired the transition and recomputed both enabled sets.

## Answers to the ten questions

1. **Largest stage.** Observation synchronizer: 53.7 % of attributed sample
   time and 33/72 timeouts. SCG construction is second (44.0 %, 18/72).
2. **Share of runtime.** 53.7 % synchronizer, 44.0 % SCG, 1.4 % LP, 0.2 %
   expansion on this sample; per family the synchronizer dominates in F06–F09,
   F11, F12.
3. **What drives complexity.** The synchronizer cost scales with
   (SCG edges) × (closure visits) × (word prefixes), i.e. with model size and
   SCG connectivity; the SCG cost scales with classes² × enabled transitions in
   the DBM closure, driven by WIP bound and rework/difficulty structure. Observable
   ambiguity (LP) is a minor term.
4. **Repeated work.** Successor lookups re-scan the entire edge list; the same
   `(state, label)` transition is recomputed for every word prefix; closures are
   recomputed per transition; expansion re-derives enabling/marking data per
   `(node, sigma)`; SCG construction re-computes enabled sets and re-fires
   transitions for every successor candidate; the DBM closure converts every
   `Fraction` to `float` twice per comparison.
5. **Redundant states/histories.** Synchronizer states are deduplicated but
   transitions are not; expansion trace records are deduplicated by a linear scan
   over the node's record list.
6. **Repeated LPs.** No. LP build and solve are each about half of a 2 %
   total; no identical pair was re-solved. Caching LP results is not the lever.
7. **FIRST_CERTIFICATE vs COMPLETE_SET.** Both die in the synchronizer first;
   `COMPLETE_SET` additionally enters history expansion (the one
   `HISTORY_ANALYSIS_PARTIAL` timeout processed 182 of 992 candidate words) and
   pays a larger LP bill. The shared upstream bottleneck is the same.
8. **Timeouts during SCG construction.** 18/72 (25 %) of the sample, 32/160 of
   the frozen Pilot. They are the heavy `WIP_3`/`difficult` cells whose class
   space reaches the 50 000-class cap or exceeds the cap in time.
9. **Why the same-model cache helps little.** The cache removes repeated SCG
   construction only. In the heavy families the dominant cost is downstream of
   the SCG (synchronizer/expansion), and the SCG-construction timeouts are on
   distinct heavy models that are not repeated within a job. A cache hit can
   therefore still be followed by a synchronizer timeout — exactly what the
   `v6-cache-validation-1` run measured (cache saving < 0.01 % of the formal
   budget).
10. **Most promising optimisations.** Index the SCG adjacency; memoise the
    synchronizer transition by `(state, label)`; cache marking-derived data in
    temporal expansion; remove the `Fraction→float` conversion from the DBM
    closure and cache DBM hashes. All are pure memoisation of deterministic
    functions — no pruning or merging.

## What was implemented and what it bought

Commit `2154432` implements exactly those four levers (see the commit message
for the code-level list). Single-core before/after on frozen models:

| Cell | Baseline | Optimised |
|---|---:|---:|
| F12 `L_mid` synchronizer-timeout cell (1 630-class SCG) | TIMEOUT at 60 s | **1.27 s** (`VER`) |
| F02 `WIP_3` SCG build | 83.9 s | 26.5 s |
| F01 `WIP_3` SCG build | 68.3 s | 21.5 s |
| F06 default-model SCG build | 39.6 s | 9.4 s |

The SCG is unchanged: class counts, edge counts, state-limit flags and every
exact DBM entry match the baseline fingerprint (see
`work/v6_scg_equivalence_1/scg_equivalence.json`).

Re-running the same 72-cell sample with the optimised code raised completed
cells from 20 to 33 and lowered timeouts from 52 to 39 (17 cells flipped
timeout → completed). Two `F12` cells flipped the other way while the SCG
optimisation was not yet merged; both are re-checked in the differential Layer B
and in the 288-cell Pilot.

## Remaining boundary

The remaining 39/72 timeouts are dominated by SCG construction (20) and
synchronizer/expansion on the very largest models (14 + 4). These are genuine
state-space costs, not duplicated work; they are reported as right-censored, not
hidden.
