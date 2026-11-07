# v6 Modal execution environment (frozen)

Scope: this document freezes everything needed to execute the v6 scalability
benchmark on Modal (formal 8,280 cells only; GitHub Actions is not used for the
formal run). It records the execution image, resource envelope, timeout
semantics, volume layout, recovery protocol, cost-observation rules and the
Pilot acceptance criteria. Nothing here changes the mathematics: the verifier
under `src/` is executed unchanged as a subprocess.

Code locations:

| Item | Path |
|---|---|
| Modal App (image, Volume, `run_shard` Function) | `modal_app/v6_modal_runner.py` |
| Pure shard executor (no `modal` import; also used by the simulation) | `modal_app/v6_shard_worker.py` |
| Local orchestrator (`plan`/`pilot`/`run`/`status`/`download`/`resume`/`simulate`) | `scripts/run_v6_modal.py` |
| Model-colocated shard plan builder | `scripts/build_v6_modal_shards.py` |
| Independent result audit | `scripts/audit_v6_modal_results.py` |
| Exact pins | `requirements-modal.lock` |
| Budget state (owned by the budget guard) | `work/v6_modal_budget/budget.json` |

> Note: the App directory is `modal_app/`, not `modal/`. A top-level `modal/`
> package directory in the repository root would collide with the real Modal
> SDK during `import modal`; `modal_app/` keeps `import modal` resolving to the
> installed client (`site-packages/modal/__init__.py`, client 1.6.3).

## 1. Frozen environment

### 1.1 Execution image

| Component | Value |
|---|---|
| Base | `modal.Image.debian_slim(python_version="3.13")` (Python 3.13 series) |
| `numpy` | `2.3.4` (hash pinned, from `requirements-v5.lock`) |
| `scipy` | `1.16.2` (hash pinned, from `requirements-v5.lock`) |
| Modal client | `1.6.3` (needed inside the container only for `current_function_call_id`) |
| LP solver | the repository's own LP path (`scipy.optimize.linprog`, HiGHS via SciPy) — no external solver is installed |
| GPU | none (`gpu` parameter is not passed; CPU-only Function) |
| Source snapshot | `src/`, `runner/`, `modal_app/`, `requirements-v5.lock`, `requirements-modal.lock` mounted at `/root/v6repo` (`add_local_dir`, `copy=False`) |
| Local artifacts in image | none: `.venv/`, `.git/`, `work/`, `results/`, `outputs/`, `protocol/`, `tests/`, `scripts/`, `docs/` are excluded |

The image is identified by a deterministic **image spec SHA-256** computed
locally from the base image string, the exact pip pin list, the remote repo
root and a `src/`+`runner/`+`modal_app/` source-tree hash
(`v6_shard_worker.image_spec_sha256`). Every raw record, shard metric and the
run manifest carry this value; the audit compares it. The Modal image object id
(`im-...`) is additionally recorded in `run_manifest.json` when the image has
been built.

Serialization / format versions in force:

* verifier records: plain JSON, canonical `json.dumps(..., sort_keys=True, default=str)`;
* SCG cache entries: `v6-scg-cache` format version 2, serializer `json-fraction-v2`
  (`src/scg/cache.py`), keyed on model hash + source SHA + builder SHA + semantics
  version + Python major/minor + serializer version;
* completion ledger / hash manifest lines: canonical JSON, one object per line.

### 1.2 Resources and limits

| Setting | Value | Notes |
|---|---|---|
| `cpu` | `1.0` | Modal `cpu=` is **physical cores**; one core per shard worker |
| `memory` | `4096 MiB` | starts here; Pilot RSS evidence (P95 ≈ 254 MiB, max 269 MiB on GitHub) leaves headroom; revisit if the Pilot reports more |
| Function timeout | `5400 s` | must exceed 69 × 60 s + startup/upload; 4140 s of cells max |
| Cell wall-clock cap | `60.0 s` | frozen per-task `timeout_s`; enforced by the worker, not by Modal |
| BLAS/OpenMP threads | `1` | `OPENBLAS_NUM_THREADS=OMP_NUM_THREADS=MKL_NUM_THREADS=NUMEXPR_NUM_THREADS=VECLIB_MAXIMUM_THREADS=1` |
| `PYTHONHASHSEED` | `0` | deterministic subprocess environment |
| Modal retries | `0` | only the orchestrator retries, only infrastructure failures |
| Containers | `single_use_containers=True` | one container per shard attempt; no SCG cache leakage across shards |
| Concurrency | `--max-parallel` (default `8`) | orchestrator never has more than N pending shard calls |
| SCG cache dir | `/tmp/scg_cache` | per container, reused only inside a shard |

