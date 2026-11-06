"""Pure helpers for selecting resumable shard state."""
from __future__ import annotations

import json


FINAL_EXECUTION_STATUSES = frozenset({"COMPLETED", "TIMEOUT_WALL_CLOCK"})


def partition_existing_records(lines):
    final_records = []
    superseded_records = []
    for line in lines:
        try:
            record = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if record.get("execution_status") in FINAL_EXECUTION_STATUSES:
            final_records.append(record)
        else:
            superseded_records.append(record)
    return final_records, superseded_records
