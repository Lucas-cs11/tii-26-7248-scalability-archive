#!/usr/bin/env python3
"""Assemble and verify the final, self-auditing v4 release archive."""
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
PILOT_RUN = 42004602864
FORMAL_RUN = 42040753991
FORMAL_SHA = "c98c75b21f59b49c89e6a2afe87c6bb01a605909"


def run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(args, cwd=cwd, text=True)


def copytree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, dirs_exist_ok=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_run_metadata(stage: Path, run_id: int, expected_sha: str | None = None) -> None:
    fields = (
        "databaseId,status,conclusion,headSha,headBranch,createdAt,updatedAt,"
        "url,workflowName,event,jobs"
    )
    run_data = json.loads(run(
        "gh", "run", "view", str(run_id), "--repo", REPO_SLUG,
        "--json", fields,
    ))
    if run_data.get("status") != "completed" or run_data.get("conclusion") != "success":
        raise RuntimeError(f"GitHub run {run_id} is not successfully completed")
    if expected_sha and run_data.get("headSha") != expected_sha:
        raise RuntimeError(
            f"GitHub run {run_id} SHA {run_data.get('headSha')} != {expected_sha}"
        )
    out = stage / "06_validation" / f"github_run_{run_id}.json"
    out.write_text(json.dumps(run_data, indent=2, sort_keys=True) + "\n")
    artifacts = json.loads(run(
        "gh", "api",
        f"repos/{REPO_SLUG}/actions/runs/{run_id}/artifacts?per_page=100",
    ))
    (stage / "06_validation" / f"github_artifacts_{run_id}.json").write_text(
        json.dumps(artifacts, indent=2, sort_keys=True) + "\n"
    )


def build_manifest(stage: Path) -> list[str]:
    entries = []
    for path in sorted(p for p in stage.rglob("*") if p.is_file()):
        rel = path.relative_to(stage).as_posix()
        if rel == "SHA256SUMS":
            continue
        entries.append(f"{sha256(path)}  {rel}")
    (stage / "SHA256SUMS").write_text("\n".join(entries) + "\n")
    return entries