The frozen formal layout is **120 shards × 69 tasks = 8,280 cells**; shards are
filled so that all tasks of one `expected_model_hash` stay in the same shard
(this is what makes the SCG cache effective for L-sweeps).

### 1.3 Frozen task inputs

* Formal: `protocol/v6_formal_manifest.json` + `protocol/v6_formal_tasks/shard_*.csv`
  (8,280 immutable rows; quota A=600, B=1,080, C=5,400, D=1,200).
* Pilot: `protocol/v6_modal_pilot_tasks.json` — 64 pre-registered cells
  (48 differential cells from audited GitHub runs + 16 difficulty cells) with an
  explicit frozen 6-shard `shard_plan`, plus audited reference status/verdict per cell.
  The builder uses the frozen shard plan verbatim and normalises `pilot_000` →
  `000` for volume paths.
* The orchestrator uploads the frozen source rows and shard manifest into the
  Volume before spawning any container, so a run is reproducible from the
  Volume alone.

## 2. Two different timeouts (do not conflate them)

1. **Algorithm cell cap = 60.0 s wall clock.** The worker runs
   `python runner/run_cell.py '<spec>'` as a child process in its own process
   group (`start_new_session=True`) and SIGKILLs the whole group when the cap
   expires. The record then becomes `execution_status=TIMEOUT_WALL_CLOCK` with
   `verdict=null`, populated from the last atomic checkpoint; stages that never
   executed keep `null` counters (never `0`). This is right-censoring, not a
   verdict and not an `INC`.
2. **Modal Function timeout = 5400 s.** If the container itself exceeds this
   (infrastructure problem — not expected: 69 × 60 s + overhead < 5400 s), Modal
   kills the invocation. The orchestrator treats the failed `.get()` as an
   infrastructure failure, retries the shard with `attempt+1` up to
   `--max-infra-retries` (default 2), and the per-task ledger makes the retry
   resume the unfinished tasks only.

There is also the orchestrator's polling interval (`--poll-seconds`, default
5 s) which is not a timeout.

## 3. Volume layout

Volume: `tii-v6-scalability` (configurable `--volume-name`), mounted at `/vol`.
Subtree prefix: `scalability_v6_formal` (configurable `--volume-prefix`).

```
/vol/scalability_v6_formal/
  protocol/
    shards/shard_XXX.csv          frozen execution rows (uploaded by orchestrator)
    source_tasks/shard_000.csv    frozen reference rows (all tasks; used by the audit)
    modal_shards_manifest.json    shard plan + SHA-256 of every shard CSV
    source_manifest.json          source protocol manifest (formal/pilot), if any
  run_metadata/
    run_<run_id>.json             frozen run config: source SHA, image spec SHA, shard SHA map,
                                  cell/function timeouts, resources, app/volume, kind
    attempts/shard_XXX_attempt_N.json   one marker per shard attempt (attempt id, invocation id)
  checkpoints/shard_XXX/<task_id>.json  last atomic verifier checkpoint of the current attempt
  raw/shard_XXX/<task_id>.json   exactly one final record per task (atomic write)
  completion_ledger/shard_XXX.jsonl     append-only task ledger (SHA-256, status, attempt, invocation)
  hashes/shard_XXX.jsonl         append-only raw-file hash manifest
  logs/shard_XXX.log             shard progress log
  metrics/shard_XXX.json         final shard totals + invocation id + image/source SHA + RSS/wall
```

Rules:

* one worker owns `raw/shard_XXX/`, `completion_ledger/shard_XXX.jsonl`,
  `hashes/shard_XXX.jsonl`, `logs/shard_XXX.log`, `checkpoints/shard_XXX/`,
  `metrics/shard_XXX.json`; different shards never write the same file;
