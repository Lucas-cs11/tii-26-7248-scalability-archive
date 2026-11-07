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
    for name in ("section_vc_suggestions.md", "s7b_suggestions.md", "reviewer_mapping_v6.md"):
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
    analysis = load(args.analysis_dir / "statistics.json", {}) or {}
    acceptance = {
        "gates": gate_decision.get("gates"),
        "formal_gates_decision": gate_decision.get("decision"),
        "raw_audit": {key: audit.get(key) for key in
                      ("PASS", "tasks", "unique_tasks", "missing", "duplicates", "errors")},
        "statistics_headline": analysis.get("headline") or analysis.get("denominators"),
        "modal_cost": load(ROOT / "work/v6_modal_budget/budget.json", {}),
        "figure6": {"files": [name for name in
                              ("figure6.svg", "figure6.pdf", "figure6.eps", "figure6.png")]},
    }
    (staging / "FINAL_ACCEPTANCE_REPORT.md").write_text(
        "# v6 Modal full-scale final acceptance report\n\n"
        "```json\n" + json.dumps(acceptance, indent=2, sort_keys=True) + "\n```\n\n"
        "Machine-readable source: `00_provenance/run_provenance.json`, "
        "`06_independent_audit/`, `07_statistical_analysis/statistics.json`.\n"
    )

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
