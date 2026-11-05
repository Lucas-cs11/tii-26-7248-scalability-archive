# Minimal proposed revision for Section V-C

The following is a replacement suggestion only. It has not been applied to
the v17 manuscript or to any approved blue paragraph.

> **Scalability.** Figure 6 reports the frozen v4 benchmark executed solely on
> GitHub-hosted Ubuntu 22.04 runners. Each formal cell contains five generated
> instances and three repetitions per instance. Figure 6(a) shows median and
> P95 runtime for increasing TPN size at WIP bounds 1--3; points marked
> \(\ge 30\) s are right-censored external timeouts and are not treated as
> zero-runtime observations or verification verdicts. All WIP=1 cells were
> conclusive, while deeper routes at WIP=2/3 crossed the 30 s execution budget.
> Figure 6(b) therefore reports the conclusive fraction for the complete
> \(3\times3\) route-depth/WIP grid (15 runs per cell), explicitly separating
> 56 external timeouts from four completed but internally inconclusive runs
> caused by the 50,000-state-class limit. An untimed reachability check confirms
> that bounds 1, 2, and 3 admit reachable concurrency levels of 1, 2, and 3.
> Figure 6(c) varies only the observable-word bound \(L\) on fixed TPNs. The
> COMPLETE_SET workload grows over consecutive history frontiers (L1: 8--11;
> L2: 9--11) and then plateaus when no longer observable histories exist. Thus,
> WIP controls state-space pressure while \(L\) controls retained-history depth;
> the measurements characterize these benchmark families and do not imply a
> polynomial-time bound.

Suggested caption:

> **Fig. 6.** Scalability of bounded verification under the frozen v4 protocol.
> (a) Runtime median--P95 versus TPN size for WIP bounds 1--3; triangles are
> right-censored 30 s timeouts. (b) Conclusive fraction over the full
> route-depth/WIP grid, with external timeouts (TO) and internal state-limit
> inconclusives (INC) reported separately. (c) Median retained COMPLETE_SET
> trace records over five fixed-model seeds, with min--max bands; all declared
> \(L\) levels are shown, including post-frontier plateaus.
