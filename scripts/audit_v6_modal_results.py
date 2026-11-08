#!/usr/bin/env python3
"""Independent audit of a downloaded v6 Modal run.

The audit is deliberately independent from the runner: it re-reads the frozen
task CSVs, the downloaded raw records, the completion ledgers, the per-shard
hashes and metrics, and the run manifest, then recomputes every hash itself.

Checks
------
* exactly one final raw record per frozen task: no missing, duplicate,
  unexpected or mis-filed record;
* every record field that the protocol freezes (experiment, family, model,
  bounded problem, configuration, seed, replicate, L, H, mode, cell timeout,
  params, model/structure hash, source SHA, image spec SHA) matches the frozen
  task and the run manifest;
* ``raw_record_hash`` and every file SHA-256 re-computed;
* completion-ledger reconciliation: the last entry of every task matches the
  raw file hash/status; a task that already had a final record is never
  re-executed (no duplicate final entries); attempt history is retained;
* shard metrics and completion status;
* VER / AMB / INC / TIMEOUT_WALL_CLOCK / ERROR classification and
  right-censored wall/RSS statistics by family, experiment, mode and strata.

Exit code is 0 only when the audit passes.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "v6-modal-audit-1"
FINAL_STATUSES = frozenset({"COMPLETED", "TIMEOUT_WALL_CLOCK"})
ALL_STATUSES = frozenset({"COMPLETED", "TIMEOUT_WALL_CLOCK", "ERROR"})
VERDICTS = frozenset({"VER", "AMB", "INC"})
FORMAL_TASKS = 8280

#: Counters of stages that have definitely not executed when a timeout is
#: checkpointed at the named stage.  They must be null/absent, never zero.
LATER_COUNTERS = {
    "MODEL_GENERATED": (
        "scg_classes", "scg_edges", "sync_states", "sync_edges", "candidate_words",
        "temporal_states", "trace_records", "terminal_pairs", "lp_calls",
        "runtime_sync", "runtime_expansion", "runtime_lp",
    ),
    "MODEL_VALIDATED": (
        "sync_states", "sync_edges", "candidate_words", "temporal_states",
        "trace_records", "terminal_pairs", "lp_calls",
        "runtime_sync", "runtime_expansion", "runtime_lp",
    ),
    "SCG_COMPLETE": (
        "sync_states", "sync_edges", "candidate_words", "temporal_states",
        "trace_records", "terminal_pairs", "lp_calls",
        "runtime_sync", "runtime_expansion", "runtime_lp",
    ),
    "SYNCHRONIZER_COMPLETE": (
        "temporal_states", "trace_records", "terminal_pairs", "lp_calls",
        "runtime_expansion", "runtime_lp",
    ),
}

COMPARE_FIELDS = (
    "protocol_version", "experiment", "benchmark_family", "model_id",
    "bounded_problem_id", "configuration_id", "seed", "replicate", "L", "H",
    "mode", "timeout_s",
)

GROUP_FIELDS = ("experiment", "benchmark_family", "mode", "stratum")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def canonical_json(value) -> str:
    return json.dumps(value, default=str, sort_keys=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def percentile(values, probability):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(probability * len(ordered)) - 1)]


def expected_spec(row: dict) -> dict:
    model_value = row.get("model_id") or row.get("model")
    return {
        "tag": row["task_id"],
        "protocol_version": row.get("protocol_version"),
        "experiment_id": row.get("experiment_id", row["experiment"]),
        "benchmark_family": row.get("benchmark_family"),
        "model_id": row.get("model_id") or model_value,
        "bounded_problem_id": row.get("bounded_problem_id"),
        "configuration_id": row.get("configuration_id", row["task_id"]),
        "experiment": row["experiment"],
        "model": model_value,
        "seed": int(row["seed"]),
        "replicate": int(row["replicate"]),
        "L": int(row["L"]),
        "H": float(row["H"]),
        "mode": row["mode"],
        "timeout_s": int(row["timeout_s"]),
        "params": json.loads(row["params"]),
        "expected_model_hash": row.get("expected_model_hash") or None,
        "expected_structure_hash": row.get("expected_structure_hash") or None,
    }


def load_frozen_tasks(source_dir: Path):
    rows: dict[str, dict] = {}
    for path in sorted(Path(source_dir).glob("shard_*.csv")):
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                if row["task_id"] in rows:
                    raise ValueError(f"duplicate frozen task_id {row['task_id']} in {path}")
                rows[row["task_id"]] = row
    if not rows:
        raise FileNotFoundError(f"no frozen shard_*.csv in {source_dir}")
    return rows


def read_jsonl(path: Path):
    entries = []
    if not Path(path).is_file():
        return entries
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                entries.append({"_invalid_json": line[:200]})
    return entries


# --------------------------------------------------------------------------- #
# audit
# --------------------------------------------------------------------------- #
class Audit:
    def __init__(self):
        self.errors: list[str] = []
        self.findings: list[dict] = []
        self.warnings: list[str] = []

    def error(self, message: str):
        if len(self.errors) < 2000:
            self.errors.append(message)

    def finding(self, kind: str, message: str, **detail):
        self.findings.append({"kind": kind, "message": message, **detail})


def audit_run(args) -> dict:
    download = Path(args.download_dir).resolve()
    shard_manifest_path = Path(
        args.shard_manifest or download / "protocol" / "modal_shards_manifest.json"
    )
    source_tasks_dir = Path(
        args.source_tasks or download / "protocol" / "source_tasks"
    )
    run_manifest_path = Path(args.run_manifest or download / "run_manifest.json")
    out_dir = Path(args.out or download / "audit")
    out_dir.mkdir(parents=True, exist_ok=True)
    audit = Audit()

    for path in (shard_manifest_path, source_tasks_dir, run_manifest_path):
        if not path.exists():
            raise FileNotFoundError(f"required audit input missing: {path}")

    shard_manifest = json.loads(shard_manifest_path.read_text(encoding="utf-8"))
    run_manifest = json.loads(run_manifest_path.read_text(encoding="utf-8"))
    frozen = load_frozen_tasks(source_tasks_dir)
    shard_plan: dict[str, dict] = shard_manifest.get("shard_sha256") or {}
    shard_task_ids: dict[str, list[str]] = shard_manifest.get("shard_task_ids") or {}
    if not shard_plan or not shard_task_ids:
        raise ValueError("shard manifest lacks shard_sha256 / shard_task_ids")

    # ---------------------------------------------------------------- inputs
    run_id = run_manifest.get("run_id")
    expected_source_sha = (args.expected_source_sha or run_manifest.get("source_sha")
                           or shard_manifest.get("source_sha"))
    expected_image = (args.expected_image_spec_sha256
                      or (run_manifest.get("image") or {}).get("spec_sha256"))
    if not run_id:
        audit.error("run manifest has no run_id")
    if not expected_source_sha:
        audit.error("no expected source SHA (run manifest / shard manifest / CLI)")

    # frozen shard CSVs on the volume (uploaded by the orchestrator)
    protocol_shards_dir = download / "protocol" / "shards"
    shard_task_counts: dict[str, int] = {}
    if protocol_shards_dir.is_dir():
        for shard in sorted(shard_plan):
            path = protocol_shards_dir / f"shard_{shard}.csv"
            if not path.is_file():
                audit.error(f"missing frozen shard CSV in download: {path.name}")
                continue
            actual = sha256_file(path)
            if actual != shard_plan[shard]:
                audit.error(f"{path.name}: SHA-256 differs from shard manifest")
            with path.open("r", encoding="utf-8", newline="") as handle:
                row_ids = [row["task_id"] for row in csv.DictReader(handle)]
            shard_task_counts[shard] = len(row_ids)
            if sorted(row_ids) != sorted(shard_task_ids.get(shard, [])):
                audit.error(f"{path.name}: task ids differ from shard manifest")
            for task_id in row_ids:
                if task_id not in frozen:
                    audit.error(f"{path.name}: task {task_id} missing from frozen source")
    else:
        for shard in sorted(shard_plan):
            shard_task_counts[shard] = len(shard_task_ids.get(shard, []))
        audit.warnings.append("protocol/shards not downloaded; shard CSV hashes not verified")

    # ------------------------------------------------------ select shard set
    if args.shard_ids:
        requested = [f"{int(part):03d}" for part in args.shard_ids.split(",") if part.strip()]
        unknown = [shard for shard in requested if shard not in shard_task_ids]
        if unknown:
            raise ValueError(f"--shard-ids not in shard manifest: {unknown}")
        expected_shards = sorted(set(requested))
    elif args.allow_partial:
        expected_shards = sorted(
            shard for shard in shard_task_ids
            if (download / "raw" / f"shard_{shard}").is_dir()
        )
        if not expected_shards:
            raise FileNotFoundError(f"no raw shard directories under {download / 'raw'}")
    else:
        expected_shards = sorted(shard_task_ids)
    expected_task_ids = {
        task_id for shard in expected_shards for task_id in shard_task_ids[shard]
    }
    if not args.allow_partial and not args.shard_ids:
        if set(expected_shards) != set(shard_task_ids):
            audit.error("not all shards of the plan were audited")
        if shard_manifest.get("tasks") != len(expected_task_ids):
            audit.error(
                f"shard manifest declares {shard_manifest.get('tasks')} tasks, "
                f"audit expects {len(expected_task_ids)}"
            )
    expected_total = args.expected_tasks or len(expected_task_ids)
    if args.expected_tasks and args.expected_tasks != len(expected_task_ids):
        audit.error(
            f"expected {args.expected_tasks} tasks, shard plan covers {len(expected_task_ids)}"
        )
    if (shard_manifest.get("kind") == "formal" and not args.allow_partial
            and not args.shard_ids and len(expected_task_ids) != FORMAL_TASKS):
        audit.error(
            f"formal run must contain {FORMAL_TASKS} unique tasks, "
            f"plan covers {len(expected_task_ids)}"
        )

    # ------------------------------------------------------------ run manifest
    # ``run_manifest["shards"]`` is a per-shard state mapping in the
    # orchestrator's native manifest, but a reconstructed/aggregate manifest may
    # carry only the shard count.  Treat a non-mapping as "no per-shard state"
    # (the per-shard metrics and ledgers are audited separately below) instead
    # of iterating over an int.
    manifest_shards_raw = run_manifest.get("shards")
    manifest_shards = manifest_shards_raw if isinstance(manifest_shards_raw, dict) else {}
    if manifest_shards:
        for shard in expected_shards:
            if shard not in manifest_shards:
                audit.error(f"run manifest has no state for shard {shard}")
    if run_manifest.get("source_sha") and expected_source_sha != run_manifest.get("source_sha"):
        audit.error("run manifest source_sha differs from expected source SHA")
    if expected_image and (run_manifest.get("image") or {}).get("spec_sha256") not in (None, expected_image):
        audit.error("run manifest image spec SHA differs from expected image spec SHA")
    if run_manifest.get("cell_timeout_s") not in (None, 60, 60.0):
        audit.finding(
            "timeout_cap",
            f"run manifest cell_timeout_s={run_manifest.get('cell_timeout_s')!r} (expected 60)",
        )

    # --------------------------------------------------------------- records
    records: list[dict] = []
    record_files: dict[str, Path] = {}
    raw_root = download / "raw"
    for shard in expected_shards:
        raw_dir = raw_root / f"shard_{shard}"
        if not raw_dir.is_dir():
            audit.error(f"missing raw shard directory: raw/shard_{shard}")
            continue
        files = sorted(raw_dir.glob("*.json"))
        expected_ids = set(shard_task_ids[shard])
        seen_in_shard = set()
        for path in files:
            task_id = path.stem
            if task_id not in expected_ids:
                audit.error(f"unexpected raw record for task not in shard {shard}: {task_id}")
                continue
            if task_id in record_files:
                audit.error(f"duplicate raw record file for task {task_id}")
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                audit.error(f"{path.name}: invalid JSON ({exc})")
                continue
            if not isinstance(record, dict):
                audit.error(f"{path.name}: raw record is not a JSON object")
                continue
            if record.get("task_id") != task_id:
                audit.error(f"{path.name}: task_id {record.get('task_id')!r} != filename")
            record_files[task_id] = path
            records.append(record)
            seen_in_shard.add(task_id)
        missing = sorted(expected_ids - seen_in_shard)
        if missing:
            audit.error(f"shard {shard}: {len(missing)} missing task records, e.g. {missing[:10]}")

    by_id: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        by_id[record.get("task_id")].append(record)
    duplicates = sorted(task_id for task_id, group in by_id.items() if len(group) != 1)
    if duplicates:
        audit.error(f"non-unique final records: {duplicates[:20]}")
    missing_tasks = sorted(expected_task_ids - set(by_id))
    unexpected_tasks = sorted(set(by_id) - expected_task_ids)
    if missing_tasks:
        audit.error(f"missing task ids ({len(missing_tasks)}), e.g. {missing_tasks[:20]}")
    if unexpected_tasks:
        audit.error(f"unexpected task ids ({len(unexpected_tasks)}), e.g. {unexpected_tasks[:20]}")
    if len(records) != expected_total:
        audit.error(f"expected {expected_total} final records, found {len(records)}")

    classification = Counter()
    audited_records = []
    technical_failures = []
    peak_missing = 0
    later_zero_findings = 0
    for record in records:
        task_id = record.get("task_id")
        row = frozen.get(task_id)
        if row is None:
            continue
        status = record.get("execution_status")
        verdict = record.get("verdict")
        if status not in ALL_STATUSES:
            audit.error(f"{task_id}: unexpected execution_status {status!r}")
            classification["UNKNOWN"] += 1
        elif status == "COMPLETED":
            if verdict not in VERDICTS:
                audit.error(f"{task_id}: completed record has invalid verdict {verdict!r}")
                classification["UNKNOWN"] += 1
            else:
                classification[verdict] += 1
        elif status == "TIMEOUT_WALL_CLOCK":
            classification["TIMEOUT_WALL_CLOCK"] += 1
            if verdict is not None:
                audit.error(f"{task_id}: timeout record carries a verdict {verdict!r}")
        else:
            classification["ERROR"] += 1
            technical_failures.append({
                "task_id": task_id, "shard": record.get("shard_id"),
                "execution_status": status, "error": record.get("error"),
                "failure_stage": record.get("failure_stage"),
            })
        shard_of_task = next(
            (shard for shard in expected_shards if task_id in set(shard_task_ids[shard])),
            None,
        )
        if shard_of_task is not None and record.get("shard_id") != int(shard_of_task):
            audit.error(f"{task_id}: shard_id {record.get('shard_id')} != plan shard {shard_of_task}")

        spec = expected_spec(row)
        comparisons = {
            "protocol_version": (record.get("protocol_version"), row["protocol_version"]),
            "experiment": (record.get("experiment"), row["experiment"]),
            "benchmark_family": (record.get("benchmark_family"), row["benchmark_family"]),
            "model_id": (record.get("model_id"), row["model_id"]),
            "bounded_problem_id": (record.get("bounded_problem_id"), row["bounded_problem_id"]),
            "configuration_id": (record.get("configuration_id"), row["configuration_id"]),
            "seed": (int(record.get("seed")), int(row["seed"])),
            "replicate": (int(record.get("replicate")), int(row["replicate"])),
            "L": (int(record.get("L")), int(row["L"])),
            "H": (float(record.get("H")), float(row["H"])),
            "mode": (record.get("mode"), row["mode"]),
            "timeout_s": (int(record.get("timeout_s")), int(row["timeout_s"])),
        }
        for field, (actual, expected) in comparisons.items():
            if actual != expected:
                audit.error(f"{task_id}: {field} {actual!r} != frozen {expected!r}")
        if record.get("params") != json.loads(row["params"]):
            audit.error(f"{task_id}: params differ from frozen task")
        if record.get("configuration_hash") != sha256_bytes(
            json.dumps(spec, sort_keys=True).encode()
        ):
            audit.error(f"{task_id}: configuration_hash mismatch")
        if int(record.get("attempt", -1)) < 0:
            audit.error(f"{task_id}: invalid attempt {record.get('attempt')!r}")

        # model / structure identity: a timeout before MODEL_GENERATED cannot
        # know the hash yet; every other record must match the frozen hashes.
        early_timeout = (
            status == "TIMEOUT_WALL_CLOCK"
            and record.get("failure_stage") == "MODEL_GENERATION"
        )
        if record.get("model_hash") is None and not early_timeout:
            if not (status == "ERROR" and record.get("model_hash") is None):
                audit.error(f"{task_id}: model_hash missing")
        elif record.get("model_hash") not in (None, row.get("expected_model_hash")):
            audit.error(f"{task_id}: model_hash differs from frozen expected_model_hash")
        expected_structure = row.get("expected_structure_hash") or None
        if expected_structure and record.get("structure_hash") not in (None, expected_structure):
            audit.error(f"{task_id}: structure_hash differs from frozen expected_structure_hash")

        # provenance
        if expected_source_sha and record.get("commit_sha") != expected_source_sha:
            audit.error(f"{task_id}: commit_sha differs from expected source SHA")
        if record.get("source_code_hash") not in (None, expected_source_sha):
            audit.error(f"{task_id}: source_code_hash differs from expected source SHA")
        if str(record.get("workflow_run_id")) != str(run_id):
            audit.error(f"{task_id}: workflow_run_id differs from run id")
        if record.get("run_id") not in (None, run_id):
            audit.error(f"{task_id}: run_id differs from run id")
        if not record.get("invocation_id"):
            audit.error(f"{task_id}: invocation_id missing")
        if expected_image and record.get("image_spec_sha256") not in (None, expected_image):
            audit.error(f"{task_id}: image_spec_sha256 differs from expected image spec")
        if record.get("executor") not in (None, "modal"):
            audit.error(f"{task_id}: executor is not 'modal'")

        # hash recomputation
        payload = {key: value for key, value in record.items() if key != "raw_record_hash"}
        actual_hash = sha256_bytes(canonical_json(payload).encode("utf-8"))
        if record.get("raw_record_hash") != actual_hash:
            audit.error(f"{task_id}: raw_record_hash mismatch")

        wall = record.get("wall_s")
        cap = record.get("wall_clock_cap_s")
        try:
            wall_value = float(wall)
        except (TypeError, ValueError):
            audit.error(f"{task_id}: wall_s missing/invalid")
            wall_value = None
        if wall_value is not None:
            if wall_value <= 0:
                audit.error(f"{task_id}: non-positive wall_s")
            if cap is not None and wall_value > float(cap) + 5.0:
                audit.error(f"{task_id}: wall_s {wall_value} exceeds the cap {cap} by >5 s")
        peak = record.get("peak_rss_mb")
        if peak is None:
            peak_missing += 1
            if status in FINAL_STATUSES:
                audit.error(f"{task_id}: peak RSS missing for a final record")
        elif float(peak) <= 0:
            audit.error(f"{task_id}: invalid peak RSS {peak!r}")

        # unexecuted-stage counters must be null, never zero
        if status == "TIMEOUT_WALL_CLOCK":
            stage = record.get("failure_stage")
            counters = record.get("counters") or {}
            for key in LATER_COUNTERS.get(stage, ()):
                if counters.get(key) == 0:
                    later_zero_findings += 1
                    audit.error(
                        f"{task_id}: timeout at {stage} reports unexecuted counter "
                        f"{key}=0 (must be null)"
                    )

        audited = dict(record)
        audited.update({
            "stratum": row.get("stratum"),
            "base_model_id": row.get("base_model_id"),
            "audited_classification": (
                "ERROR" if status == "ERROR"
                else "TIMEOUT_WALL_CLOCK" if status == "TIMEOUT_WALL_CLOCK"
                else verdict
            ),
        })
        audited_records.append(audited)

    if peak_missing:
        audit.finding("rss", f"{peak_missing} record(s) have no peak RSS sample")
    if later_zero_findings:
        audit.finding(
            "null_semantics",
            f"{later_zero_findings} timeout counter(s) were zero instead of null",
        )

    # -------------------------------------------- reference (Pilot portability)
    reference_summary = {"available": False}
    reference_mismatches = []
    reference_status = Counter()
    reference_compared = 0
    if any(row.get("reference_execution_status") for row in frozen.values()):
        available = True
        for record in audited_records:
            task_id = record.get("task_id")
            row = frozen.get(task_id) or {}
            ref_status = row.get("reference_execution_status")
            ref_verdict = row.get("reference_verdict")
            if not ref_status:
                continue
            reference_compared += 1
            reference_status[ref_status] += 1
            if ref_status == "COMPLETED" and record.get("execution_status") == "COMPLETED":
                if record.get("verdict") != ref_verdict:
                    reference_mismatches.append({
                        "task_id": task_id,
                        "reference_verdict": ref_verdict,
                        "record_verdict": record.get("verdict"),
                    })
                    if not getattr(args, "allow_reference_drift", False):
                        audit.error(
                            f"{task_id}: verdict {record.get('verdict')!r} differs from the "
                            f"audited reference {ref_verdict!r}"
                        )
            elif ref_status == "COMPLETED" and record.get("execution_status") != "COMPLETED":
                audit.finding(
                    "modal_regression", f"{task_id}: reference COMPLETED, Modal "
                    f"{record.get('execution_status')}")
            elif ref_status != "COMPLETED" and record.get("execution_status") == "COMPLETED":
                audit.finding(
                    "modal_improved", f"{task_id}: reference {ref_status}, Modal COMPLETED "
                    f"({record.get('verdict')})")
        reference_summary = {
            "available": True,
            "compared": reference_compared,
            "reference_status": dict(reference_status),
            "verdict_mismatches": reference_mismatches,
        }

    # ---------------------------------------------------------------- ledgers
    ledger_root = download / "completion_ledger"
    hash_root = download / "hashes"
    ledger_entry_count = 0
    for shard in expected_shards:
        ledger_path = ledger_root / f"shard_{shard}.jsonl"
        entries = read_jsonl(ledger_path)
        if not entries:
            audit.error(f"shard {shard}: completion ledger missing or empty")
            continue
        ledger_entry_count += len(entries)
        by_task: dict[str, list[dict]] = defaultdict(list)
        for entry in entries:
            if "_invalid_json" in entry:
                audit.error(f"shard {shard}: invalid ledger line")
                continue
            by_task[entry.get("task_id")].append(entry)
        expected_ids = set(shard_task_ids[shard])
        ledger_ids = set(by_task)
        if ledger_ids != expected_ids:
            missing = sorted(expected_ids - ledger_ids)
            extra = sorted(ledger_ids - expected_ids)
            if missing:
                audit.error(f"shard {shard}: ledger missing {len(missing)} task(s), e.g. {missing[:10]}")
            if extra:
                audit.error(f"shard {shard}: ledger has unexpected task(s) {extra[:10]}")
        for task_id in sorted(expected_ids & ledger_ids):
            task_entries = by_task[task_id]
            final_entries = [
                entry for entry in task_entries
                if entry.get("status") in FINAL_STATUSES or entry.get("final") is True
            ]
            final_entries = [entry for entry in final_entries
                             if entry.get("status") in FINAL_STATUSES]
            if len(final_entries) != 1:
                audit.error(
                    f"{task_id}: expected exactly one final ledger entry, "
                    f"found {len(final_entries)}"
                )
            # duplicate appends of the same attempt are a dedupe failure
            fingerprints = Counter(
                (entry.get("attempt"), entry.get("sha256")) for entry in task_entries
            )
            for fingerprint, count in fingerprints.items():
                if count > 1:
                    audit.error(f"{task_id}: duplicate ledger entry {fingerprint}")
            raw_path = record_files.get(task_id)
            raw_file_sha = sha256_file(raw_path) if raw_path and raw_path.is_file() else None
            record = by_id.get(task_id, [{}])[0]
            last = task_entries[-1]
            if raw_file_sha is not None and last.get("sha256") != raw_file_sha:
                audit.error(f"{task_id}: ledger last SHA != raw file SHA")
            if last.get("bytes") is not None and raw_path is not None and raw_path.is_file():
                if int(last["bytes"]) != raw_path.stat().st_size:
                    audit.error(f"{task_id}: ledger byte count != raw file size")
            if last.get("status") != record.get("execution_status"):
                audit.error(f"{task_id}: ledger status != raw record status")
            if last.get("raw_record_hash") not in (None, record.get("raw_record_hash")):
                audit.error(f"{task_id}: ledger raw_record_hash != raw record hash")
            if last.get("run_id") not in (None, run_id):
                audit.error(f"{task_id}: ledger run_id != run id")
            if last.get("invocation_id") != record.get("invocation_id"):
                audit.error(f"{task_id}: ledger invocation_id != raw record invocation_id")
            if any(entry.get("status") in FINAL_STATUSES for entry in task_entries[:-1]):
                audit.error(f"{task_id}: a final task was executed again (dedupe failure)")

        hash_path = hash_root / f"shard_{shard}.jsonl"
        hash_entries = read_jsonl(hash_path)
        if not hash_entries:
            audit.error(f"shard {shard}: hash manifest missing or empty")
            continue
        hash_by_task: dict[str, list[dict]] = defaultdict(list)
        for entry in hash_entries:
            hash_by_task[entry.get("task_id")].append(entry)
        if set(hash_by_task) != set(shard_task_ids[shard]):
            audit.error(f"shard {shard}: hash manifest task set != plan")
        for task_id, entries_for_task in hash_by_task.items():
            raw_path = record_files.get(task_id)
            if raw_path is None or not raw_path.is_file():
                audit.error(f"shard {shard}: hash manifest references missing raw record {task_id}")
                continue
            if entries_for_task[-1].get("sha256") != sha256_file(raw_path):
                audit.error(f"{task_id}: hash manifest SHA != raw file SHA")

    # ---------------------------------------------------------------- metrics
    metrics_root = download / "metrics"
    shard_statuses = {}
    for shard in expected_shards:
        metrics_path = metrics_root / f"shard_{shard}.json"
        if not metrics_path.is_file():
            audit.error(f"shard {shard}: metrics document missing")
            continue
        try:
            metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            audit.error(f"shard {shard}: invalid metrics JSON ({exc})")
            continue
        shard_statuses[shard] = metrics.get("status")
        if metrics.get("run_id") not in (None, run_id):
            audit.error(f"shard {shard}: metrics run_id mismatch")
        if expected_source_sha and metrics.get("source_sha") != expected_source_sha:
            audit.error(f"shard {shard}: metrics source_sha mismatch")
        if expected_image and metrics.get("image_spec_sha256") not in (None, expected_image):
            audit.error(f"shard {shard}: metrics image spec SHA mismatch")
        if not metrics.get("invocation_id"):
            audit.error(f"shard {shard}: metrics invocation_id missing")
        expected_count = shard_task_counts.get(shard, len(shard_task_ids[shard]))
        if metrics.get("tasks_total") != expected_count:
            audit.error(
                f"shard {shard}: metrics tasks_total {metrics.get('tasks_total')} "
                f"!= plan {expected_count}"
            )
        shard_records = [
            record for record in records
            if record.get("task_id") in set(shard_task_ids[shard])
        ]
        status_counts = Counter(record.get("execution_status") for record in shard_records)
        for field, key in (("tasks_completed", "COMPLETED"),
                           ("tasks_timeout", "TIMEOUT_WALL_CLOCK"),
                           ("tasks_error", "ERROR")):
            if metrics.get(field) != status_counts[key]:
                audit.error(
                    f"shard {shard}: metrics {field}={metrics.get(field)} "
                    f"!= raw records {status_counts[key]}"
                )
        if metrics.get("status") == "COMPLETED" and status_counts["ERROR"]:
            audit.error(f"shard {shard}: marked COMPLETED but contains ERROR records")
        if metrics.get("tasks_unfinished"):
            audit.error(f"shard {shard}: {metrics.get('tasks_unfinished')} unfinished task(s)")
        if not metrics.get("environment"):
            audit.finding("environment", f"shard {shard}: container environment not recorded")

    # -------------------------------------------------------------- statistics
    def group_rows(field):
        values = sorted({str(record.get(field)) for record in audited_records})
        return [(field, value) for value in values]

    def summary_row(group_name, group_value, group):
        walls = [float(record["wall_s"]) for record in group
                 if record.get("wall_s") is not None]
        rss = [float(record["peak_rss_mb"]) for record in group
               if record.get("peak_rss_mb") is not None]
        statuses = Counter(record.get("execution_status") for record in group)
        verdicts = Counter(str(record.get("verdict")) for record in group)
        return {
            "group": group_name,
            "value": group_value,
            "tasks": len(group),
            "completed": statuses["COMPLETED"],
            "ver": verdicts["VER"],
            "amb": verdicts["AMB"],
            "inc": verdicts["INC"],
            "timeout": statuses["TIMEOUT_WALL_CLOCK"],
            "error": statuses["ERROR"],
            "timeout_fraction": round(statuses["TIMEOUT_WALL_CLOCK"] / len(group), 6),
            "inc_fraction": round(verdicts["INC"] / len(group), 6),
            "wall_p50_s": percentile(walls, 0.50),
            "wall_p90_s": percentile(walls, 0.90),
            "wall_p95_s": percentile(walls, 0.95),
            "wall_max_s": max(walls) if walls else None,
            "peak_rss_p50_mb": percentile(rss, 0.50),
            "peak_rss_p90_mb": percentile(rss, 0.90),
            "peak_rss_p95_mb": percentile(rss, 0.95),
            "peak_rss_max_mb": max(rss) if rss else None,
            "censored": True,
        }

    stats_rows = []
    for field in GROUP_FIELDS:
        if not any(record.get(field) is not None for record in audited_records):
            continue
        for group_field, value in group_rows(field):
            group = [record for record in audited_records
                     if str(record.get(group_field)) == value]
            stats_rows.append(summary_row(group_field, value, group))
    stats_rows.append(summary_row("ALL", "ALL", audited_records))

    # ------------------------------------------------------------------ output
    audited_records.sort(key=lambda record: (record.get("task_id") or ""))
    with (out_dir / "audited_records.jsonl").open("w", encoding="utf-8") as handle:
        for record in audited_records:
            handle.write(canonical_json(record) + "\n")
    fieldnames = [
        "task_id", "shard", "experiment", "benchmark_family", "stratum", "mode",
        "L", "H", "seed", "replicate", "execution_status", "verdict",
        "analysis_status", "inconclusive_reason", "failure_stage", "wall_s",
        "peak_rss_mb", "run_id", "attempt", "task_attempt", "invocation_id",
        "expected_model_hash", "commit_sha", "image_spec_sha256",
        "raw_record_hash", "raw_artifact",
    ]
    with (out_dir / "records.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore",
                                lineterminator="\n")
        writer.writeheader()
        for record in audited_records:
            row = dict(record)
            row["shard"] = f"{int(record.get('shard_id', 0)):03d}"
            writer.writerow(row)
    if stats_rows:
        with (out_dir / "group_statistics.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(stats_rows[0]), lineterminator="\n")
            writer.writeheader()
            writer.writerows(stats_rows)
    with (out_dir / "technical_failures.json").open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(technical_failures, indent=2, sort_keys=True) + "\n")

    status_counts = Counter(record.get("execution_status") for record in audited_records)
    verdict_counts = Counter(str(record.get("verdict")) for record in audited_records)
    total_wall = sum(float(record["wall_s"]) for record in audited_records
                     if record.get("wall_s") is not None)
    rss_values = [float(record["peak_rss_mb"]) for record in audited_records
                  if record.get("peak_rss_mb") is not None]
    result = {
        "schema": SCHEMA,
        "PASS": not audit.errors,
        "generated_at": utc_now(),
        "download_dir": str(download),
        "run_id": run_id,
        "shard_manifest": str(shard_manifest_path),
        "shard_manifest_sha256": sha256_file(shard_manifest_path),
        "run_manifest_sha256": sha256_file(run_manifest_path),
        "source_sha": expected_source_sha,
        "image_spec_sha256": expected_image,
        "kind": shard_manifest.get("kind"),
        "partial": bool(args.allow_partial or args.shard_ids),
        "shards_expected": len(expected_shards),
        "shards_audited": len([shard for shard in expected_shards
                               if (download / "raw" / f"shard_{shard}").is_dir()]),
        "shard_statuses": shard_statuses,
        "tasks_expected": expected_total,
        "tasks_audited": len(audited_records),
        "unique_task_ids": len({record.get("task_id") for record in audited_records}),
        "distinct_model_hashes": len({record.get("model_hash") for record in audited_records
                                      if record.get("model_hash")}),
        "distinct_bounded_problems": len({record.get("bounded_problem_id")
                                          for record in audited_records}),
        "classification": {
            "VER": verdict_counts["VER"],
            "AMB": verdict_counts["AMB"],
            "INC": verdict_counts["INC"],
            "TIMEOUT_WALL_CLOCK": status_counts["TIMEOUT_WALL_CLOCK"],
            "ERROR": status_counts["ERROR"],
            "UNKNOWN": len(audited_records) - (
                verdict_counts["VER"] + verdict_counts["AMB"] + verdict_counts["INC"]
                + status_counts["TIMEOUT_WALL_CLOCK"] + status_counts["ERROR"]
            ),
        },
        "execution_status": dict(status_counts),
        "verdicts": dict(verdict_counts),
        "ledger_entries": ledger_entry_count,
        "technical_failures": len(technical_failures),
        "technical_failure_tasks": [item["task_id"] for item in technical_failures],
        "peak_rss_missing": peak_missing,
        "peak_rss_max_mb": max(rss_values) if rss_values else None,
        "peak_rss_p95_mb": percentile(rss_values, 0.95),
        "observed_wall_minutes": round(total_wall / 60, 3),
        "timeout_fraction": (
            round(status_counts["TIMEOUT_WALL_CLOCK"] / len(audited_records), 6)
            if audited_records else None
        ),
        "audit_errors": audit.errors,
        "audit_findings": audit.findings,
        "audit_warnings": audit.warnings,
        "reference_comparison": reference_summary,
        "outputs": {
            "audited_records": str(out_dir / "audited_records.jsonl"),
            "records_csv": str(out_dir / "records.csv"),
            "group_statistics": str(out_dir / "group_statistics.csv"),
            "technical_failures": str(out_dir / "technical_failures.json"),
        },
    }
    (out_dir / "audit.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-dir", type=Path, required=True)
    parser.add_argument("--shard-manifest", type=Path, default=None)
    parser.add_argument("--source-tasks", type=Path, default=None)
    parser.add_argument("--run-manifest", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--expected-source-sha", default=None)
    parser.add_argument("--expected-image-spec-sha256", default=None)
    parser.add_argument("--expected-tasks", type=int, default=None)
    parser.add_argument("--allow-partial", action="store_true",
                        help="audit only the shards whose raw directory exists (Pilot subset runs)")
    parser.add_argument("--shard-ids", default=None,
                        help="comma separated shard ids to audit, e.g. 000,001")
    parser.add_argument("--allow-technical-errors", action="store_true",
                        help="keep ERROR records as findings instead of audit failures")
    parser.add_argument("--allow-reference-drift", action="store_true",
                        help="do not fail on Pilot verdict drift against the audited reference")
    parser.add_argument("--json", action="store_true", help="only print the audit JSON")
    args = parser.parse_args(argv)
    try:
        result = audit_run(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"audit input error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        printable = {
            key: result[key] for key in (
                "PASS", "run_id", "kind", "partial", "shards_expected",
                "shards_audited", "tasks_expected", "tasks_audited",
                "unique_task_ids", "classification", "timeout_fraction",
                "technical_failures", "peak_rss_max_mb", "observed_wall_minutes",
                "audit_errors", "audit_warnings",
            )
        }
        print(json.dumps(printable, indent=2, sort_keys=True))
    return 0 if result["PASS"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
