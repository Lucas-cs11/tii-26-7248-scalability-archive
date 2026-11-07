#!/usr/bin/env python3
"""Generate the minimal Section V-C / S7-B revision suggestions for v6.

Reads the audited statistics JSON (never the raw records for numbers), the
independent audit summary and the frozen protocol, and writes replacement-only
suggestion documents. The v17 manuscript is not modified.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(path: Path, default=None):
    if not path.is_file():
        return default
    return json.loads(path.read_text())


def fmt(value, digits=1):
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--analysis", type=Path, default=ROOT / "work/v6_analysis_formal/statistics.json")
    parser.add_argument("--audit", type=Path, default=ROOT / "work/v6_formal_audit/audit.json")
    parser.add_argument("--protocol", type=Path,
                        default=ROOT / "protocol/scalability_v6_frozen_protocol.json")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "docs")
    args = parser.parse_args()
    stats = load(args.analysis, {}) or {}
    audit = load(args.audit, {}) or {}
    frozen = load(args.protocol, {}) or {}
    if not stats:
        raise SystemExit(f"missing statistics: {args.analysis}")
    dataset = stats.get("dataset", {})
    denom = stats.get("denominators", {}).get("by_experiment", {})
    integrity = stats.get("integrity", {})
    cost = stats.get("cost_accounting", {})
    groups = stats.get("groups", {})
    cells = dataset.get("tasks")
    status = "FORMAL" if str(dataset.get("analysis_kind", "")).lower().startswith("formal") else "PILOT-SELFCHECK — NOT FORMAL DATA"
    rows = []
    for name in ("A_structural", "B_wip", "C_Lsweep", "D_difficult"):
        bucket = denom.get(name, {})
        rows.append(
            f"| {name} | {bucket.get('n_total', 'n/a')} | {bucket.get('n_completed', 'n/a')} | "
            f"{bucket.get('n_timeout', 'n/a')} | {bucket.get('n_inc', 'n/a')} | "
            f"{bucket.get('n_ver', 'n/a')} / {bucket.get('n_amb', 'n/a')} |"
        )
    table = "\n".join(rows)
    vc = f"""# Minimal proposed revision for Section V-C (v6)

Status marker: **{status}**. This is a replacement suggestion only; it is not
applied to the v17 manuscript or to any approved blue paragraph.

> **Scalability (v6 execution).** Figure 6 reports the frozen v6 matrix
> (`{frozen.get('protocol', 'scalability-v6-frozen-protocol-1')}`, {cells} cells,
> {dataset.get('cell_timeout_s', 60)} s per-cell wall-clock cap, 50,000
> state-class cap) executed entirely on Modal CPU workers; the independent
> audit reported {audit.get('unique_tasks', audit.get('tasks', 'n/a'))} unique
> task records with {audit.get('missing', 0)} missing and
> {audit.get('duplicates', 0)} duplicate records. Points marked at the cap are
> right-censored wall-clock timeouts and are not treated as zero-runtime
> observations or as verdicts. Completed-but-inconclusive cells are reported
> separately from timeouts.

| experiment | total | completed | timeout | INC | VER / AMB |
|---|---:|---:|---:|---:|---:|
{table}

Reproduce these numbers from `07_statistical_analysis/statistics.json`; the
denominators are explicit and time quantiles use COMPLETED cells only.

Suggested caption:

> **Fig. 6.** Bounded-verification cost of the frozen v6 matrix. Panels report
> right-censored runtime (triangles at the cap), conclusive/INC/timeout
> fractions with explicit denominators, and the L-sensitive versus plateau
> structure of the observable-word sweep. Timeouts are censored and never
> imputed as completions.
"""
    s7b = f"""# Minimal proposed revision for Supplementary S7-B (v6)

Status marker: **{status}**. Guidance only; not inserted into the manuscript
source.

> **S7-B. Scalability protocol, environment and audit (v6).** The protocol was
> frozen before execution (`{frozen.get('protocol', 'scalability-v6-frozen-protocol-1')}`,
> manifest SHA-256 `{(frozen.get('task_grid') or {}).get('manifest_sha256', 'n/a')}`)
> and executed only on Modal CPU Functions with a pinned container image
> (Python {((frozen.get('execution') or {}).get('python'))}, NumPy
> {((frozen.get('execution') or {}).get('numpy'))}, SciPy
> {((frozen.get('execution') or {}).get('scipy'))}, Modal
> {((frozen.get('execution') or {}).get('modal_sdk'))}) and a per-task wall-clock
> cap of {((frozen.get('execution') or {}).get('task_timeout_seconds'))} s;
> container start-up, image pulls and Volume uploads are excluded from that cap
> and reported as cloud overhead. Each task writes one atomic record plus a
> ledger entry with a byte-level SHA-256; interrupted containers resume from the
> ledger without re-running completed tasks. The independent audit reproduced
> {audit.get('unique_tasks', 'n/a')} unique records with
> {audit.get('missing', 0)} missing and {audit.get('duplicates', 0)} duplicates.
>
> Status fields are orthogonal. `execution_status=TIMEOUT_WALL_CLOCK` means the
> external cap expired before analysis completed; `verdict=INC` is emitted only
> after a completed run reports an internal limit (`SCG_STATE_LIMIT` or a
> bounded-analysis limit); `VER`/`AMB` carry certificates. All three are
> reported separately and never merged.
"""
    mapping = f"""# Reviewer-issue mapping — v6 evidence

| Issue | v6 resolution | Evidence |
|---|---|---|
| Timeout treated as zero work | timeouts are right-censored at the cap and reported as their own denominator | `07_statistical_analysis/statistics.json`, `failure_stage_distribution` |
| INC conflated with timeout | INC is completed-but-inconclusive with `inconclusive_reason` | audit + statistics |
| Missing provenance | every record carries source SHA, image-spec SHA, Modal invocation id, model hash, task id | `04_raw_8280/`, `06_independent_audit/` |
| Selective retention | the frozen manifest has {frozen.get('task_grid', {}).get('tasks', 'n/a')} tasks; the audit checks uniqueness and missing/duplicates | `01_frozen_protocol/`, `06_independent_audit/` |
| Budget transparency | expected {fmt(cost.get('completed_wall_sum_s'))} s completed wall + {fmt(cost.get('n_cells_at_cap'))} capped cells; Modal billing audit included | `03_modal_cost_and_resource_audit/` |
| Semantic equivalence of the optimised verifier | differential v2 accepted (`DIFFERENTIAL_V2_ACCEPTED = GO`), SCG fingerprints exact | `10_reproducibility/` |
"""
    out = args.out_dir
    out.mkdir(parents=True, exist_ok=True)
    (out / "v6_section_vc_suggestions.md").write_text(vc)
    (out / "v6_s7b_suggestions.md").write_text(s7b)
    (out / "v6_reviewer_mapping.md").write_text(mapping)
    print(json.dumps({
        "analysis": str(args.analysis),
        "status_marker": status,
        "outputs": [str(out / name) for name in
                    ("v6_section_vc_suggestions.md", "v6_s7b_suggestions.md", "v6_reviewer_mapping.md")],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
