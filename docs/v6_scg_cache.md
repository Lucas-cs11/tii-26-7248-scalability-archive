# v6 same-model SCG cache

## Motivation

The State Class Graph (SCG) is a function of the Time Petri Net alone. The
observable horizon `L`, the horizon `H` and the analysis mode (`first` /
`complete`) only enter *after* construction, in the synchronizer and the
temporal expansion. A benchmark that sweeps `L`/`H`/`mode` for one fixed model
therefore rebuilds the identical SCG once per cell.

The v6 Pilot showed why this matters and where it stops mattering:

* 160 of 288 Pilot cells hit the 60 s wall clock; only 32 of those stopped
  during SCG construction, while 126 stopped *after* `SCG_COMPLETE` in
  synchronizer work and 2 in partial-history analysis.
* The L-sweep cells are the ones that repeat one model's construction, but the
  completed L-sweep cells are cheap (Pilot P50 ≈ 8.9 s for the whole cell).

Semantics-preserving SCG reuse is therefore the only optimization this project
permits, and its expected effect is bounded: it removes repeated construction,
not synchronizer or expansion time. No truncation, approximation or
cross-model reuse is implemented.

## Correctness contract

`src/scg/cache.py` implements a content-addressed, versioned cache.

**Key.** A cache entry is only read when every component matches:

```text
key_material = {
  "format": "v6-scg-cache", "format_version": 2,
  "model_hash":      <sha256 of the full TPN model identity>,
  "source_sha":      <executed commit SHA>,
  "scg_builder_sha": <sha256 of src/scg/builder.py bytes>,
  "semantics_version": "v6-strong-time-1",
  "python":          "<major.minor>",
  "serialization":   "json-fraction-v2"
}
cache_key = sha256(canonical_json(key_material))
```

`model_hash` is the same identity used by the frozen task lists, so `L`, `H`
and `mode` deliberately do *not* appear: they cannot change the SCG, and any
change to the builder, the semantics, the interpreter or the format invalidates
the entry.

**Storage.** Line-oriented canonical JSON (`json-fraction-v2`): one header
line, one line per state class, one line for the edge list, and a trailing
integrity line holding the SHA-256 of every preceding byte. Rationals are
`"num/den"` strings; the two infinities are `"inf"` / `"-inf"`. There is no
pickle, so a cache file can never execute code.

**Failure semantics.** A missing, truncated, corrupted, hash-mismatched,
key-mismatched or oversize (`> 64 MiB`) entry is a *miss* with a recorded
reason, never an error. Writes go to a temporary file followed by
`os.replace`, so a killed worker cannot leave a half-written entry. `64 MiB` is
a resource guard only; it never changes a verdict.

**No counter masquerading.** `verify()` accepts `prebuilt_scg` and
`scg_build_seconds`. A cache hit passes no build duration, so
`stats["runtime_scg"]` is `0.0` and the loader time is recorded separately as
`runtime_scg_load_s`; a miss attributes the measured construction time to the
cell that paid it. `scg_cache_status`, `scg_cache_key` and
`scg_cache_provenance` live on the record, outside the algorithm counters.
The default `verify()` path (no prebuilt SCG) is byte-for-byte unchanged.

**Ordering.** On a miss the SCG is built, persisted, and only then handed to
the verifier; therefore a cell that later dies in the synchronizer still leaves
a usable entry. The miss decision is checkpointed *before* construction, so a
kill during construction records `miss`, not `unknown`.

## Differential evidence

`tests/test_scg_cache.py` (13 tests, part of the 48-test suite):

* cold build vs in-memory prebuilt vs serialized-load must agree on verdict,
  inconclusive reason, certificate mode, enumeration completeness, every
  certificate (φ, σ_N, σ_F, overlap), both SCG flags and every non-runtime
  workload counter — in both `first` and `complete` modes;
* key binding: every component of the key changes the key; the builder hash is
  the SHA-256 of the builder file;
* corruption: invalid JSON, a flipped payload byte, an oversize entry and a
  disabled cache all degrade to `miss`; atomic write leaves no temporary files;
* end-to-end: two identical `runner/run_cell.py` subprocesses with one
  `SCG_CACHE_DIR` produce `miss` then `hit`, equal verdicts/counters, positive
  build time on the first and `0.0` build time plus a separate load time on the
  second.

`protocol/v6-cache-validation-1` (96 cells, 12 families × 8, one family per
shard, pre-registered 60 miss / 36 hit) added end-to-end evidence on GitHub:
36/36 hits had an earlier same-key miss in the same shard, 0 store failures,
0 degraded/disabled cells, all keys and provenance verified by an independent
audit. See `docs/v6_cache_validation_report.md`.

## Measured cost

Serialization format v2 exists because the first, monolithic encoder doubled
peak RSS on large graphs:

| 50,001-class SCG (SCG state limit) | build | store | load | peak RSS |
|---|---:|---:|---:|---:|
| cold build only | 60.7 s | – | – | 202.8 MiB |
| v1 monolithic encoder | – | 1.4 s | 4.8 s | 458.8 MiB |
| v2 streaming encoder | – | 1.7 s | 4.8 s | 244.0 MiB |

On the cache-validation run (all 12 families):

| Phase | P50 | P90 |
|---|---:|---:|
| SCG build (miss) | 1.48 s | 39.8 s |
| SCG load (hit) | 0.0037 s | 0.334 s |

The largest representative model in the validation set (F06, 26,919 classes)
built in 39.8 s and loaded in 2.9 s; every later cell of that model in the same
shard spent the saved time in the synchronizer instead and still hit the 60 s
wall.

## Limits

* The cache removes repeated construction only. It cannot reduce synchronizer,
  expansion or LP time, so it does not change the timeout fraction — in the
  validation run the cache-hit cells of heavy families still timed out.
* Entries are per-job and ephemeral (`SCG_CACHE_DIR` points into the runner
  temporary directory). Reuse is bounded by same-model colocation inside one
  job, and by the executed commit SHA.
* Mean stored entry is 6.08 MiB (max 30.5 MiB) on the validation set; the
  per-shard peak was 70 MiB. Aggregate storage across a whole formal matrix is
  not a per-runner requirement and must not be reported as one.
