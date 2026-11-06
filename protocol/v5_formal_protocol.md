# IEEE TII Scalability Benchmark v5 — Frozen Formal Protocol

This protocol is frozen before the formal GitHub Actions run. The v5 pilot
(run `42052597272`) was used only to verify semantics, resource limits,
checkpoint behavior, and the need for a broader consecutive-L interval. No
pilot outcome was used to remove a family, seed, L value, or difficult case.

The formal matrix contains 8,280 configurations and 7,380 distinct bounded
`(model, L, H)` problems over all 12 declared model families. Experiment A has
12 controlled structural levels with 50 independent seeds per level (600
cells). Experiment B has 12 structure groups, 30 independent fixed models per
group, and three paired WIP levels (1,080 bounded problems); no timing repeat is
counted toward this total. Experiment C has 300 fixed TPNs and 15 consecutive L
levels (4,500 FIRST_CERTIFICATE cells), plus 900 preregistered complete-set
pairings at evenly spaced seeds 0, 6, 12, 18, and 24. Experiment D has 1,200
monotonically declared difficulty cells. Every C sweep holds the TPN and H fixed.

Within each A level and B structure group, seed candidates are considered in
ascending integer order. A candidate is rejected only when its generated full
TPN hash duplicates an already accepted model in that stratum; scanning stops
at the declared count. This deterministic pre-execution deduplication rule does
not inspect verifier outcomes.

Every one of the 3,180 declared model identities is regenerated before the
formal run and checked structurally. A constructive untimed witness then fires
the normal processing path to task completion, reaches every seeded fault and
its recovery from an operation-start marking, fires every alternative route,
and fires every positive-time rework cycle before completion. For each B model,
the validator additionally fires WIP distinct part-release transitions from the
initial marking and verifies the reached in-process count. Thus the reported
WIP value is witnessed from the generated net rather than copied from a model
attribute. Experiment A is checked for its declared one-factor controls and
strictly increasing measured `|P|+|T|` ranges within each subseries.

Formal execution uses only standard GitHub-hosted `ubuntu-22.04` runners in the
public repository. Every cell has an independent subprocess wall-clock limit;
algorithmic INC, wall-clock timeout, runner error, and conclusive VER/AMB are
separate statuses. Timeout records retain checkpointed partial workload and
must never be plotted as zero work. All declared cells remain in denominators
and raw data, irrespective of outcome.

The committed manifest and 120 task CSV files are regenerated during the
workflow validation job; any difference fails the run before experiments begin.
The independent audit checks task coverage, hashes, provenance, status
semantics, fixed-model C sweeps, paired WIP structures, and semantic validation.
Failed or missing shards are retried only through the dedicated retry workflow,
which restores the exact prior shard artifact and resumes by task ID. Successful
validated shards are not included in its explicitly supplied shard list. Each
retry writes a shard-attempt supersession manifest. Before auditing, the
selection script produces one machine-readable choice per shard; the auditor
loads only those 120 selected artifacts and accepts records only from the
authorized formal/retry run IDs.
