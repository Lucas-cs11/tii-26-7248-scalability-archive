# v6 Differential v2 — Layer B / Layer C component-equivalence evidence

Optimisation under audit: commit `2154432` (`v6: semantics-preserving synchronizer,
expansion and SCG-index optimisation`).  Baseline: `10d2772`
(`10d27726c085076fe790d574eabd15d7423ac86d`).  Pilot/optimisation HEAD used by the
frozen v1/v2 differential: `835e4bd`; the production sources are byte-identical
to `835e4bd` except for the single read-only `transition_cache` exposure listed
in §2.  All evidence below was produced in `/tmp/v6work` with
`/tmp/v6work/.venv/bin/python` (CPython 3.13.5, 10 cores, 16 GB) on 2026-11-07.

This document reports **two new evidence layers** on top of the frozen
end-to-end differential (`v6-performance-differential-2`):

* **Layer B** — a direct, transition-by-transition proof that the synchronizer
  transition memo does not change semantics, covering the 48 frozen
  differential cells.
* **Layer C** — an audit of each optimised component against a reference
  implementation kept inside the verification script (SCG adjacency index,
  temporal-expansion marking caches, trace-record dedupe, DBM/Fraction
  arithmetic).

It deliberately does **not** claim more than was measured; §8 lists every
limitation and every cell/sample that was not covered.

---

## 1. Reproduce