* writes are `tmp` + `os.replace` (atomic); a raw record is visible only when
  complete, and its SHA-256 is appended to the ledger only after the record is
  durable;
* ledger lines use `O_APPEND` + `fsync`; the file is single-writer;
* the worker calls `Volume.commit()` after every task and at shard end so a
  killed/preempted container loses at most the current cell;
* `metrics/shard_XXX.json` is written last and is the shard-completion marker.

Record/provenance fields include: `task_id`, frozen task identity fields,
`model_hash`, `structure_hash`, `verdict`, `execution_status`,
`termination_status`, `failure_stage`, `counters` (null-correct), `wall_s`,
`peak_rss_mb`, `scg_cache_status/key/provenance`, `commit_sha` (= executed
source SHA), `run_id`, `attempt`, `task_attempt`, `invocation_id`,
`image_spec_sha256`, `wall_clock_cap_s`, `configuration_hash`,
`raw_record_hash`, and the v5/v6 compatibility aliases used by the analysis
scripts.

## 4. Status semantics, dedupe and resume

Final per-task statuses:

| status | meaning | retried? |
|---|---|---|
| `COMPLETED` | verifier finished; `verdict ∈ {VER, AMB, INC}` | never |
| `TIMEOUT_WALL_CLOCK` | 60 s cap killed the cell; `verdict=null` | never |
| `ERROR` | technical failure of the cell (subprocess/parse/validation) | yes, on a later shard attempt only |

Resume algorithm (`shard_worker.execute_shard`):

1. read `completion_ledger/shard_XXX.jsonl`;
2. for each task take the **last** entry; skip only if its status is final
   (`COMPLETED`/`TIMEOUT_WALL_CLOCK`), the raw record file exists and its
   SHA-256 equals the ledger entry; anything else is regenerated;
3. `ERROR` tasks are re-executed by the next attempt with a new
   `task_attempt`; the previous attempt stays in the ledger (attempt history is
   preserved, never double-counted);
4. a shard is `COMPLETED` only when every task has a final or error-free record
   and no task is unfinished; `INCOMPLETE` triggers an orchestrator `resume`
   (never an automatic retry of mathematical outcomes).

Orchestrator `resume --run-id ...` reads the remote shard manifest and metrics,
spawns only the shards whose metrics are missing / not `COMPLETED` / contain
errors / whose raw record count differs from the frozen shard, and uses
`max(previous attempt)+1` as the new attempt id.

## 5. Cost observation and budget guard

* Before and after every paid run the orchestrator calls the official CLI
  `modal billing summary --for 'this month' --json` (read-only) and stores
  `metered_cost`/`metered_cost_breakdown` before/after plus the metered-cost
  difference as an **estimate** (the monthly meter is not a per-run invoice).
  Only metered cost fields are kept; tokens, `.modal.toml` and credential
  material are never read, parsed, logged or committed.
* User authorization: **$10 Modal cap**, baseline metered `$23.58024702`
  (`work/v6_modal_budget/budget.json`).
* Live guard (`BudgetMonitor` in `scripts/run_v6_modal.py`, equivalent to
  `scripts/modal_budget_guard.py check`): after each finished shard and at
  most once per `--billing-check-seconds` (default 300 s) it computes
  `estimated_spend = max(metered_delta, task_wall × $/machine-hour)` and
  `projected_full_run = estimated_spend / tasks_processed × 8280 × 1.3`. It
  stops spawning **new** shards (running containers are allowed to finish) when
  `estimated_spend > $9.5` or (formal run) `projected_full_run > $9.5`, marks
  the remaining shards `BUDGET_STOPPED`, and records every event in
  `run_manifest.json`.
* Rates used for the plan estimate: `$0.0473/CPU-hour`, `$0.008/GiB-hour`
  (public list rates captured 2026-11-07; re-read `modal billing rates` before
  a paid run and update this document, not silently the code).