def verify_manifest(stage: Path) -> None:
    lines = (stage / "SHA256SUMS").read_text().splitlines()
    expected_paths = {
        p.relative_to(stage).as_posix()
        for p in stage.rglob("*") if p.is_file() and p.name != "SHA256SUMS"
    }
    observed_paths = set()
    for line in lines:
        digest, rel = line.split("  ", 1)
        observed_paths.add(rel)
        if sha256(stage / rel) != digest:
            raise RuntimeError(f"SHA-256 mismatch: {rel}")
    if observed_paths != expected_paths:
        raise RuntimeError("SHA-256 manifest coverage mismatch")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--pilot-artifacts", type=Path, required=True)
    ap.add_argument("--formal-artifacts", type=Path, required=True)
    ap.add_argument("--final-build", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    repo = args.repo.resolve()
    stage = args.final_build.resolve() / "TII_Scalability_Benchmark_Final_Results"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    # Human-facing map and frozen protocols.
    shutil.copy2(repo / "docs/final_README.md", stage / "README.md")
    protocol = stage / "01_protocol"
    protocol.mkdir()
    for name in ("v4_pilot_manifest.json", "v4_formal_manifest.json"):
        shutil.copy2(repo / "protocol" / name, protocol / name)
    copytree(repo / "protocol/v4_pilot_tasks", protocol / "v4_pilot_tasks")
    copytree(repo / "protocol/v4_formal_tasks", protocol / "v4_formal_tasks")
    workflows = protocol / "workflows"
    workflows.mkdir()
    for name in ("scalability_v4_pilot.yml", "scalability_v4_formal.yml"):
        shutil.copy2(repo / ".github/workflows" / name, workflows / name)

    # Preserve the downloaded artifact directory names and every raw file.
    copytree(args.pilot_artifacts.resolve(), stage / "02_pilot_raw")
    copytree(args.formal_artifacts.resolve(), stage / "03_formal_raw")
    merged = stage / "04_merged_results"
    subprocess.run([
        sys.executable, str(repo / "scripts/audit_v4_results.py"),
        "--tasks", str(protocol / "v4_formal_tasks"),
        "--artifacts", str(stage / "03_formal_raw"),
        "--out", str(merged),
    ], cwd=repo, check=True)

    # Exact code snapshot, history bundle, and the audit/replot entry points.
    source = stage / "05_source"
    source.mkdir()
    commit = run("git", "rev-parse", "HEAD", cwd=repo).strip()
    branch = run("git", "branch", "--show-current", cwd=repo).strip()
    (source / "source_identity.json").write_text(json.dumps({
        "repository": f"https://github.com/{REPO_SLUG}",
        "branch": branch,
        "commit": commit,
        "formal_run_commit": FORMAL_SHA,
    }, indent=2) + "\n")
    archive_path = source / "release_source_snapshot.tar.gz"
    with archive_path.open("wb") as fh:
        subprocess.run(
            ["git", "archive", "--format=tar.gz", "--prefix=source/", "HEAD"],
            cwd=repo, stdout=fh, check=True,
        )
    with (source / f"formal_run_source_{FORMAL_SHA[:7]}.tar.gz").open("wb") as fh:
        subprocess.run(
            ["git", "archive", "--format=tar.gz", "--prefix=source/", FORMAL_SHA],
            cwd=repo, stdout=fh, check=True,
        )
    subprocess.run(
        ["git", "bundle", "create", str(source / "source_repository.bundle"),
         "scalability-v4-final"], cwd=repo, check=True,
    )
    scripts = source / "reproduction_scripts"
    scripts.mkdir()
    for name in (
        "audit_v4_results.py", "model_semantics_v4.py",
        "plot_figure6_v4.py", "build_final_package.py",
    ):
        shutil.copy2(repo / "scripts" / name, scripts / name)

    # Independent validation, GitHub provenance, and final graphics.
    validation = stage / "06_validation"
    validation.mkdir()
    shutil.copy2(merged / "validation.json", validation / "validation.json")
    shutil.copy2(args.final_build / "06_validation/reachable_concurrency.json",
                 validation / "reachable_concurrency.json")
    shutil.copy2(repo / "docs/timing_provenance.md",
                 validation / "timing_provenance.md")
    write_run_metadata(stage, PILOT_RUN)
    write_run_metadata(stage, FORMAL_RUN, FORMAL_SHA)
    copytree(args.final_build.resolve() / "07_figure6", stage / "07_figure6")

    guidance = stage / "08_manuscript_guidance"
    guidance.mkdir()
    for name in ("manuscript_section_vc.md", "manuscript_s7b.md", "reviewer_mapping.md"):
        shutil.copy2(repo / "docs" / name, guidance / name)

    entries = build_manifest(stage)
    verify_manifest(stage)
    validation_data = json.loads((validation / "validation.json").read_text())
    if not validation_data.get("PASS"):
        raise RuntimeError("formal result validation is not PASS")

    args.out.mkdir(parents=True, exist_ok=True)
    zip_path = args.out / "TII_Scalability_Benchmark_Final_Results.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(p for p in stage.rglob("*") if p.is_file()):
            zf.write(path, Path(stage.name) / path.relative_to(stage))

    # Verify the archive independently by extracting and rechecking its manifest.
    verify_dir = args.final_build.resolve() / "zip_verification"
    if verify_dir.exists():
        shutil.rmtree(verify_dir)
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad:
            raise RuntimeError(f"corrupt ZIP member: {bad}")
        zf.extractall(verify_dir)
    verify_manifest(verify_dir / stage.name)
    shutil.rmtree(verify_dir)
    (args.out / f"{zip_path.name}.sha256").write_text(
        f"{sha256(zip_path)}  {zip_path.name}\n"
    )
    print(json.dumps({
        "zip": str(zip_path),
        "zip_sha256": sha256(zip_path),
        "manifest_entries": len(entries),
        "formal_validation_PASS": True,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