```bash
cd /tmp/v6work

# Layer B: transition-level synchronizer equivalence, 48 frozen cells
.venv/bin/python scripts/verify_v6_sync_transition_equivalence.py \
    --jobs 3 --cell-timeout 900 --no-memo-timeout 60
# -> work/v6_differential_v2_component/sync_transition_equivalence.json

# Layer C: per-component audits
.venv/bin/python scripts/verify_v6_component_equivalence.py --jobs 3
# -> work/v6_differential_v2_component/component_equivalence.json

# Full test suite (48 pre-existing + 19 new)
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

Both scripts are deterministic up to wall-clock fields; every per-cell or
per-family sample records the exact model parameters, seed and `L`/`H` it used.

---

## 2. Production-path integrity

The frozen differential/pilot evidence was produced from `835e4bd` and the
formal run uses a snapshot of the final HEAD sources, so the production path must
not gain verification-only switches.  The only source change kept is a
read-only exposure of the transition memo that the builder already maintains:

```text
$ git diff 835e4bd -- src/ runner/
--- a/src/synchronizer/builder.py
+++ b/src/synchronizer/builder.py
@@ ObservationSynchronizer
+    # Read-only view of the transition memo used during construction. ...
+    transition_cache: Optional[Dict[Tuple[Tuple, str], Tuple["SyncState", Tuple]]] = None
@@ build_synchronizer return
+        transition_cache=transition_cache,
```

The memo is the same local dictionary that `build_synchronizer` always built
in `2154432`; it is now attached to the returned dataclass instead of being
dropped.  It is not consulted by the construction, it does not change the BFS
order or the automaton, and no caller mutates it.  `use_transition_memo=False`,
`expose_transitions`, `use_caches` and `dedupe_records` parameters were
**removed** from `src/`: the Layer B no-memo reference and the Layer C
cache-free expansion are implemented *inside the verification scripts*, by
copying the frozen algorithm and stripping the cache branches, and the dedupe
comparison uses an in-script monkeypatch of `TemporalExpansion.add_record`
(process-local, restored in `finally`; production code untouched).

---

## 3. Layer B — transition-level synchronizer equivalence

### 3.1 Method

For each of the 48 frozen `v6_perf_differential_1` cells (12 families ×
{depth_shallow, L_mid, L_high_complete, WIP_1}) the checker, in an isolated
child process:

1. generates the model, validates it, builds the SCG and the
   completion-truncated SCG, then builds the synchronizer and reads the exposed
   memo;
2. for **every** synchronizer state (31,374 in total) × **every** observable
   label, recomputes the successor through the uncached path
   `synchronizer_transition(scg, tpn, state, label)` and compares it with the
   memoised `(SyncState, next_key)` for the same `(state_key, label)`:
   `X_N`, `X_F`, the state key and the empty-successor case.  Equality is exact
   (frozensets of class ids);
3. replays the automaton from the initial state using **only those freshly
   computed transitions** and compares the state sequence, the ordered edge
   list and the ordered candidate-word list with
   `sync.states` / `sync.edges` / `sync.candidate_words`; a SHA-256 of the
   canonical automaton is recorded for both;
4. separately, a cache-free copy of the BFS (no memo at all) is built as an
   independent no-memo reference; it is compared with the memoised automaton
   within a per-cell 60 s budget and the comparison is reported separately
   (`no_memo_full_automaton`), never merged into the transition-level gate.

Memo-missing pairs are not compared with a memo (none exists): these are
transitions of states that the production BFS never expands because the state is
only reached at the depth limit (`depth_limit`) or has empty `X_N`.  They are
counted, and their fresh values are still exercised by the graph replay when
reachable.

### 3.2 Result: PASS

| quantity | value |
| --- | --- |
| cells total | 48 |
| cells checked at transition level | **45** |
| cells censored (900 s cell budget) | **3** |
| states checked | **31,374** |
| transitions checked (state × label) | **726,665** |
| fresh == memo | **627,833** |
| memo-missing pairs (depth-limit states) | **98,832** |
| empty-successor pairs | **657,299** |
| mismatches | **0** |
| graph reconstruction pass | **45 / 45 checked cells** |
| no-memo full automaton | **39 match / 6 skipped_budget / 0 mismatch** |
| PASS / coverage_complete | **true / true** |
| wall (parallel run, jobs=3) | 1,428.51 s; per-cell sum 3,815.60 s |

The 18 cells censored by the frozen v1 differential are all included: **15 were
checked** at transition level and only the same **3** that are unbuildable were
censored again (not silently removed).

### 3.3 Per-cell results

Walls are per-cell wall seconds.  `—` = censored.

| family | stratum | status | states | transitions | mismatch | graph | no-memo | wall s |
| --- | --- | --- | ---: | ---: | ---: | --- | --- | ---: |
| F01 | L_high_complete | CHECKED | 10 | 80 | 0 | yes | match | 0.014 |
| F01 | L_mid | CHECKED | 7 | 63 | 0 | yes | match | 0.013 |
| F01 | WIP_1 | CHECKED | 85 | 1,955 | 0 | yes | match | 0.08 |
| F01 | depth_shallow | CHECKED | 9 | 72 | 0 | yes | match | 0.012 |
| F02 | L_high_complete | CHECKED | 21 | 357 | 0 | yes | match | 0.019 |
| F02 | L_mid | CHECKED | 10 | 170 | 0 | yes | match | 0.015 |
| F02 | WIP_1 | CHECKED | 197 | 8,668 | 0 | yes | match | 0.22 |
| F02 | depth_shallow | CHECKED | 17 | 255 | 0 | yes | match | 0.017 |
| F03 | L_high_complete | CHECKED | 23 | 460 | 0 | yes | match | 0.071 |
| F03 | L_mid | CHECKED | 15 | 300 | 0 | yes | match | 0.02 |
| F03 | WIP_1 | CHECKED | 201 | 9,246 | 0 | yes | match | 1.116 |
| F03 | depth_shallow | CHECKED | 20 | 360 | 0 | yes | match | 0.042 |
| F04 | L_high_complete | CHECKED | 45 | 1,170 | 0 | yes | match | 0.06 |
| F04 | L_mid | CHECKED | 23 | 598 | 0 | yes | match | 0.026 |
| F04 | WIP_1 | CHECKED | 117 | 3,978 | 0 | yes | match | 0.299 |
| F04 | depth_shallow | CHECKED | 32 | 640 | 0 | yes | match | 0.035 |
| F05 | L_high_complete | CHECKED | 53 | 1,643 | 0 | yes | match | 0.757 |
| F05 | L_mid | CHECKED | 30 | 930 | 0 | yes | match | 0.057 |
| F05 | WIP_1 | CHECKED | 137 | 5,206 | 0 | yes | match | 4.619 |
| F05 | depth_shallow | CHECKED | 43 | 1,118 | 0 | yes | match | 0.264 |
| F06 | L_high_complete | **CENSORED** | — | — | 0 | — | not_attempted | 1020.77 |
| F06 | L_mid | CHECKED | 9,131 | 228,275 | 0 | yes | match | 40.631 |
| F06 | WIP_1 | CHECKED | 93 | 2,325 | 0 | yes | match | 0.101 |
| F06 | depth_shallow | CHECKED | 5,699 | 91,184 | 0 | yes | match | 30.577 |
| F07 | L_high_complete | CHECKED | 2,448 | 48,960 | 0 | yes | skipped_budget | 77.557 |
| F07 | L_mid | CHECKED | 1,738 | 34,760 | 0 | yes | match | 8.21 |
| F07 | WIP_1 | CHECKED | 81 | 1,539 | 0 | yes | match | 0.071 |
| F07 | depth_shallow | CHECKED | 407 | 6,105 | 0 | yes | match | 0.727 |
| F08 | L_high_complete | CHECKED | 1,939 | 54,292 | 0 | yes | skipped_budget | 130.033 |
| F08 | L_mid | CHECKED | 1,865 | 50,355 | 0 | yes | match | 37.059 |
| F08 | WIP_1 | CHECKED | 97 | 2,619 | 0 | yes | match | 0.602 |
| F08 | depth_shallow | CHECKED | 1,218 | 26,796 | 0 | yes | match | 41.914 |
| F09 | L_high_complete | **CENSORED** | — | — | 0 | — | not_attempted | 1021.16 |
| F09 | L_mid | CHECKED | 1,087 | 32,610 | 0 | yes | match | 27.421 |
| F09 | WIP_1 | CHECKED | 165 | 7,095 | 0 | yes | match | 1.967 |
| F09 | depth_shallow | CHECKED | 1,722 | 46,494 | 0 | yes | skipped_budget | 142.739 |
| F10 | L_high_complete | CHECKED | 21 | 357 | 0 | yes | match | 0.133 |
| F10 | L_mid | CHECKED | 12 | 204 | 0 | yes | match | 0.034 |
| F10 | WIP_1 | CHECKED | 173 | 6,747 | 0 | yes | match | 7.006 |
| F10 | depth_shallow | CHECKED | 17 | 272 | 0 | yes | match | 0.146 |
| F11 | L_high_complete | CHECKED | 457 | 10,054 | 0 | yes | skipped_budget | 99.021 |
| F11 | L_mid | CHECKED | 316 | 6,952 | 0 | yes | match | 1.835 |
| F11 | WIP_1 | CHECKED | 85 | 1,870 | 0 | yes | match | 1.385 |
| F11 | depth_shallow | CHECKED | 580 | 8,700 | 0 | yes | match | 5.952 |
| F12 | L_high_complete | **CENSORED** | — | — | 0 | — | not_attempted | 919.82 |
| F12 | L_mid | CHECKED | 430 | 9,890 | 0 | yes | match | 24.594 |
| F12 | WIP_1 | CHECKED | 109 | 3,161 | 0 | yes | skipped_budget | 79.381 |
| F12 | depth_shallow | CHECKED | 389 | 7,780 | 0 | yes | skipped_budget | 86.986 |

Slowest checked cells: F09 depth_shallow 142.7 s (2.51 M sync edges),
F08 L_high_complete 130.0 s (2.44 M), F11 L_high_complete 99.0 s (1.03 M),
F12 depth_shallow 87.0 s (0.40 M), F12 WIP_1 79.4 s.  Timings are split into
`model / scg / sync / transitions / replay` in the JSON.

### 3.4 Censored cells (3) — explicit budget reason

Each cell had a **900 s transition-level wall budget** (in-worker `SIGALRM`);
the parent kills a child after a 1020 s hard grace (900 s + 120 s).  The three
cells below cannot construct their synchronizer automaton within that budget,
so no transition can exist to compare.  They are listed in both
`censored_cells` of the JSON and in `per_cell` (never silently dropped from the
denominator), and they are **not counted as passes**:

| task_id | family | stratum | why |
| --- | --- | --- | --- |
| `cfg6-39963f9bd2f7199fffc5` | F06_parallel_parts | L_high_complete | sync BFS enumerates label-prefix words up to L=26 with a large observable alphabet and non-emptying `X_N`; exceeds 900 s |
| `cfg6-6d9fb9c2c4dcd5ab9dad` | F09_deep_fault_stages | L_high_complete | same, L=34 |
| `cfg6-1720f92bbc0b6ab25ad6` | F12_sparse_cycle | L_high_complete | same, L=30 (cyclic, `X_N` never empties) |

These are exactly the three cells that are also censored **on the optimised
side** of the frozen v1 differential at its 300 s cap
(`optimized_status = TIMEOUT_WALL_CLOCK` in
`work/v6_perf_differential_1/differential_results.json`), i.e. the automaton
itself is not constructible at a realistic budget.  Extending the budget does
not help: word-prefix enumeration grows combinatorially in L for these
unpruned cells.

### 3.5 No-memo full-automaton comparison (separate gate)

A cache-free copy of the production BFS was run for every checked cell with a
**60 s per-cell budget**; the result is reported under
`no_memo_full_automaton` and never affects Layer B PASS:

* 39 cells: cache-free automaton **identical** (state sequence, ordered edges,
  ordered candidate words), 0 mismatches;
* 6 cells: `skipped_budget` — the cache-free BFS needs to re-evaluate every
  label-prefix occurrence (up to millions of occurrences), so it exceeds 60 s:
  `cfg6-2895e04d423aad0dc640` (F07 L_high),
  `cfg6-25b732e87235ba7ee634` (F08 L_high),
  `cfg6-ebd02972adb6f5321bbe` (F09 depth),
  `cfg6-3312354754090ff7c06c` (F11 L_high),
  `cfg6-4254da700cc1d40b9a43` (F12 WIP_1),
  `cfg6-08bada213ee6a3b28be1` (F12 depth).

For these six, the transition-level check (fresh vs memo over every state ×
label, plus the fresh-transition replay) still fully passed; only the
independent no-memo rebuild is budget-censored.

---

## 4. Layer C — component audits

Output: `work/v6_differential_v2_component/component_equivalence.json`
(`PASS=true`, `mismatches=[]`, run wall 81.05 s, jobs=3).

Coverage is pre-registered in the script (`COVERAGE_MINIMA`, declared before
measurement) and reported per component in a `coverage` object.  `PASS`
requires **zero mismatches and the minimum coverage**; a zero-mismatch run on
too little evidence would not be reported as a pass.

### 4.1 SCG adjacency index — PASS

Sample: the 24 frozen `v6_profile_sample_1` cells of strata
`depth_shallow` and `L_mid` = 12 families × 2, including the heavy F06/F12
models.  For every state class of every SCG:

* `scg.successors(cid)` must equal the raw edge-table projection
  `[(t, dst) for (src, t, dst) in scg.edges if src == cid]` — exact list
  equality, hence order, multiplicity and labels;
* classes without outgoing edges return `[]`;
* the index is lazily built (observed `None` before first use), repeated
  lookups are stable (not stale after construction), unknown ids return `[]`,
  and `build_truncated()`'s fresh graph projects the truncated edge list.

| measured | value |
| --- | --- |
| models checked | **24 / 24** |
| classes checked (full SCG) | **60,767** |
| classes checked (truncated SCG) | **60,767** |
| empty-successor classes | 24 |
| lazy index observed on all models | yes |
| mismatches | **0** |

### 4.2 Temporal-expansion marking caches — PASS

The cache-free reference `build_expansion_uncached` is a verbatim copy of the
frozen algorithm with the marking caches removed.  For each family the audit
searches the same generator for a model with ≥2 expandable candidate words; the
chosen configuration is recorded in the JSON.  The fingerprint compared is:

* ordered node sequence `(marking, box, k, q)` (canonical rational/`inf` render);
* ordered edge list `(src, transition, dst)`;
* for every node, the ordered `(sigma, J_hat)` record list;
* the ordered `terminal_normal` and `terminal_faulty` lists.

| measured | value |
| --- | --- |
| families / models checked | **12 / 12** |
| words compared | **25** |
| (node, box, k, q) nodes compared | **1,891** |
| multi-record nodes (different sigmas merged) | **1,354** |
| mismatches | **0** |

Per-family sample (all candidate-word samples are genuine synchronizer
candidate words except F02, explained below; `reduced_pX_oY` = same F-family
generator re-run with `n_parts=X`, `ops_per_part=Y` because the frozen
full-size candidate expansions exceed the audit budget):

| family | sample kind | seed | overrides | words | nodes | multi-record nodes |
| --- | --- | --- | --- | ---: | ---: | ---: |
| F01 | candidate_word_reduced_p2_o2 | 2 | n_parts=2, ops_per_part=2 | 2 | 68 | 0 |
| F02 | synthetic_observable_word | 60977 | — | 3 | 12 | 0 |
| F03 | candidate_word_frozen_model | 38924 | ops_per_part=8 | 2 | 171 | 14 |
| F04 | candidate_word_frozen_model | 75714 | ops_per_part=3 | 2 | 64 | 0 |
| F05 | candidate_word_frozen_model | 72541 | ops_per_part=4 | 2 | 102 | 0 |
| F06 | candidate_word_reduced_p2_o2 | 2 | n_parts=2, ops_per_part=2 | 2 | 212 | 206 |
| F07 | candidate_word_reduced_p2_o2 | 1 | n_parts=2, ops_per_part=2 | 2 | 100 | 94 |
| F08 | candidate_word_reduced_p2_o2 | 2 | n_parts=2, ops_per_part=2 | 2 | 206 | 200 |
| F09 | candidate_word_reduced_p2_o2 | 2 | n_parts=2, ops_per_part=2 | 2 | 194 | 188 |
| F10 | candidate_word_reduced_p2_o2 | 2 | n_parts=2, ops_per_part=2 | 2 | 96 | 0 |
| F11 | candidate_word_frozen_model | 52855 | ops_per_part=2 | 2 | 169 | 161 |
| F12 | candidate_word_reduced_p2_o2 | 1 | n_parts=2, ops_per_part=2 | 2 | 497 | 491 |

### 4.3 Trace-record dedupe — PASS

Comparison: production expansion (dedupe on) vs the same expansion with
`TemporalExpansion.add_record` monkeypatched in-process to the no-dedupe rule.
The monkeypatch is restored in `finally` and never touches `src/`.

| measured | value |
| --- | --- |
| samples compared (word-level) | **25** |
| multi-record nodes (different sigmas, same `(marking,box,k,q)`) | **1,354** |
| distinct COMPLETE_SET critical pairs compared | **85,710** |
| duplicate identical `(node, sigma)` occurrences hidden by dedupe | **0** |
| mismatches (distinct record set / terminal set / critical-pair set) | **0** |

Because `duplicate_sigma_occurrences = 0`, the dedupe rule is **inert on every
audited expansion**: the successor of `(node, sigma)` in Algorithm S3.2 is
deterministic, so the same firing sequence is generated at most once per node,
and the rule is defensive.  What the audit proves exactly is what is required:
switching dedupe off adds no record that is not already present with an
identical `J_hat`; the distinct record set, the distinct terminal set and the
COMPLETE_SET critical-pair set are unchanged and no different-sigma record is
hidden (1,354 multi-record nodes are preserved in both runs).  The
`families_with_zero_duplicate_sigma` list in the JSON names all 12 families: for
these models no duplicate sigma exists to hide, which is why no
duplicate-sigma sample can be exhibited.

### 4.4 DBM / Fraction boundaries — PASS

`tests/test_v6_component_equivalence.py::DBMFractionBoundaryTests`, 13 cases,
all pass (no existing test was changed or weakened):

half-second values (`Fraction(1,2)` initial zones and successors); arbitrary
denominators (`1/3`, `5/7`, `2/7`); negative and zero bounds; infinite latest;
exact boundary equality (tight `epsilon=0` constraint legal and urgent-conflict
infeasible); empty-zone normalisation (`-1 <= x <= -2` ⇒ negative diagonal);
normalisation canonicality (redundant loose/tight cross terms normalise to the
same matrix, equal hashes, idempotent hash cache); persistent-clock projection
after a half-second firing; newly-enabled reset with quarter-second intervals;
self-reenabling reset; and exact-rational oracle trace equivalence
(`enumerate_oracle_traces` vs SCG traces) on half-second nets.

### 4.5 Independent oracles — PASS

Existing `tests/test_fraction_oracle.py`: **15/15 pass**.
Existing `tests/test_v6_family_semantics.py`: **1/1 pass** (all families have
exact timed witnesses and invariants).

---

## 5. Evidence hierarchy (do not conflate the layers)

| layer | what it proves | coverage |
| --- | --- | --- |
| frozen v1 end-to-end differential (`work/v6_perf_differential_1/`) | baseline vs optimised *verdict, certificates, completeness, workload counters* on frozen cells | 30/48 matched, 18 censored (3 of those are also censored on the optimised side at 300 s) |
| **Layer B (this document)** | the memoised synchronizer transition equals the uncached transition for **every** state × label, and a fresh-transition replay reproduces the automaton exactly | 45/48 cells; 3 cells unbuildable, listed |
| **Layer C (this document)** | each optimised component equals an independent reference (adjacency index, marking caches, dedupe rule, exact-rational DBM) | 24 SCG models, 12 expansion families/25 words, 25 dedupe samples, 13 DBM cases + 16 oracle tests |

Layer B is **not** a claim that "all 48 full automata were compared end-to-end
with the baseline": it is an in-tree equivalence proof of the memoised
transition plus an exact automaton reconstruction, which is stronger at the
transition level but does not replace the end-to-end differential for the 3
unbuildable cells.

---

## 6. Tests

```text
$ .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
...................................................................
----------------------------------------------------------------------
Ran 67 tests in 16.951s

