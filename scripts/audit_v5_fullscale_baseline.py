#!/usr/bin/env python3
"""Recover and audit the frozen 36,450-evaluation proposed/reference baseline."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path


PRIMARY_RUN = "41781437233"
SUPPLEMENT_RUNS = {0: "41781103322", 235: "41789970846"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def percentile(values, q):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", type=Path, required=True)
    ap.add_argument("--reference", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    errors = []
    shard_manifest = []
    proposed = []
    fieldnames = None
    for shard in range(255):
        run_id = SUPPLEMENT_RUNS.get(shard, PRIMARY_RUN)
        candidates = sorted(
            (args.artifacts / f"run_{run_id}").glob(
                f"timing-{shard}-{run_id}/shard_{shard:03d}.csv"
            )
        )
        if len(candidates) != 1:
            errors.append(f"shard {shard}: expected one selected artifact, found {len(candidates)}")
            continue
        path = candidates[0]
        with path.open(newline="") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)
            if fieldnames is None:
                fieldnames = reader.fieldnames
            elif reader.fieldnames != fieldnames:
                errors.append(f"shard {shard}: header mismatch")
        expected_rows = 128 if shard == 254 else 143
        if len(rows) != expected_rows:
            errors.append(f"shard {shard}: {len(rows)} rows != {expected_rows}")
        for row in rows:
            row["source_workflow_run_id"] = run_id
            row["source_shard_id"] = str(shard)
            row["source_artifact"] = f"timing-{shard}-{run_id}"
            proposed.append(row)
        shard_manifest.append({
            "shard_id": shard,
            "workflow_run_id": run_id,
            "artifact_name": f"timing-{shard}-{run_id}",
            "selected_file": str(path.relative_to(args.artifacts)),
            "rows": len(rows),
            "sha256": sha256(path),
        })

    with args.reference.open(newline="") as fh:
        reference = list(csv.DictReader(fh))
    if len(proposed) != 36_450 or len(reference) != 36_450:
        errors.append(f"row counts proposed/reference={len(proposed)}/{len(reference)}")
    proposed_by_id = {row["model_id"]: row for row in proposed}
    reference_by_id = {row["model_id"]: row for row in reference}
    if len(proposed_by_id) != len(proposed) or len(reference_by_id) != len(reference):
        errors.append("model_id is not unique within one timing source")
    if set(proposed_by_id) != set(reference_by_id):
        errors.append("proposed/reference evaluation identities differ")

    identity_mismatches = []
    for model_id in sorted(set(proposed_by_id) & set(reference_by_id)):
        proposed_row = proposed_by_id[model_id]
        reference_row = reference_by_id[model_id]
        if (proposed_row["model_hash"], proposed_row["L"], float(proposed_row["H"])) != (
                reference_row["model_hash"], reference_row["L"], float(reference_row["H"])):
            identity_mismatches.append(model_id)
    if identity_mismatches:
        errors.append(f"proposed/reference bounded identity mismatches: {len(identity_mismatches)}")

    expected_mismatches = [
        row["model_id"] for row in proposed
        if row["model_hash"] != row["expected_model_hash"]
        or row["L"] != row["expected_L"]
        or float(row["H"]) != float(row["expected_H"])
    ]
    if expected_mismatches:
        errors.append(f"proposed frozen-spec mismatches: {len(expected_mismatches)}")
    bad_status = [
        row["model_id"] for row in proposed
        if row["execution_status"] != "COMPLETED" or row["verdict"] not in {"VER", "AMB"}
        or row["error"]
    ]
    if bad_status:
        errors.append(f"proposed non-completed/error records: {len(bad_status)}")
    if {row["n_repeats"] for row in proposed} != {"3"}:
        errors.append("proposed timing repetitions are not uniformly three")

    proposed_fields = list(fieldnames or []) + [
        "source_workflow_run_id", "source_shard_id", "source_artifact"
    ]
    with (args.out / "proposed_36450_recovered.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=proposed_fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(sorted(proposed, key=lambda row: row["model_id"]))
    with (args.out / "selected_shard_manifest.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(shard_manifest[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(shard_manifest)

    problem_rows = []
    evaluations = defaultdict(list)
    for row in proposed:
        evaluations[(row["model_hash"], row["L"], row["H"])].append(row)
    for key, rows in sorted(evaluations.items()):
        reference_rows = [reference_by_id[row["model_id"]] for row in rows]
        proposed_runtime = [float(row["runtime_total_median"]) for row in rows]
        reference_runtime = [float(row["runtime_total_median"]) for row in reference_rows]
        problem_rows.append({
            "model_hash": key[0], "L": key[1], "H": key[2],
            "evaluation_count": len(rows),
            "proposed_runtime_median_s": statistics.median(proposed_runtime),
            "reference_runtime_median_s": statistics.median(reference_runtime),
        })
    with (args.out / "bounded_problem_statistics.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(problem_rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(problem_rows)

    proposed_runtime = [float(row["runtime_total_median"]) for row in proposed]
    reference_runtime = [float(row["runtime_total_median"]) for row in reference]
    summary = {
        "PASS": not errors,
        "errors": errors,
        "selected_shards": len(shard_manifest),
        "evaluation_runs": len(proposed),
        "distinct_evaluation_ids": len(proposed_by_id),
        "distinct_model_hashes": len({row["model_hash"] for row in proposed}),
        "distinct_bounded_problems": len(evaluations),
        "proposed": {
            "method": "proposed verifier rerun",
            "environment": "GitHub Actions; Python 3.13",
            "timing_repeats": 3,
            "execution_status": dict(Counter(row["execution_status"] for row in proposed)),
            "verdict": dict(Counter(row["verdict"] for row in proposed)),
            "runtime_median_s": statistics.median(proposed_runtime),
            "runtime_p95_s": percentile(proposed_runtime, 0.95),
            "runtime_max_s": max(proposed_runtime),
            "workflow_run_ids": sorted({row["source_workflow_run_id"] for row in proposed}),
        },
        "reference": {
            "method": "archived corrected MSCG-LP reference",
            "environment": "original measurement machine",
            "timing_repeats": sorted({int(row["n_repeats"]) for row in reference}),
            "execution_status": dict(Counter(row["execution_status"] for row in reference)),
            "verdict": dict(Counter(row["verdict"] for row in reference)),
            "runtime_median_s": statistics.median(reference_runtime),
            "runtime_p95_s": percentile(reference_runtime, 0.95),
            "runtime_max_s": max(reference_runtime),
            "source_sha256": sha256(args.reference),
        },
        "identity_mismatches": identity_mismatches,
        "expected_spec_mismatches": expected_mismatches,
    }
    (args.out / "baseline_validation.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    (args.out / "source_provenance.md").write_text(
        "# Frozen 36,450-evaluation baseline provenance\n\n"
        "The proposed-verifier records were recovered from 255 GitHub Actions "
        "timing artifacts. Run 41781437233 produced 255 successful shard jobs "
        "although its aggregate workflow concluded failure; shards 0 and 235 "
        "are taken from successful supplement runs 41781103322 and 41789970846. "
        "No evaluation was rerun for v5.\n\n"
        "The archived corrected MSCG-LP reference CSV uses a different method, "
        "machine, and timing protocol. Absolute runtimes from the two sources "
        "must not be pooled. Their 36,450 evaluation IDs and all "
        "(model_hash,L,H) identities match exactly. The 36,450 rows represent "
        "5,589 distinct bounded problems; evaluation-weighted and problem-weighted "
        "statistics must be labeled separately.\n"
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0 if summary["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
