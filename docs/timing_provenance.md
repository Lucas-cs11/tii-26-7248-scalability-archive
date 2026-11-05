# Timing and execution provenance

## Pilot

- Repository: `Lucas-cs11/tii-26-7248-scalability-v2`
- Branch: `scalability-v4-final`
- Run ID: `42004602864`
- Commit: `0a98dd2`
- Result: 99/99 tasks, 30/30 shards, successful.
- Purpose: acceptance of model semantics and consecutive L frontiers; selection
  of a uniform formal WIP timeout before the formal matrix was frozen.

A later duplicate pilot trigger (`42005549605`) was cancelled and contributes
no record or timing to the formal data.

## Formal run

- Run ID: `42040753991`
- Commit: `c98c75b`
- Workflow: `.github/workflows/scalability_v4_formal.yml`
- Runner declaration: GitHub-hosted `ubuntu-22.04`.
- Result: 60/60 shard jobs successful; 465 formal records recovered.
- Per-task budgets: 30 s for WIP and 60 s for structural/L-sweep tasks.
- Repetitions: three per model/seed/bound cell.

The `runtime_s` and `wall_s` values in the formal merged data originate only
from this run. Pilot timings, local validation timings, v3 timings, and the
cancelled duplicate run are not merged into any Figure 6 statistic.

## Interpretation

The workflow-level success means every shard completed and uploaded its raw
record file. Individual external timeouts remain valid recorded outcomes.
They are right-censored at the declared per-task budget, carry no verification
verdict, and are never converted to zero. The independent 50,000 SCG-class
limit may instead yield a completed process with verdict INC and an explicit
reason; this is distinct from an external timeout.
