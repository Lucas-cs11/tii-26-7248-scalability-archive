# Reviewer-issue mapping

| Prior issue | v4 resolution | Evidence |
|---|---|---|
| Unreachable machine configurations | Generator paths are seeded and reachable; a separate state exploration validates completion and concurrency. | `06_validation/reachable_concurrency.json`, generator tests |
| WIP=3 ineffective with two parts | Formal WIP models use three parts and the semantic probe reaches 1/2/3 simultaneous in-process parts for WIP 1/2/3. | Figure 6(a,b), reachable-concurrency evidence |
| L-sweep plateaued almost immediately | Reachable acyclic inspection forks create three or four consecutive genuine history frontiers on fixed models. | `04_merged_results/validation.json`, Figure 6(c) |
| L/H or topology changed during structural comparison | Structural sizes use declared S1--S7 cells with strictly increasing \(|P|+|T|\); each L-sweep keeps one model hash fixed while only L changes. | frozen tasks and validation |
| Runtime provenance was mixed or unclear | Formal timing is exclusively from one GitHub Actions run on `ubuntu-22.04`; no local or cross-run timing is merged. | run metadata and timing provenance |
| INC work was plotted as zero | TIMEOUT, INC, and conclusive results are separate. Figure 6 uses censoring and conclusive fractions; no incomplete path is assigned zero runtime/work. | Figure 6(a,b), plotting source |
| Conflicting PASS flags | One independent release gate recomputes task identity and status semantics; its `PASS` is true only when all checks pass. | `06_validation/validation.json` |
| Partial SHA coverage | `SHA256SUMS` covers every regular file in the final package except itself, with a post-build verification pass. | `SHA256SUMS`, package audit |
| Missing raw shards and manuscript guidance | All 30 pilot and 60 formal shard artifacts are included, together with focused V-C/S7-B suggestions. | package directories `02`, `03`, and `08` |

## Transparent residual limitations

- The formal WIP grid exposes a sharp timeout boundary, not a densely resolved
  smooth transition. The figure reports that boundary without interpolation.
- The 30 s and 50,000-class limits are experimental resource limits, not model
  properties. Timeout and INC counts must therefore accompany any comparison.
- The measurements support empirical scalability claims for the declared
  generator families and implementation only; no asymptotic claim is made.
