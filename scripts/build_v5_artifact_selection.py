#!/usr/bin/env python3
"""Select exactly one final artifact per v5 shard, with explicit retries."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--artifacts", type=Path, required=True)
    ap.add_argument("--formal-run-id", required=True)
    ap.add_argument("--authorized-retry-run", action="append", default=[])
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    selected = {
        shard: f"v5-formal-{shard}-{args.formal_run_id}"
        for shard in range(120)
    }
    errors = []
    protocol_path = (
        args.artifacts / f"v5-formal-semantic-{args.formal_run_id}"
        / "formal_protocol_validation.json"
    )
    if not protocol_path.is_file():
        raise SystemExit(f"missing formal protocol validation: {protocol_path}")
    protocol_validation = json.loads(protocol_path.read_text())
    formal_commit_sha = protocol_validation.get("commit_sha")
    if (not protocol_validation.get("PASS")
            or str(protocol_validation.get("workflow_run_id")) != args.formal_run_id
            or not formal_commit_sha):
        raise SystemExit("formal protocol validation provenance is invalid")

    retry_runs = set(args.authorized_retry_run)
    seen_retry_shards = set()
    for path in sorted(args.artifacts.glob("v5-formal-retry-*/attempt_manifest.json")):
        payload = json.loads(path.read_text())
        retry_run = str(payload.get("retry_run_id"))
        if retry_run not in retry_runs:
            continue
        shard = payload.get("shard_id")
        artifact = payload.get("selected_artifact")
        if payload.get("source_formal_run_id") != args.formal_run_id:
            errors.append(f"retry {path} targets another formal run")
            continue
        if payload.get("source_head_sha") != formal_commit_sha:
            errors.append(f"retry {path} used a different source commit")
            continue
        if shard in seen_retry_shards:
            errors.append(f"multiple authorized retries for shard {shard}")
            continue
        if path.parent.name != artifact:
            errors.append(f"retry artifact directory/name mismatch: {path}")
            continue
        raw = path.parent / "raw" / f"shard_{int(shard):03d}.jsonl"
        if not raw.is_file():
            errors.append(f"retry artifact lacks completed raw shard: {path.parent}")
            continue
        selected[int(shard)] = artifact
        seen_retry_shards.add(int(shard))

    # Validate the final choice only after authorized retries have superseded
    # missing or failed base artifacts.
    for shard, artifact in selected.items():
        raw = args.artifacts / artifact / "raw" / f"shard_{shard:03d}.jsonl"
        if not raw.is_file():
            errors.append(f"selected artifact/raw shard is missing: {artifact}")

    if errors:
        raise SystemExit("\n".join(errors))
    payload = {
        "schema": "v5-artifact-selection-1",
        "formal_run_id": args.formal_run_id,
        "formal_commit_sha": formal_commit_sha,
        "authorized_retry_run_ids": sorted(retry_runs),
        "authorized_record_run_ids": sorted({args.formal_run_id, *retry_runs}),
        "selected_by_shard": {str(key): value for key, value in sorted(selected.items())},
        "selected_artifacts": [selected[key] for key in sorted(selected)],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(json.dumps({
        "selected_shards": len(selected),
        "retried_shards": len(seen_retry_shards),
        "authorized_run_ids": payload["authorized_record_run_ids"],
    }, indent=2))


if __name__ == "__main__":
    main()
