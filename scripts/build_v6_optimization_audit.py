#!/usr/bin/env python3
"""Assemble the v6 bottleneck-optimisation audit package.

Collects the frozen protocols, profiling evidence, source diff, semantic
equivalence checks, differential run, 288-cell Pilot comparison, budget,
historical-impact audit and GitHub run metadata into a deterministic staging
tree, writes a SHA-256 manifest and zips it, then re-opens the archive and
verifies every hash (the ZIP self-check required by the handoff).

Nothing is rewritten in place: raw artifacts are copied byte-for-byte.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path("/tmp/v6work")
WORK = ROOT / "work"
BASELINE_COMMIT = "10d27726c085076fe790d574eabd15d7423ac86d"
OPTIMIZED_COMMIT = "2154432"
INSTRUMENTED_COMMIT = "1dd1d49"


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
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        count += 1
    return count


def copy_file(source: Path, destination: Path) -> bool:
    if not source.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return True


def git_output(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True
    ).stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-zip", type=Path,
                        default=ROOT / "outputs/TII_Scalability_v6_Bottleneck_Optimization_Audit.zip")
    parser.add_argument("--staging-dir", type=Path,
                        default=Path("/tmp/v6_audit_staging"))
    parser.add_argument("--meta", type=Path, default=None,
                        help="JSON with run ids / gate decision / budget summary")
    args = parser.parse_args()
    staging = args.staging_dir
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    meta = {}
    if args.meta and args.meta.is_file():
        meta = json.loads(args.meta.read_text())

    copied = {}
    # 1. protocols
    protocols = staging / "01_protocols"
    for name in (
        "v6_profile_sample_1.json",
        "v6_perf_differential_1.json",
        "v6_semantics_gate.json",
        "v6_pilot_manifest.json",
        "v6_cache_validation_manifest.json",
    ):
        copied[name] = copy_file(ROOT / "protocol" / name, protocols / name)
    copied["pilot_tasks"] = copy_tree(ROOT / "protocol/v6_pilot_tasks", protocols / "v6_pilot_tasks")
    copied["cache_validation_tasks"] = copy_tree(
        ROOT / "protocol/v6_cache_validation_tasks", protocols / "v6_cache_validation_tasks"
    )

    # 2. docs
    docs = staging / "02_documents"
    for name in (
        "v6_bottleneck_root_cause.md",
        "v6_optimization_differential_report.md",
        "v6_gate_decision.md",
        "v6_scg_cache.md",
        "v6_cache_validation_report.md",
        "v6_historical_impact_audit.md",
        "v6_status_report.md",
    ):
        copied[name] = copy_file(ROOT / "docs" / name, docs / name)

    # 3. source diff and test evidence
    code = staging / "03_optimisation"
    code.mkdir(parents=True, exist_ok=True)
    (code / "source_diff.patch").write_text(
        git_output("diff", BASELINE_COMMIT, "HEAD", "--", "src", "runner", "scripts",
                   "tests", ".github/workflows", "protocol", "docs")
    )
    (code / "commit_log.txt").write_text(
        git_output("log", "--oneline", f"{BASELINE_COMMIT}..HEAD")
    )
    try:
        tests = subprocess.run(
            [str(ROOT / ".venv/bin/python"), "-m", "unittest", "discover", "-s", "tests",
             "-p", "test_*.py", "-v"],
            cwd=ROOT, capture_output=True, text=True,
        )
        (code / "test_suite.txt").write_text(tests.stdout + tests.stderr)
    except Exception as exc:  # noqa: BLE001
        (code / "test_suite.txt").write_text(f"test suite could not be run: {exc}\n")

    # 4. evidence trees
    evidence = {
        "04_profiling_baseline": WORK / "v6_profile_20261107_134729",
        "05_profiling_optimized": WORK / "v6_profile_opt",
        "06_scg_equivalence": WORK / "v6_scg_equivalence_1",
        "07_performance_differential": WORK / "v6_perf_differential_1",
        "08_historical_impact": WORK / "v6_historical_impact",
        "09_cache_validation_baseline": WORK / "v6_cache_validation_audit_42339602408",
        "09b_cache_validation_optimized": WORK / "v6_cache_validation_opt_42347953385",
        "15_synchronizer_equivalence": WORK / "v6_sync_equivalence_1",
        "16_optimized_semantics_validation": WORK / "v6_optimized_semantics_validation",
        "13_gate_decision": WORK / "v6_gate_decision",

    }
    for arcname, source in evidence.items():
        target = staging / arcname
        copied[arcname] = copy_tree(source, target)

    # 5. pilot runs / audits / comparison (optional names discovered from meta)
    for key, arcname in (
        ("optimized_pilot_artifacts", "10_pilot_optimized/raw_artifacts"),
        ("optimized_pilot_audit", "10_pilot_optimized/audit"),
        ("baseline_pilot_artifacts", "11_pilot_instrumented_baseline/raw_artifacts"),
        ("baseline_pilot_audit", "11_pilot_instrumented_baseline/audit"),
        ("pilot_comparison", "12_pilot_comparison"),
        ("budget", "13_budget"),
        ("differential_results", "07_performance_differential"),
    ):
        value = meta.get(key)
        if not value:
            continue
        source = Path(value)
        if source.is_dir():
            copied[arcname] = copy_tree(source, staging / arcname)
        else:
            copied[arcname] = copy_file(source, staging / arcname)

    copied["gate_criteria"] = copy_file(
        ROOT / "protocol/v6_gate_criteria_1.json", staging / "01_protocols/v6_gate_criteria_1.json"
    )

    # 6. run metadata / gate decision
    github = staging / "14_github_and_gates"
    github.mkdir(parents=True, exist_ok=True)
    (github / "runs_and_gates.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    (github / "commits.txt").write_text(
        f"baseline={BASELINE_COMMIT}\ninstrumented_baseline={INSTRUMENTED_COMMIT}\n"
        f"optimized={OPTIMIZED_COMMIT}\nhead={git_output('rev-parse', 'HEAD').strip()}\n"
    )

    # 7. SHA-256 manifest over the staging tree
    digest_lines = []
    for path in sorted(staging.rglob("*")):
        if path.is_dir() or path == staging / "SHA256SUMS":
            continue
        digest_lines.append(f"{sha256_file(path)}  {path.relative_to(staging)}")
    (staging / "SHA256SUMS").write_text("\n".join(digest_lines) + "\n")

    # 8. zip and self-check
    args.out_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.out_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(staging.rglob("*")):
            if path.is_dir():
                continue
            archive.write(path, path.relative_to(staging))
    with zipfile.ZipFile(args.out_zip) as archive:
        names = set(archive.namelist())
        if "SHA256SUMS" not in names:
            raise SystemExit("self-check failed: SHA256SUMS missing")
        manifest_text = archive.read("SHA256SUMS").decode()
        checked = 0
        for line in manifest_text.splitlines():
            expected, relative = line.split("  ", 1)
            data = archive.read(relative)
            actual = hashlib.sha256(data).hexdigest()
            if actual != expected:
                raise SystemExit(f"self-check failed: {relative}")
            checked += 1
        expected_entries = [name for name in names if name != "SHA256SUMS"]
        if checked != len(expected_entries):
            raise SystemExit(
                f"self-check failed: {checked} hashes for {len(expected_entries)} files"
            )
        # Nested SHA-256 manifests (evidence directories keep their own) are part of
        # the archive and are themselves verified against the files they list.
        nested_checked = 0
        for name in sorted(names):
            if name == "SHA256SUMS" or not name.endswith("SHA256SUMS"):
                continue
            base = name[: -len("SHA256SUMS")]
            for line in archive.read(name).decode().splitlines():
                if not line.strip():
                    continue
                expected, relative = line.split("  ", 1)
                relative = relative.lstrip("./")
                member = base + relative if not relative.startswith(base) else relative
                if member not in names:
                    raise SystemExit(f"self-check failed: nested entry missing {member}")
                if hashlib.sha256(archive.read(member)).hexdigest() != expected:
                    raise SystemExit(f"self-check failed: nested hash mismatch {member}")
                nested_checked += 1
        if nested_checked == 0:
            raise SystemExit("self-check failed: no nested manifests verified")
    size = args.out_zip.stat().st_size
    print(json.dumps({
        "zip": str(args.out_zip),
        "bytes": size,
        "files": len(digest_lines),
        "self_check": "PASS",
        "copied": {key: value for key, value in copied.items() if value},
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
