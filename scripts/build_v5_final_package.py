#!/usr/bin/env python3
"""Assemble the audited non-plotting v5 final-delivery archive."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


REPO_SLUG = "Lucas-cs11/tii-26-7248-scalability-v2"
FORMAL_RUN = "42222027847"
FORMAL_SHA = "dc7cae34310edacd6fdf3df9c2c4382ad715bd77"


def command(*args, cwd=None):
    return subprocess.check_output(args, cwd=cwd, text=True)


def copytree(src, dst):
    shutil.copytree(src, dst, dirs_exist_ok=True)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest(stage):
    entries = []
    for path in sorted(path for path in stage.rglob("*") if path.is_file()):
        rel = path.relative_to(stage).as_posix()
        if rel == "09_independent_validation/MANIFEST.sha256":
            continue
        entries.append(f"{sha256(path)}  {rel}")
    target = stage / "09_independent_validation/MANIFEST.sha256"
    target.write_text("\n".join(entries) + "\n")
    return entries


def verify_manifest(stage):
    manifest_path = stage / "09_independent_validation/MANIFEST.sha256"
    listed = {}
    for line in manifest_path.read_text().splitlines():
        digest, rel = line.split("  ", 1)
        listed[rel] = digest
    actual = {
        path.relative_to(stage).as_posix(): path
        for path in stage.rglob("*") if path.is_file() and path != manifest_path
    }
    if set(listed) != set(actual):
        raise RuntimeError("package manifest coverage mismatch")
    for rel, path in actual.items():
        if sha256(path) != listed[rel]:
            raise RuntimeError(f"package SHA mismatch: {rel}")


def write_github_metadata(stage):
    run = json.loads(command(
        "gh", "run", "view", FORMAL_RUN, "--repo", REPO_SLUG,
        "--json", "databaseId,status,conclusion,headSha,headBranch,createdAt,updatedAt,url,workflowName,event,jobs",
    ))
    if run["conclusion"] != "success" or run["headSha"] != FORMAL_SHA:
        raise RuntimeError("formal GitHub run status/SHA does not match frozen provenance")
    out = stage / "07_github_actions"
    (out / "formal_run_metadata.json").write_text(json.dumps(run, indent=2, sort_keys=True) + "\n")
    artifacts = command(
        "gh", "api", "--paginate", "--slurp",
        f"repos/{REPO_SLUG}/actions/runs/{FORMAL_RUN}/artifacts?per_page=100",
    )
    (out / "formal_artifact_metadata.json").write_text(artifacts)
    with (out / "job_status_history.csv").open("w") as fh:
        fh.write("job_id,name,status,conclusion,started_at,completed_at,url\n")
        for job in run["jobs"]:
            fh.write(
                f'{job.get("databaseId")},{json.dumps(job.get("name"))},{job.get("status")},'
                f'{job.get("conclusion")},{job.get("startedAt")},{job.get("completedAt")},{job.get("url")}\n'
            )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--formal-artifacts", type=Path, required=True)
    ap.add_argument("--formal-audit", type=Path, required=True)
    ap.add_argument("--formal-analysis", type=Path, required=True)
    ap.add_argument("--baseline-artifacts", type=Path, required=True)
    ap.add_argument("--baseline-audit", type=Path, required=True)
    ap.add_argument("--build-root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    repo = args.repo.resolve()
    integrity = json.loads((args.formal_audit / "integrity_validation.json").read_text())
    baseline = json.loads((args.baseline_audit / "baseline_validation.json").read_text())
    analysis = json.loads((args.formal_analysis / "statistics_summary.json").read_text())
    if not (integrity.get("PASS") and baseline.get("PASS") and analysis.get("PASS")):
        raise RuntimeError("refusing to package non-PASS audit/analysis inputs")

    stage = args.build_root.resolve() / "TII_Scalability_Benchmark_v5"
    if stage.exists():
        shutil.rmtree(stage)
    for name in (
        "00_readme_and_protocol", "01_generator_and_models", "02_structural_scaling",
        "03_wip_scaling", "04_dense_L_sweep", "05_difficult_cases",
        "06_fullscale_baseline", "07_github_actions", "08_figure6",
        "09_independent_validation", "10_manuscript_recommendations",
    ):
        (stage / name).mkdir(parents=True, exist_ok=True)

    readme = stage / "00_readme_and_protocol/README.md"
    readme.write_text(
        "# IEEE TII Scalability Benchmark v5\n\n"
        "This release contains audited v5 formal raw data, frozen protocol, full "
        "GitHub Actions provenance, recovered 36,450-evaluation background data, "
        "statistics, and manuscript guidance. Figure rendering is intentionally "
        "deferred under the current user instruction; panel-ready data and the "
        "inherited plotting source are included. See FINAL_ACCEPTANCE_REPORT.md.\n"
    )
    copytree(repo / "protocol", stage / "00_readme_and_protocol/protocol")
    for workflow in ("scalability_v5_formal.yml", "scalability_v5_retry.yml"):
        shutil.copy2(repo / ".github/workflows" / workflow,
                     stage / "00_readme_and_protocol" / workflow)

    # Formal source is archived exactly at the run SHA; analysis source is separate.
    source = stage / "01_generator_and_models/source_snapshot"
    source.mkdir(parents=True)
    with (source / f"formal_run_source_{FORMAL_SHA[:12]}.tar.gz").open("wb") as fh:
        subprocess.run(["git", "archive", "--format=tar.gz", "--prefix=source/", FORMAL_SHA],
                       cwd=repo, stdout=fh, check=True)
    current = command("git", "rev-parse", "HEAD", cwd=repo).strip()
    with (source / "postrun_analysis_source.tar.gz").open("wb") as fh:
        subprocess.run(["git", "archive", "--format=tar.gz", "--prefix=source/", "HEAD"],
                       cwd=repo, stdout=fh, check=True)
    subprocess.run(["git", "bundle", "create", str(source / "repository.bundle"), "--all"],
                   cwd=repo, check=True)
    (source / "source_identity.json").write_text(json.dumps({
        "repository": f"https://github.com/{REPO_SLUG}", "formal_run_commit": FORMAL_SHA,
        "postrun_analysis_commit": current, "formal_run_id": FORMAL_RUN,
    }, indent=2) + "\n")
    copytree(args.formal_artifacts / f"v5-formal-semantic-{FORMAL_RUN}",
             stage / "01_generator_and_models/semantic_validation")

    for directory, raw, stats in (
        ("02_structural_scaling", "structural_raw.csv", "structural_statistics.csv"),
        ("03_wip_scaling", "wip_raw.csv", "wip_statistics.csv"),
        ("04_dense_L_sweep", "L_sweep_raw.csv", "L_sweep_statistics.csv"),
        ("05_difficult_cases", "difficult_raw.csv", "difficult_statistics.csv"),
    ):
        shutil.copy2(args.formal_analysis / raw, stage / directory / "merged_results.csv")
        shutil.copy2(args.formal_analysis / stats, stage / directory / "statistics.csv")
    shutil.copy2(args.formal_analysis / "L_model_sensitivity.csv",
                 stage / "04_dense_L_sweep/L_model_sensitivity.csv")
    shutil.copy2(args.formal_audit / "merged_results.jsonl",
                 stage / "04_dense_L_sweep/formal_merged_raw.jsonl")

    copytree(args.baseline_artifacts, stage / "06_fullscale_baseline/original_proposed_artifacts")
    copytree(args.baseline_audit, stage / "06_fullscale_baseline/verified_statistics")

    copytree(args.formal_artifacts, stage / "07_github_actions/artifacts")
    shutil.copy2(args.formal_artifacts / "artifact_selection.json",
                 stage / "07_github_actions/artifact_selection.json")
    write_github_metadata(stage)

    figure = stage / "08_figure6"
    copytree(args.formal_analysis, figure / "panel_source_data")
    shutil.copy2(repo / "scripts/plot_figure6_v4.py", figure / "plotting_source_inherited_v4.py")
    shutil.copy2(repo / "docs/v5_figure6_deferred.md", figure / "PLOTTING_DEFERRED.md")

    validation = stage / "09_independent_validation"
    copytree(args.formal_audit, validation / "formal_audit")
    shutil.copy2(args.formal_analysis / "statistics_summary.json", validation / "statistics_recomputation.json")
    shutil.copy2(args.baseline_audit / "baseline_validation.json", validation / "baseline_validation.json")
    for script in (
        "audit_v5_results.py", "validate_v5_formal_protocol.py", "analyze_v5_formal_results.py",
        "audit_v5_fullscale_baseline.py", "build_v5_artifact_selection.py", "build_v5_final_package.py",
    ):
        shutil.copy2(repo / "scripts" / script, validation / script)

    guidance = stage / "10_manuscript_recommendations"
    for source_name, target_name in (
        ("v5_manuscript_section_vc.md", "suggested_section_vc.md"),
        ("v5_manuscript_s7b.md", "suggested_supplement_s7b.md"),
        ("v5_reviewer_mapping.md", "reviewer_response_mapping.md"),
    ):
        shutil.copy2(repo / "docs" / source_name, guidance / target_name)

    (stage / "FINAL_ACCEPTANCE_REPORT.md").write_text(
        "# Final acceptance report\n\n"
        "## Decision\n\n"
        "**PASS WITH LIMITATION — Figure 6 drawing deferred by explicit user instruction.**\n\n"
        "The non-plotting deliverable passes: 8,280/8,280 formal tasks have a final "
        "record; 120/120 formal raw shards and all GitHub artifacts are present; "
        "the independent formal audit is PASS; the 36,450-row baseline recovery is PASS; "
        "and this archive has an exhaustive SHA-256 manifest.\n\n"
        "Formal outcome counts: 8,166 COMPLETED, 114 TIMEOUT_WALL_CLOCK, 1,787 VER, "
        "2,088 AMB, and 4,291 INC. Timeouts are retained and not converted to zero "
        "runtime/work. The baseline contains 36,450 evaluation IDs and 5,589 distinct "
        "bounded problems; proposed and reference absolute timings remain separate.\n\n"
        "The omitted Figure 6 artwork is the only acceptance limitation. Panel-ready data, "
        "the inherited style source, and caption guidance are present for a later drawing step.\n"
    )

    entries = manifest(stage)
    verify_manifest(stage)
    args.out.mkdir(parents=True, exist_ok=True)
    zip_path = args.out / "TII_Scalability_Benchmark_v5_FullScale_Results.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(path for path in stage.rglob("*") if path.is_file()):
            archive.write(path, Path(stage.name) / path.relative_to(stage))
    verify_root = args.build_root.resolve() / "zip_verification"
    if verify_root.exists():
        shutil.rmtree(verify_root)
    with zipfile.ZipFile(zip_path) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"corrupt ZIP member: {bad}")
        archive.extractall(verify_root)
    verify_manifest(verify_root / stage.name)
    shutil.rmtree(verify_root)
    (args.out / f"{zip_path.name}.sha256").write_text(f"{sha256(zip_path)}  {zip_path.name}\n")
    print(json.dumps({
        "PASS": True, "zip": str(zip_path), "zip_sha256": sha256(zip_path),
        "manifest_entries": len(entries), "formal_run": FORMAL_RUN,
    }, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
