#!/usr/bin/env python3
"""Modal spend guard for the v6 formal matrix.

The Modal CLI cannot read the remaining credit balance or a workspace spending
cap (``modal workspace settings`` only exposes environment/image-builder
settings), so the guard works from the workspace billing summary delta measured
around our own runs plus an explicit user-authorized cap.

Never prints tokens or credential material; only metered cost and rates.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = ROOT / "work" / "v6_modal_budget" / "budget.json"
DEFAULT_CLI = os.environ.get(
    "MODAL_CLI",
    "/Users/lucas/Library/Mobile Documents/com~apple~CloudDocs/WorkingFiles/"
    "在投文章/TII_TPN/02_experiments/firing_interval_adjustment/.venv/bin/modal",
)
FORMAL_TASKS = 8280


def billing_summary() -> dict:
    result = subprocess.run(
        [DEFAULT_CLI, "billing", "summary", "--for", "this month", "--json"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"modal billing summary failed: {result.stderr[-400:]}")
    return json.loads(result.stdout)


def rates() -> dict:
    result = subprocess.run(
        [DEFAULT_CLI, "billing", "rates", "--json"], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise SystemExit(f"modal billing rates failed: {result.stderr[-400:]}")
    data = json.loads(result.stdout)
    return {
        "cpu_hour_cost": float(data.get("cpu_hour_cost", 0)),
        "mem_gib_hour_cost": float(data.get("mem_gib_hour_cost", 0)),
    }


def load_state(path: Path) -> dict:
    return json.loads(path.read_text()) if path.is_file() else {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("init", "snapshot", "check", "report"))
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--cap-usd", type=float, default=10.0)
    parser.add_argument("--pilot-tasks", type=int, default=0)
    parser.add_argument("--tasks-per-shard", type=int, default=69)
    parser.add_argument("--max-parallel", type=int, default=20)
    parser.add_argument("--safety-factor", type=float, default=1.30)
    args = parser.parse_args()
    state = load_state(args.state)
    summary = billing_summary()
    metered = float(summary.get("metered_cost", 0))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if args.action == "init":
        state.update({
            "authorized": True,
            "cap_usd": args.cap_usd,
            "credits_confirmed_by_user": True,
            "user_authorization": "$10 Modal cap confirmed by the user (2026-11-07)",
            "created_at": now,
            "baseline_metered_cost_usd": metered,
            "baseline_snapshot": summary,
            "current_spend_usd": 0.0,
        })
        save_state(args.state, state)
        print(json.dumps({"initialized": True, "baseline_metered_cost_usd": metered,
                          "cap_usd": args.cap_usd}, indent=2))
        return 0
    if args.action == "snapshot":
        state.update({"snapshot_at": now, "snapshot_metered_cost_usd": metered})
        save_state(args.state, state)
        print(json.dumps({"snapshot_metered_cost_usd": metered}, indent=2))
        return 0
    baseline = state.get("baseline_metered_cost_usd")
    if baseline is None:
        raise SystemExit("run `init` first")
    spend = round(metered - baseline, 6)
    rates_info = rates()
    projection = None
    if args.pilot_tasks and args.pilot_tasks > 0:
        per_task = spend / args.pilot_tasks
        projection = round(per_task * FORMAL_TASKS * args.safety_factor, 4)
    elif state.get("projected_full_run_usd"):
        projection = state["projected_full_run_usd"]
    state.update({
        "checked_at": now,
        "current_metered_cost_usd": metered,
        "current_spend_usd": spend,
        "rates": rates_info,
        "authorized": state.get("authorized", False),
        "credits_confirmed_by_user": state.get("credits_confirmed_by_user", False),
        "cap_usd": state.get("cap_usd", args.cap_usd),
    })
    if projection is not None:
        state["projected_full_run_usd"] = projection
    cap = float(state.get("cap_usd", args.cap_usd))
    remaining = round(cap - spend, 4)
    projected = state.get("projected_full_run_usd")
    within_cap = (
        spend <= cap and (projected is None or projected <= cap)
    )
    state["within_cap"] = within_cap
    state["remaining_budget_usd"] = remaining
    save_state(args.state, state)
    print(json.dumps({
        "current_spend_usd": spend,
        "cap_usd": cap,
        "remaining_budget_usd": remaining,
        "projected_full_run_usd": projected,
        "within_cap": within_cap,
        "rates": rates_info,
    }, indent=2))
    if args.action == "check" and not within_cap:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
