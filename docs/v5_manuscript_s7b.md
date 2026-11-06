# Suggested Supplementary S7-B revision — v5 protocol and audit

This text is guidance only and has not been inserted into the manuscript.

> **S7-B. Frozen benchmark, execution semantics, and audit.** A pilot was
> completed before the formal matrix was frozen. The pilot was used for
> generator semantics, checkpoint behavior, and resource-budget assessment; it
> was not used to remove families, seeds, L levels, or unfavorable outcomes.
> The formal matrix contains 8,280 configurations and 7,380 distinct bounded
> problems over 12 synthetic TPN families. Experiment A has 12 controlled
> levels with 50 hash-deduplicated seeded models per level. Experiment B has
> 12 structure groups, 30 independent fixed models per group, and WIP 1/2/3.
> Experiment C has 300 fixed TPNs with 15 consecutive L values plus a
> preregistered 60-model complete-set subset. Experiment D has 1,200 declared
> difficult configurations. No extra timing repetitions are counted as new
> configurations.
>
> Before execution, every declared model identity was regenerated and
> structurally validated. Constructive untimed witnesses establish a normal
> completion path, reach every seeded fault/recovery, reach each alternative
> route and positive-time rework cycle, and prove the WIP release witnesses.
> The formal workflow regenerates the frozen manifest and task CSVs, validates
> all model identities, installs hash-locked dependencies, pins Actions by
> commit SHA, and records run/job/shard/artifact provenance in every raw record.
>
> `TIMEOUT_WALL_CLOCK` denotes expiry of the independent subprocess budget and
> has no verdict or runtime. Its checkpointed counters are explicitly marked
> partial. `INC` denotes a completed verifier invocation that reached its
> internal inconclusive condition; it is not merged with timeout. Any ERROR or
> validation failure is retained as a separate attempt and is never silently
> made final. The v5 formal run produced 8,166 completed records and 114
> wall-clock timeouts, with 1,787 VER, 2,088 AMB, and 4,291 INC results.
>
> Independent post-run validation checks all task identities, 120 raw shards,
> SHA-256 record/configuration hashes, status semantics, model and source
> provenance, paired-WIP static structure, fixed-model L sweeps, semantic
> validation artifacts, and frozen-manifest identity. The audit passed for all
> 8,280 records. The released archive separately includes a recovered frozen
> 36,450-evaluation background dataset: it has 5,589 distinct bounded
> problems, and its proposed rerun timings and archived MSCG-LP reference
> timings are explicitly not pooled.
