# Suggested Section V-C revision — v5 results

This is a proposed replacement for the scalability discussion only. It does
not alter the approved v17 manuscript or any approved blue passage.

> **Scalability.** We evaluated the verifier on a frozen, synthetic v5
> benchmark executed on standard GitHub-hosted Ubuntu 22.04 runners. The formal
> matrix comprises 8,280 parameter configurations, 7,380 distinct bounded
> verification problems, and 3,160 distinct generated TPN hashes across 12
> declared structural families. It contains 600 controlled structural-size
> configurations, 1,080 paired WIP configurations, 4,500
> FIRST_CERTIFICATE L-sweep configurations, 900 preregistered COMPLETE_SET
> pairings, and 1,200 difficult configurations. All cells are retained: 8,166
> completed and 114 reached their independent wall-clock limit. The latter are
> reported as censored execution outcomes, not as zero workload or verification
> verdicts.
>
> The three structural subseries have strictly increasing measured
> |P|+|T| ranges of 77--257 (operation depth), 79--316 (part count), and
> 107--116 (alternative-route density). Their outcomes illustrate that static
> size alone does not determine workload: resource/route alternatives increase
> completed runtime while some larger instances encounter the state-class
> limit. For the paired WIP study, a constructive reachability witness confirms
> the declared in-process counts for every model. Completion was 343/360 for
> WIP=1, with 17 external timeouts; all WIP=2 and WIP=3 cells completed but
> terminated internally as INC at the SCG limit. Completed-runtime medians
> were 3.32 s, 18.65 s, and 32.42 s, respectively; these runtime summaries are
> separate from completion rates and do not assign times to timeouts.
>
> In the complete-set L analysis, 21 of 60 preregistered fixed models exhibit
> at least one retained-history growth frontier, yielding 126 frontiers in
> total. The remaining models, including early plateaus and resource-limited
> cases, remain in the released data and denominators. FIRST_CERTIFICATE and
> COMPLETE_SET measurements are reported separately because early termination
> changes the observed runtime and LP workload. These synthetic measurements
> characterize the declared benchmark generator and implementation; they do
> not establish an asymptotic complexity bound.

Suggested Figure 6 caption when the separately requested drawing step is run:

> **Fig. 6.** Scalability under the frozen v5 protocol. (a) Paired WIP results:
> completed-runtime median--P95 is shown separately from the all-cell outcome
> denominator. (b) Controlled structural subseries retain their own size axes
> and status counts. (c) COMPLETE_SET retained-history workload over fixed-TPN
> consecutive L sweeps; all declared levels, plateaus, INC outcomes, and
> right-censored timeouts are retained. FIRST_CERTIFICATE metrics are not mixed
> with complete-set workload.
