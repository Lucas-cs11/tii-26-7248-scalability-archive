# Minimal proposed revision for Supplementary S7-B

The following text is guidance only and has not been inserted into the source
manuscript.

> **S7-B. Scalability protocol and audit.** The v4 matrix was frozen after a
> separately labelled pilot and before the formal GitHub Actions run. The pilot
> was used only to locate consecutive \(L\)-frontiers and to set a uniform 30 s
> budget for the WIP experiment; formal cells were not filtered by outcome.
> The formal matrix contains 465 tasks in 60 shards: 105 structural-size, 135
> WIP, and 225 \(L\)-sweep tasks. Every cell uses seeds 0--4 and three repeats.
> Structural and \(L\)-sweep tasks use a 60 s external budget, whereas WIP tasks
> use 30 s; all use an independent internal limit of 50,000 SCG classes.
>
> Status fields are orthogonal. `execution_status=TIMEOUT` means the external
> wall-clock budget expired, so analysis was not completed and no verdict was
> assigned. `verdict=INC` is emitted only after a completed process reports an
> internal analysis limit, with `inconclusive_reason` identifying the limit.
> The formal run produced 409 completed tasks and 56 external timeouts; among
> completed tasks, 405 were conclusive and four were INC because of
> `SCG_STATE_LIMIT`. Repeated runs reproduced model hashes and deterministic
> workload counters exactly.
>
> The generator adds seeded, reachable, acyclic post-process inspection forks.
> This preserves the original topology when the inspection count is zero and
> introduces genuine observable histories at multiple consecutive \(L\) values
> when enabled. For every formal fixed model, the model hash remains constant
> throughout its \(L\)-sweep. A separate untimed state exploration verifies
> that WIP bounds 1--3 achieve the corresponding reachable concurrency.
>
> The archive contains every task specification and raw shard artifact, the
> source snapshot and Git history bundle, GitHub run/job metadata, merged data,
> audit and plotting scripts, vector figures, and a SHA-256 manifest covering
> all package files.
