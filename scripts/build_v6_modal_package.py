#!/usr/bin/env python3
"""Assemble the final Modal full-scale deliverable package.

Collects provenance, the frozen protocol, the Modal environment record, cost and
resource audit, all 8,280 raw records, checkpoint/ledger evidence, the
independent audit, statistics, Figure 6 (four formats), paper material and the
reproducibility pointers into a deterministic staging tree, writes
``MANIFEST.sha256``, zips it and re-opens the archive to verify every hash.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_tree(source: Path, destination: Path) -> int:
    if not source.exists():
        return 0
    count = 0
    for path in sorted(source.rglob("*")):
        if path.is_dir():
            continue
        target = destination / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        count += 1
    return count


def copy_file(source: Path, destination: Path) -> bool:
    if not source.is_file():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return True


def load(path: Path, default=None):
    if not path.is_file():
        return default
    return json.loads(path.read_text())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True,
                        help="downloaded Modal run directory (raw/, metrics/, ledgers)")
    parser.add_argument("--audit-dir", type=Path, required=True)
    parser.add_argument("--analysis-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path,
                        default=ROOT / "outputs/TII_Scalability_Benchmark_v6_Modal_FullScale_Results.zip")
    parser.add_argument("--staging", type=Path, default=Path("/tmp/v6_modal_package_staging"))
    args = parser.parse_args()
    staging = args.staging
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    copied = {}

    # 00 provenance
    provenance = staging / "00_provenance"
    for name in ("v6_formal_manifest.json",):
        copy_file(ROOT / "protocol" / name, provenance / name)
    run_manifest = load(args.run_dir / "run_manifest.json", {}) or {}
    modal_summary = load(args.run_dir / "pilot_summary.json", {}) or {}
    gate_decision = load(ROOT / "work/v6_formal_gate_decision/formal_gate_decision.json", {}) or {}
    (provenance / "run_provenance.json").write_text(json.dumps({
        "run_manifest": run_manifest,
        "gate_decision": gate_decision.get("gates"),
        "task_count": (load(ROOT / "protocol/v6_formal_manifest.json", {}) or {}).get("tasks"),
        "source": "docs/v6_STAGE_STATUS.md and protocol/scalability_v6_frozen_protocol.json",
    }, indent=2, sort_keys=True) + "\n")

    # 01 frozen protocol
    frozen = staging / "01_frozen_protocol"
    for name in ("scalability_v6_frozen_protocol.yaml", "scalability_v6_frozen_protocol.json",
                 "v6_modal_shards_manifest.json", "v6-semantics_gate.json"):
        copy_file(ROOT / "protocol" / name, frozen / name)
    copy_file(ROOT / "protocol/v6_semantics_gate.json", frozen / "v6_semantics_gate.json")
    copied["shard_csvs"] = copy_tree(ROOT / "protocol/v6_modal_shards", frozen / "shards")

    # 02 modal environment / 03 cost
    for arcname, names in (
        ("02_modal_environment", ("v6_modal_environment.md", "v6_modal_pilot_report.md")),
        ("03_modal_cost_and_resource_audit", ("v6_modal_budget",)),
    ):
        target = staging / arcname
        if arcname.startswith("02"):
            for name in names:
                copy_file(ROOT / "docs" / name, target / name)
        else:
            copy_tree(ROOT / "work/v6_modal_budget", target)
            copy_file(ROOT / "work/v6_modal_budget/budget.json", target / "budget.json")

    # 04 raw 8280, 05 checkpoint/recovery
    copied["raw"] = copy_tree(args.run_dir, staging / "04_raw_8280")
    # Drop the orchestrator staging copy (a duplicate of the frozen protocol)
    # so the raw block contains only the 8,280 records and their sidecars.
    shutil.rmtree(staging / "04_raw_8280" / "staging", ignore_errors=True)
    copied["recovery"] = copy_tree(ROOT / "work/v6_modal_recovery", staging / "05_checkpoint_and_recovery")
    copied["pilot"] = copy_tree(ROOT / "work/v6_modal_pilot", staging / "05_checkpoint_and_recovery/pilot")

    # 06 audit, 07 statistics
    copied["audit"] = copy_tree(args.audit_dir, staging / "06_independent_audit")
    copied["analysis"] = copy_tree(args.analysis_dir, staging / "07_statistical_analysis")

    # 08 figures
    figures = staging / "08_figures"
    for name in ("figure6.svg", "figure6.pdf", "figure6.eps", "figure6.png",
                 "figure6_caption.md", "figure6_data.csv", "figure6_metadata.json"):
        copy_file(args.analysis_dir / name, figures / name)
    copy_file(ROOT / "docs/v6_figure6_spec.md", figures / "v6_figure6_spec.md")

    # 09 paper material
    paper = staging / "09_paper_material"
    for name in ("v6_section_vc_suggestions.md", "v6_s7b_suggestions.md",
                 "v6_reviewer_mapping.md"):
        copy_file(ROOT / "docs" / name, paper / name)

    # 10 reproducibility
    repro = staging / "10_reproducibility"
    for name in ("v6_STAGE_STATUS.md", "v6_gate_decision.md",
                 "v6_differential_v2_acceptance_report.md", "v6_optimization_differential_report.md"):
        copy_file(ROOT / "docs" / name, repro / name)
    (repro / "reproduce.md").write_text(
        "# Reproduce the Modal full-scale matrix\n\n"
        "```bash\n"
        "cd <repo>\n"
        "python scripts/validate_v6_formal_protocol.py --manifest protocol/v6_formal_manifest.json \\\n"
        "  --tasks protocol/v6_formal_tasks --semantics-gate protocol/v6_semantics_gate.json \\\n"
        "  --out results/formal_validation.json\n"
        "export TIIPY=<venv with modal 1.6.3>\n"
        "\"$TIIPY\" scripts/run_v6_modal.py run --confirm-budget --max-parallel 20\n"
        "\"$TIIPY\" scripts/run_v6_modal.py download --run-id <run id>\n"
        "\"$TIIPY\" scripts/audit_v6_modal_results.py --download-dir work/v6_formal_run_<run id> ...\n"
        "python scripts/analyze_v6_formal_results.py --tasks-dir protocol/v6_formal_tasks \\\n"
        "  --manifest protocol/v6_formal_manifest.json --raw-dir work/v6_formal_run_<run id> \\\n"
        "  --out work/v6_analysis_formal\n"
        "python scripts/plot_figure6_v6.py --analysis-dir work/v6_analysis_formal --out work/v6_analysis_formal\n"
        "```\n"
    )

    # acceptance report
    audit = load(args.audit_dir / "audit.json", {}) or {}
    acceptance_extra = {
        "audit_tasks_audited": audit.get("tasks_audited"),
        "audit_unique_task_ids": audit.get("unique_task_ids"),
        "audit_shards_audited": audit.get("shards_audited"),
        "audit_execution_status": audit.get("execution_status"),
        "audit_classification": audit.get("classification"),
        "audit_technical_failures": audit.get("technical_failures"),
        "audit_findings": audit.get("audit_findings"),
        "audit_errors": audit.get("audit_errors"),
    }
    analysis = load(args.analysis_dir / "statistics.json", {}) or {}
    acceptance = {
        "gates": gate_decision.get("gates"),
        "formal_gates_decision": gate_decision.get("decision"),
        "raw_audit": acceptance_extra,
        "statistics_headline": analysis.get("headline") or analysis.get("denominators"),
        "modal_cost": load(ROOT / "work/v6_modal_budget/budget.json", {}),
        "figure6": {"files": [name for name in
                              ("figure6.svg", "figure6.pdf", "figure6.eps", "figure6.png")]},
    }
    gates = gate_decision.get("gates") or {}
    totals = (run_manifest.get("totals") or {})
    audit_pass = bool(audit.get("PASS"))
    report = [
        "# Final acceptance report — v6 Modal full-scale (8,280 cells)",
        "",
        f"**Decision: {'READY_FOR_IEEE_TII' if (all(gates.values()) and audit_pass) else 'NOT READY'}**",
        "",
        "## Execution",
        f"- run id `{run_manifest.get('run_id')}`, source commit `{run_manifest.get('source_sha')}`,",
        f"  image spec `{run_manifest.get('image_spec_sha256')}`, Modal CPU 1 core / 4096 MiB,",
        f"  60 s per-cell cap, `max_parallel` {((run_manifest.get('resources') or {}).get('max_parallel'))},",
        f"  volume `tii-v6-scalability` prefix `{run_manifest.get('volume_prefix')}`.",
        f"- raw records: **{run_manifest.get('raw_records')}** / 8,280 over 120 shards;",
        f"  run manifest declares {run_manifest.get('tasks')} tasks in {run_manifest.get('shards')} shards.",
        "",
        "## Verdict / status accounting",
        f"- COMPLETED {totals.get('tasks_completed')}, TIMEOUT_WALL_CLOCK {totals.get('tasks_timeout_wall_clock')},",
        f"  ERROR {totals.get('tasks_error')} (right-censored; never imputed).",
        f"- VER {totals.get('verdict_ver')}, AMB {totals.get('verdict_amb')}, INC {totals.get('verdict_inc')} (INC is completed-but-inconclusive).",
        "",
        "## Formal gates",
        "```text",
        *[f"{name} = {str(value).lower()}" for name, value in sorted(gates.items())],
        "```",
        "",
        "## Independent audit (GitHub Actions)",
        f"- PASS = **{audit_pass}**; audited tasks {audit.get('tasks_audited')},",
        f"  unique task ids {audit.get('unique_task_ids')}, shards {audit.get('shards_audited')},",
        f"  technical failures {audit.get('technical_failures')}, findings {audit.get('audit_findings')}.",
        "",
        "## Cost and incidents",
        f"- Modal metered delta for pilot + formal: ${(run_manifest.get('actual_modal_cost') or {}).get('delta_usd')}",
        f"  (cap $10, guard $9.50); incidents:",
        *[f"  - {item.get('issue')} (data loss {item.get('data_loss')})"
          for item in (run_manifest.get("incidents") or [])],
        "",
        "## Deliverable contents",
        "`00_provenance`, `01_frozen_protocol`, `02_modal_environment`,",
        "`03_modal_cost_and_resource_audit`, `04_raw_8280`, `05_checkpoint_and_recovery`,",
        "`06_independent_audit`, `07_statistical_analysis`, `08_figures`, `09_paper_material`,",
        "`10_reproducibility`, `MANIFEST.sha256`.",
        "",
        "Machine-readable source: `00_provenance/run_provenance.json`,",
        "`06_independent_audit/audit.json`, `07_statistical_analysis/statistics.json`.",
        "",
    ]
    (staging / "FINAL_ACCEPTANCE_REPORT.md").write_text("\n".join(report))

    # MANIFEST.sha256
    lines = []
    for path in sorted(staging.rglob("*")):
        if path.is_dir() or path.name == "MANIFEST.sha256":
            continue
        lines.append(f"{sha256_file(path)}  {path.relative_to(staging)}")
    (staging / "MANIFEST.sha256").write_text("\n".join(lines) + "\n")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(staging.rglob("*")):
            if path.is_dir():
                continue
            archive.write(path, path.relative_to(staging))
    with zipfile.ZipFile(args.out) as archive:
        names = set(archive.namelist())
        manifest = archive.read("MANIFEST.sha256").decode()
        checked = 0
        for line in manifest.splitlines():
            expected, relative = line.split("  ", 1)
            if hashlib.sha256(archive.read(relative)).hexdigest() != expected:
                raise SystemExit(f"self-check failed: {relative}")
            checked += 1
        if checked != len(names) - 1:
            raise SystemExit(f"self-check failed: {checked}/{len(names) - 1}")
    print(json.dumps({
        "zip": str(args.out),
        "bytes": args.out.stat().st_size,
        "files": len(lines),
        "self_check": "PASS",
        "copied": {key: value for key, value in copied.items() if value},
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
