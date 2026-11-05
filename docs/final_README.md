# IEEE TII Scalability Benchmark v4 — final audited results

This package is the reproducible record for the v4 scalability rerun of
*Bounded Task-Completion Diagnosability of Time Petri Nets: Verification and
Firing-Interval Adjustment*. It does not modify the v17 manuscript or any
previously approved text.

## Result at a glance

- Formal GitHub Actions run: **42040753991** (`success`), 60/60 shard jobs.
- Frozen formal matrix: 465 tasks = 105 structural-size + 135 WIP + 225 L-sweep.
- Completeness: 465 records, 465 unique task IDs, no missing, unexpected, or
  duplicate records.
- Execution: 409 completed and 56 external timeouts.
- Analysis: 405 conclusive, 4 internally inconclusive, and 56 not run because
  of the external timeout.
- Verdicts: 105 VER, 300 AMB, 4 INC, and 56 without a verdict.
- All four INC cases have the explicit reason `SCG_STATE_LIMIT`; no timeout is
  relabelled as INC or assigned a verdict.
- Reachability probe confirms that WIP bounds 1, 2, and 3 admit reachable
  markings with exactly 1, 2, and 3 parts in process, respectively.

`06_validation/validation.json` is the machine-readable release gate. The
release is PASS for completeness, identity, status semantics, repeat
invariants, L-sweep sensitivity, and monotone structural x-coordinates.

## Directory map

- `01_protocol/`: frozen pilot/formal task grids, manifests, and workflows.
- `02_pilot_raw/`: all 30 downloaded pilot shard artifacts.
- `03_formal_raw/`: all 60 downloaded formal shard artifacts.
- `04_merged_results/`: independently reconstructed JSONL/CSV and validation.
- `05_source/`: separate exact archives for the formal-run commit and release
  commit, a Git bundle, commit metadata, and reproduction scripts.
- `06_validation/`: release validation, reachable-concurrency evidence, run
  metadata, and timing provenance.
- `07_figure6/`: combined and separate panels in PDF/SVG/EPS/PNG.
- `08_manuscript_guidance/`: minimal Section V-C and Supplementary S7-B text
  suggestions plus a reviewer-issue mapping. These are suggestions only.
- `SHA256SUMS`: SHA-256 for every package file except the manifest itself.

## Reproduce the audit and figure

From the extracted package root, run (with NumPy and Matplotlib available):

```text
python3 05_source/reproduction_scripts/audit_v4_results.py \
  --tasks 01_protocol/v4_formal_tasks \
  --artifacts 03_formal_raw \
  --out reproduced_results

python3 05_source/reproduction_scripts/plot_figure6_v4.py \
  --records reproduced_results/merged_results.jsonl \
  --out reproduced_figure6
```

The plotted data are the full preregistered formal cells used by each panel;
no seed, repeat, or L level was selected after observing the outcome.

## Scientific limitations

The WIP experiment identifies a sharp computational boundary under the frozen
30 s per-task budget rather than a smooth transition: the WIP=1 cells finish,
whereas deeper routes at WIP=2/3 are dominated by right-censored timeouts. The
L experiment intentionally retains the post-frontier plateaus. These empirical
results demonstrate sensitivity of this implementation and benchmark family;
they do not establish an asymptotic complexity bound.