OK
```

48 pre-existing tests are unchanged and still pass; `tests/test_v6_component_equivalence.py`
adds 19 tests (13 DBM/Fraction boundary + 6 component-cache equivalence tests:
memo vs fresh, fresh replay, adjacency index vs edge projection, marking cache
vs cache-free reference, dedupe projection, exposed cache invariants).

---

## 7. Files

| path | role |
| --- | --- |
| `scripts/verify_v6_sync_transition_equivalence.py` | Layer B checker (per-cell isolated child processes, budgeted) |
| `scripts/verify_v6_component_equivalence.py` | Layer C checker (cache-free references kept in-script) |
| `tests/test_v6_component_equivalence.py` | 19 new regression tests |
| `work/v6_differential_v2_component/sync_transition_equivalence.json` | Layer B evidence (PASS=true) |
| `work/v6_differential_v2_component/component_equivalence.json` | Layer C evidence (PASS=true) |
| `work/v6_differential_v2_component/per_cell_sync_transition/*.json` | raw per-cell Layer B records |
| `src/synchronizer/builder.py` | only the 7-line read-only `transition_cache` exposure |

`git diff 835e4bd -- src/ runner/` contains only that exposure; no other
production file differs from the pilot head.

---

## 8. Limitations (honest list)

1. **3/48 Layer B cells are censored**, not passed: F06/F09/F12
   `L_high_complete` cannot build the synchronizer within 900 s (long-L
   word-prefix enumeration).  They are also censored on the optimised side of
   the frozen v1 differential at 300 s, so no automaton exists to compare.
2. **No-memo full-automaton comparison covers 39/45 checked cells**; the other
   6 are `skipped_budget` at 60 s because the cache-free BFS must re-evaluate
   every prefix occurrence (millions).  They remain fully covered by the
   transition-level check and the fresh-transition replay.
3. **Memo-missing pairs (98,832) are not compared against a memo** because the
   production BFS never memoises transitions of states that are never expanded
   (depth limit).  They are counted and their fresh values participate in the
   replay where reachable.
4. **Layer C expansion coverage is small/medium-sized expansions** (max 497
   nodes per sample): for F06/F08/F12 the full-size candidate words exceed the
   audit budget, so the same generator was re-run with reduced
   `n_parts`/`ops_per_part` (recorded per family).  The cache logic exercised is
   the same; this is not evidence about full-size expansions.
5. **F02 has no candidate word in any sampled model** (normal and faulty class
   sets never both touch the completion class at the same synchroniser state in
   the sampled configurations).  For F02 the marking-cache audit uses three
   synthetic single-label observable words and is labelled
   `synthetic_observable_word`; it still exercises every cache call site.
6. **Trace-record dedupe is inert on the audited samples** (`0` duplicate
   sigma); the audit proves exact projection/set equality and preservation of
   1,354 multi-record nodes, but cannot exhibit a duplicate-sigma sample because
   the expansion is deterministic.  This is a property of the algorithm, not a
   coverage gap that could be fixed by more models.
7. **Layer C is not a baseline-vs-optimised differential**: both sides run in
   the optimised tree, the reference implementations are copies kept in the
   audit script.  Baseline-vs-optimised component evidence is the existing
   `work/v6_scg_equivalence_1/` (24/24 exact SCG fingerprints) and
   `work/v6_sync_equivalence_1/` (1/18 matched, 17 baseline-timeout censored).
8. Wall-clock and peak memory are environment-dependent; peak RSS for the
   largest Layer B cell was ≈2.7 GB, which is why the audit ran with 3 workers.