Plan for the formal matrix (60 s cap, 4 GiB, cpu=1): worst-case compute
`8,280 × 60 s = 138 CPU-hours ≈ $10.9`; expected (`18.8 s/cell` conservative
expectation from `docs/v6_gate_decision.md`) `43.2 CPU-hours ≈ $3.4`; wall
hours at `--max-parallel 8` are ~17.3 h worst / ~5.4 h expected. Egress,
volume storage and image build are excluded from the plan estimate. The $9.5
guard threshold is below the worst case on purpose: it stops the run instead of
crossing the authorized cap.

## 6. Pilot acceptance criteria

Pilot source: `protocol/v6_modal_pilot_tasks.json` (64 cells, 6 frozen shards;
`--pilot-shards N` can limit to the first N shards, default: all).

Pilot passes only if all of the following hold:

1. `python scripts/run_v6_modal.py simulate` passes locally (atomic write,
   SHA-256, ledger dedupe, resume of a deleted record, timeout/null semantics,
   and the audit detects planted missing/duplicate/corrupt data);
2. every planned Pilot shard has `metrics.status = COMPLETED`, 0 `ERROR`
   records, and `tasks_total` equal to the frozen shard size;
3. the independent audit passes (`scripts/audit_v6_modal_results.py`,
   `--allow-partial` for a limited shard run): one final record per frozen task,
   all frozen fields/hashes match, ledger reconciliation clean, source SHA and
   image spec SHA match;
4. portability against the audited GitHub references: every cell whose
   reference status is `COMPLETED` and whose Modal status is `COMPLETED` must
   have the same verdict; reference-COMPLETED→Modal-TIMEOUT differences are
   reported as `modal_regression` findings (they are not silently accepted);
5. observed RSS stays within the 4 GiB reservation (expect ≪ 1 GiB based on the
   GitHub audits: P95 ≈ 254 MiB, max ≈ 269 MiB);
6. measured metered-cost delta and the formal projection stay well below the
   `$9.5` guard threshold; the Pilot's 64 cells are budgeted at ≪ $1;
7. no token/secret appears in any artifact (the audit does not read
   credentials; the orchestrator stores only metered cost fields).

Recommended commands (run with the Modal client interpreter; see §10):

```bash
python scripts/run_v6_modal.py plan --dry-run --pilot
python scripts/run_v6_modal.py simulate
python scripts/run_v6_modal.py pilot --confirm-budget --max-parallel 8
python scripts/audit_v6_modal_results.py --download-dir work/v6_modal_pilot_<run_id>
```

The formal run is only started after the Pilot acceptance report and an
independent audit of the Pilot are accepted by the Lead (and only with
`--confirm-budget`).

## 7. Local self-test (free, no Modal resources)

`python scripts/run_v6_modal.py simulate` creates a fake Volume under
`/tmp/v6_modal_simulation/vol/scalability_v6_formal`, selects
`--tasks` (default 12) frozen cells from the pilot list including at least one
known-heavy cell, executes 2 shards with a shortened `--cell-timeout`
(default 3 s) and then asserts:

* one atomic raw record per task, no `*.tmp` leftovers;
* `raw_record_hash` recomputation matches;
* one ledger and one hash entry per final task, file SHA-256 matches;
* timeout path exercised: `TIMEOUT_WALL_CLOCK` records have `verdict=null`;
* re-running a finished shard executes 0 tasks and appends 0 ledger lines;
* deleting one raw record + its ledger line and re-running the shard executes
  exactly that task and skips the rest (no duplicate entries);
* the audit script returns exit 0 on the clean download;
* the audit detects every planted fault with a non-zero exit code:
  missing record, duplicated ledger line, corrupted record (hash mismatch),
  duplicated final entry.

The simulation runs `runner/run_cell.py` with the local Modal client
interpreter (its numpy/scipy) — not with the pinned image — so it validates the
infrastructure, not numerical reproducibility. Container-level reproducibility
is covered by the Pilot reference-verdict comparison and the recorded image
spec SHA.

## 8. Modal SDK 1.6.3 API confirmation

Confirmed by inspecting the installed client (`modal` 1.6.3) in the TII venv:

