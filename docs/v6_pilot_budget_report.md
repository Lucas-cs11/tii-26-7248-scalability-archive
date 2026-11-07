# v6 Pilot audit and budget decision

## Integrity

GitHub Actions run `42323424751` executed commit
`7b9ff58ef768c9cf4d642c095f36cb9fb23fa829` on 24 standard
`ubuntu-22.04` shards with `max-parallel: 6`. All 25 jobs succeeded. The
independent audit found exactly 288 unique final records, 232 model hashes and
280 bounded problems, with no missing shard, duplicate configuration, hash
mismatch, ERROR, or missing Peak RSS sample.

## Observed results

| Experiment | Tasks | Completed | Timeout | INC | Wall P50 | Wall P90 | Wall P95 | RSS P95 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Structural/observation | 120 | 63 | 57 | 1 | 16.766 s | 60.091 s | 60.098 s | 169.078 MiB |
| WIP | 72 | 23 | 49 | 5 | 60.035 s | 60.097 s | 60.102 s | 251.945 MiB |
| L sensitivity | 72 | 40 | 32 | 0 | 8.934 s | 60.093 s | 60.099 s | 167.680 MiB |
| Difficult | 24 | 2 | 22 | 2 | 60.085 s | 60.111 s | 60.113 s | 253.965 MiB |

Overall there were 128 completed evaluations, 160 external wall-clock
timeouts, and 8 completed `INC` evaluations. Conclusive outcomes were 58 VER
and 62 AMB. Maximum observed Peak RSS was 269.410 MiB. Raw artifact storage was
1.63 MB after download.

The workflow consumed 183.05 runner-minutes including validation, dependency
setup, execution and upload. Per-cell compute/wait time summed to 174.46
minutes. At the current 60-second cell cap, the 8,280-cell formal matrix has a
compute-only worst case of 8,280 runner-minutes (138 hours), before setup,
validation, upload and retry reserve.

## Censoring and stage diagnosis

The Pilot timeout fraction is 55.6%. WIP=2 timed out in 24/24 cells; WIP=3 had
19 timeouts and 5 SCG-limit `INC` outcomes. Difficult cases timed out in 22/24
cells. Within the L Pilot, low/mid/high cells timed out in 6/24, 12/24 and
14/24 cases respectively. These are retained as computational-boundary
evidence and are not converted to zero work.

Thirty-two timeouts stopped during SCG construction, 126 after SCG construction
while entering synchronizer work, and two during partial history analysis.
Consequently, SCG caching can reduce repeated construction for fixed-model
L-scans, but cannot by itself resolve the dominant synchronizer censoring.

## Instrumentation finding

The immutable raw artifacts retain 126 `SCG_COMPLETE` timeout checkpoints in
which later, unexecuted counters were serialized as zero. The independent
audited view replaces these values with `null` and records the transformation.
The runner is corrected before any later run. Two partial-history checkpoints
also lacked cumulative expansion/LP timing; the verifier now updates these
timers before checkpointing.

## Gate decision

`PILOT_BUDGET_AND_MEMORY_PASS = false` for the current execution design.

Memory and artifact storage are acceptable, and GitHub emitted no policy or
resource warning. The blocker is the combination of 55.6% right-censoring and
an 8,280-run worst-case compute budget of 138 hours. The formal protocol is not
frozen and the formal run is not started.

The next permitted work is correctness-preserving reuse of identical-model SCG
construction, task colocation by model, and a focused differential performance
validation. Any synchronizer optimization must have a proof-preserving rule and
differential regression evidence; otherwise the remaining timeout boundary is
reported rather than hidden.
