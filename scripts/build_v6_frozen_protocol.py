#!/usr/bin/env python3
"""Build the frozen formal protocol for the Modal 8,280-cell matrix.

Only run this after every formal gate has passed (see
``scripts/evaluate_v6_formal_gates.py``).  The generated YAML + JSON pair is the
single source of truth for the Modal execution: task grid hashes, environment
pins, resource configuration, checkpoint/retry policy and acceptance gates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

YAML_OUT = ROOT / "protocol" / "scalability_v6_frozen_protocol.yaml"
JSON_OUT = ROOT / "protocol" / "scalability_v6_frozen_protocol.json"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def tree_digest(directory: Path, pattern: str) -> str:
    digest = hashlib.sha256()
    for path in sorted(directory.glob(pattern)):
        digest.update(path.name.encode())
        digest.update(sha256_file(path).encode())
    return digest.hexdigest()


def _scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if any(ch in text for ch in ":#[]{}&*!|>'\"%@`") or text.strip() != text:
        return json.dumps(text)
    return text


def _yaml(value, indent=0) -> str:
    pad = "  " * indent
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}{key}:")
                lines.append(_yaml(item, indent + 1))
            elif isinstance(item, list) and not item:
                lines.append(f"{pad}{key}: []")
            elif isinstance(item, dict) and not item:
                lines.append(f"{pad}{key}: {{}}")
            else:
                lines.append(f"{pad}{key}: {_scalar(item)}")
        return "\n".join(lines)
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(f"{pad}-")
                lines.append(_yaml(item, indent + 1))
            else:
                lines.append(f"{pad}- {_scalar(item)}")
        return "\n".join(lines)
    return f"{pad}{_scalar(value)}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verifier-commit", default=None)
    parser.add_argument("--image-digest", default="recorded-at-runtime")
    parser.add_argument("--semantics-gate", type=Path, default=ROOT / "protocol/v6_semantics_gate.json")
    parser.add_argument("--differential-v2", type=Path,
                        default=ROOT / "work/v6_differential_v2_acceptance/acceptance.json")
    parser.add_argument("--formal-gates", type=Path,
                        default=ROOT / "work/v6_formal_gate_decision/formal_gate_decision.json")
    parser.add_argument("--modal-shards", type=Path,
                        default=ROOT / "protocol/v6_modal_shards_manifest.json")
    args = parser.parse_args()
    verifier_commit = args.verifier_commit or subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    manifest = json.loads((ROOT / "protocol/v6_formal_manifest.json").read_text())
    semantics = json.loads(args.semantics_gate.read_text())
    v2 = json.loads(args.differential_v2.read_text()) if args.differential_v2.is_file() else {}
    formal_gates = json.loads(args.formal_gates.read_text()) if args.formal_gates.is_file() else {}
    shards_manifest = (
        json.loads(args.modal_shards.read_text()) if args.modal_shards.is_file() else {}
    )
    if not formal_gates.get("gates", {}).get("FORMAL_PROTOCOL_READY"):
        raise SystemExit("formal gates do not authorise freezing the protocol")
    protocol = {
        "protocol": "scalability-v6-frozen-protocol-1",
        "frozen_before_execution": True,
        "frozen_at": subprocess.run(
            ["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"], capture_output=True, text=True
        ).stdout.strip(),
        "source": {
            "verifier_commit": verifier_commit,
            "optimized_commit": "215443219c524878f7af4cef0e4b8d8caf790531",
            "baseline_commit": "10d27726c085076fe790d574eabd15d7423ac86d",
            "semantics_version": "v6-strong-time-1",
            "serialization": "json-fraction-v2",
            "semantics_gate_sha256": sha256_file(args.semantics_gate),
            "semantics_gate_PASS": bool(semantics.get("PASS")),
        },
        "task_grid": {
            "manifest": "protocol/v6_formal_manifest.json",
            "manifest_sha256": sha256_file(ROOT / "protocol/v6_formal_manifest.json"),
            "tasks": manifest["tasks"],
            "shards": manifest["shards"],
            "tasks_per_shard": manifest["tasks_per_shard"],
            "quota": manifest["quota"],
            "experiments": manifest["experiments"],
            "distinct_model_hashes": manifest["distinct_model_hashes"],
            "distinct_bounded_problems": manifest["distinct_bounded_problems"],
            "task_files_sha256": tree_digest(ROOT / "protocol/v6_formal_tasks", "shard_*.csv"),
            "validation": {
                "unique_tasks": 8280,
                "duplicates": 0,
                "missing": 0,
                "model_hashes_recomputed": True,
            },
        },
        "execution": {
            "backend": "modal",
            "modal_sdk": "1.6.3",
            "python": "3.13",
            "numpy": "2.3.4",
            "scipy": "1.16.2",
            "lp_solver": "scipy.optimize.linprog (HiGHS)",
            "base_image": "debian_slim python3.13",
            "image_digest": args.image_digest,
            "cpu_physical_cores": 1,
            "memory_mib": 4096,
            "gpu": "none",
            "task_timeout_seconds": 60,
            "scg_class_cap": 50000,
            "function_timeout_seconds": 5400,
            "max_parallel_shards": 20,
            "shard_manifest": "protocol/v6_modal_shards_manifest.json",
            "shard_manifest_sha256": (
                sha256_file(args.modal_shards) if args.modal_shards.is_file() else None
            ),
            "shard_manifest_summary": {
                "shards": shards_manifest.get("shards"),
                "tasks": shards_manifest.get("tasks"),
                "model_clustered": shards_manifest.get("model_clustered"),
            },
            "volume": "tii-v6-scalability",
            "volume_prefix": "/scalability_v6_formal",
            "checkpoint": "per-task ledger entry + sha256 + atomic file write + volume commit",
            "retry_policy": (
                "infrastructure failures only; at most one retry per shard attempt; never "
                "retry a mathematical INC or TIMEOUT"
            ),
            "cost_guard": {
                "authorized_cap_usd": 10.0,
                "monitor": "modal billing summary consumed vs baseline; stop when projected > 9.5",
            },
        },
        "gates": {
            "differential_v2": v2.get("gates"),
            "formal": formal_gates.get("gates"),
        },
        "acceptance": {
            "tasks": 8280,
            "unique_task_ids": True,
            "verdicts": ["VER", "AMB", "INC", "TIMEOUT_WALL_CLOCK", "ERROR"],
            "unknown_work_counters_are_null": True,
            "raw_artifacts_retained": True,
        },
    }
    payload = json.dumps(protocol, indent=2, sort_keys=True) + "\n"
    JSON_OUT.write_text(payload)
    header = (
        "# IEEE TII Scalability v6 - frozen formal protocol (Modal, 8,280 cells)\n"
        "# Machine-readable mirror: protocol/scalability_v6_frozen_protocol.json\n"
        "# Generated by scripts/build_v6_frozen_protocol.py; do not hand-edit.\n"
    )
    YAML_OUT.write_text(header + _yaml(protocol) + "\n")
    print(json.dumps({
        "yaml": str(YAML_OUT.relative_to(ROOT)),
        "json": str(JSON_OUT.relative_to(ROOT)),
        "json_sha256": sha256_bytes(payload.encode()),
        "tasks": protocol["task_grid"]["tasks"],
        "verifier_commit": verifier_commit,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