| API | Confirmed signature / behaviour |
|---|---|
| `modal.Image.debian_slim(python_version=...)` | returns an `Image`; `3.13` is passed through to `python:3.13-slim` for the current image-builder version |
| `Image.pip_install(*packages)` | accepts a list of exact pins, run at build time |
| `Image.add_local_dir(local_path, remote_path, *, copy=False, ignore=...)` | `ignore` accepts a predicate `Callable[[Path], bool]` (relative path); `copy=False` mounts the snapshot at container start |
| `Image.build(app)` / `Image.object_id` | builds/reuses the image and gives the Modal image id (`im-...`) for the run manifest |
| `modal.Volume.from_name(name, *, create_if_missing=False)` | returns a volume handle; `create_if_missing` only used by the orchestrator |
| `Volume.commit()` | commits this container's writes so they become visible to other containers / the orchestrator |
| `Volume.reload()` | refreshes the local view before listing/downloading |
| `Volume.batch_upload(force=...)` | context manager with `put_file(local, remote)` / `put_directory(...)` |
| `Volume.listdir(path, *, recursive=False)` | returns `FileEntry` list (`type == FileEntryType.FILE`) |
| `Volume.read_file(path)` | generator of bytes chunks |
| `App(name).run()` | sync context manager; spawn/`get` calls must happen inside it |
| `Function.spawn(*args)` | returns `FunctionCall` with `.object_id` (call id) and `.get(timeout=...)` (`timeout=0` polls, raises builtin `TimeoutError` while running) |
| `modal.current_function_call_id()` | current invocation id inside the container |
| `app.function(..., cpu=, memory=, timeout=, volumes=, retries=, single_use_containers=)` | `cpu` is physical cores; `memory` MiB; `timeout` seconds |

Uncertainties / limits accepted in this design:

* the exact image digest (`im-...`) is only known after the image is built, so
  the deterministic image-spec SHA-256 is the primary environment identity and
  the Modal image object id is recorded as secondary provenance;
* Modal memory billing is assumed to follow reserved memory (4 GiB), matching
  the public rate calculation; the plan estimate is conservative;
* monthly billing summary granularity can make the metered-cost delta lag or
  round; the guard therefore also uses a compute-time estimate and a 1.3 safety
  factor, and the run manifest labels the delta as an estimate;
* `Volume.commit()` is called per task (~120 commits per shard); this is
  deliberate for checkpoint granularity but adds a small fixed cost per cell
  and is included in the worst-case estimate;
* a budget stop only prevents *new* shards: already-running shards finish and
  are recorded; the remaining shards are reported as `BUDGET_STOPPED` and can
  be resumed later with the same `run_id`.

## 9. Hard rules

* Never retry `TIMEOUT_WALL_CLOCK` or `INC` as if they were infrastructure
  failures; never convert a timeout into a verdict or into `INC`.
* Never write `0` for a stage that did not execute; use JSON `null`.
* Never overwrite an existing downloaded evidence directory unless `--force`
  is passed for the same run id; never copy v5 or Pilot records into a formal
  result set.
* Never start a paid run without `--confirm-budget`; never exceed the
  authorized cap; never print/commit Modal tokens or `.modal.toml` content.
* Freeze the source commit before a paid run: a dirty tree is reported in the
  plan and in the run manifest (`source_tree_dirty`) and must be cleaned or
  accepted explicitly by the Lead.

## 10. How to run (interpreters)

* Local orchestration and simulation: use the Modal client venv (it has
  `modal` 1.6.3, numpy and scipy):

  ```bash
  V6PY="/Users/lucas/Library/Mobile Documents/com~apple~CloudDocs/WorkingFiles/在投文章/TII_TPN/02_experiments/firing_interval_adjustment/.venv/bin/python"
  "$V6PY" scripts/run_v6_modal.py plan --dry-run --formal
  "$V6PY" scripts/run_v6_modal.py simulate
  ```

  `scripts/run_v6_modal.py` also re-executes itself with `$V6_MODAL_PYTHON` /
  the discovered client venv when a paid command is invoked with an
  interpreter that has no Modal SDK.
* Do not install Modal into `/tmp/v6work/.venv` (that venv is the local
  verifier environment with numpy/scipy).
* Inside the Modal image only the exact pins from `requirements-modal.lock`
  are installed; the local `.venv` is never copied into the image.
