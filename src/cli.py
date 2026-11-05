"""
CLI entry point for TPN diagnosability verification and repair.

Usage:
  python -m src.cli verify --model benchmarks/micro/M01/model.yaml --L 5 --H 20
  python -m src.cli repair --model benchmarks/micro/M01/model.yaml --L 5 --H 20
"""
from __future__ import annotations

import argparse
import json
import sys
from fractions import Fraction
from typing import Any


def _fraction_default(obj: Any) -> Any:
    """JSON serializer for Fraction and other non-serializable types."""
    if isinstance(obj, Fraction):
        return float(obj)
    if hasattr(obj, "__float__"):
        return float(obj)
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")


def cmd_verify(args: argparse.Namespace) -> int:
    from .model.parser import parse_model
    from .model.validation import validate_model
    from .verifier import verify

    # Override L/H from CLI if provided
    tpn, L_file, H_file = parse_model(args.model)

    L = args.L if args.L is not None else L_file
    H = args.H if args.H is not None else H_file

    errors = validate_model(tpn)
    if errors:
        print("Model validation errors:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    budget = args.budget if hasattr(args, "budget") and args.budget else None
    result = verify(tpn, L, H, budget=budget)

    print(f"Result: {result['result']}")

    certs = result.get("certificates", [])
    if certs:
        print(f"Certificates ({len(certs)}):")
        for i, cert in enumerate(certs):
            print(f"  [{i}] phi={cert.phi}")
            print(f"       sigma_N={cert.sigma_N}")
            print(f"       sigma_F={cert.sigma_F}")
            print(f"       overlap={cert.overlap}")

    stats = result.get("stats", {})
    print("Statistics:")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    if args.output:
        # Serialize result to JSON
        out_data = {
            "result": result["result"],
            "certificates": [
                {
                    "phi": list(c.phi),
                    "sigma_N": list(c.sigma_N),
                    "sigma_F": list(c.sigma_F),
                    "overlap": [float(c.overlap[0]), float(c.overlap[1])],
                }
                for c in certs
            ],
            "stats": {
                k: (float(v) if isinstance(v, Fraction) else v)
                for k, v in stats.items()
            },
        }
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(out_data, f, indent=2, default=_fraction_default)
        print(f"Output written to {args.output}")

    return 0 if result["result"] != "INC" else 2


def cmd_repair(args: argparse.Namespace) -> int:
    from .model.parser import parse_model
    from .model.validation import validate_model
    from .verifier import verify
    from .repair.algorithm import repair, Certificate

    tpn, L_file, H_file = parse_model(args.model)
    L = args.L if args.L is not None else L_file
    H = args.H if args.H is not None else H_file

    errors = validate_model(tpn)
    if errors:
        print("Model validation errors:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    # Run initial verification
    budget = args.budget if hasattr(args, "budget") and args.budget else None
    initial = verify(tpn, L, H, budget=budget, complete_set=True)
    print(f"Initial verification: {initial['result']}")

    if initial["result"] == "VER":
        print("Model is already diagnosable. No repair needed.")
        return 0

    if initial["result"] == "INC":
        print("Initial verification inconclusive. Cannot proceed with repair.")
        return 2

    if not initial.get("enumeration_complete"):
        print("Initial verification enumeration incomplete. Cannot proceed with repair.")
        return 2

    certs = initial.get("certificates", [])

    def task_predicate(candidate_tpn) -> bool:
        """Default task predicate: check reachability of completion markings."""
        # Check that at least one completion marking is reachable
        from .scg.builder import build_scg
        try:
            scg = build_scg(candidate_tpn)
            return len(scg.completion_ids) > 0
        except Exception:
            return False

    def verifier_fn(candidate_tpn) -> dict:
        result = verify(candidate_tpn, L, H, budget=budget, complete_set=True)
        if not result.get("enumeration_complete"):
            result["result"] = "INC"
        return result

    repair_result = repair(
        tpn=tpn,
        certificates=certs,
        L=L,
        H=H,
        task_predicate=task_predicate,
        verifier_fn=verifier_fn,
    )

    print(f"Repair status: {repair_result.status}")
    print(f"Iterations: {repair_result.iterations}")
    if repair_result.modified_transitions:
        print(f"Modified transitions: {repair_result.modified_transitions}")

    if repair_result.status == "SUCCESS" and repair_result.repaired_tpn:
        print("Repaired intervals:")
        for t_name in repair_result.modified_transitions:
            orig = tpn.transitions[t_name]
            rep = repair_result.repaired_tpn.transitions[t_name]
            print(f"  {t_name}: {orig.interval} -> {rep.interval}")

    return 0 if repair_result.status == "SUCCESS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="TPN bounded fault diagnosability verifier and timing repair."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Verify command
    verify_parser = subparsers.add_parser(
        "verify", help="Verify bounded fault diagnosability."
    )
    verify_parser.add_argument("--model", required=True, help="Path to model YAML file.")
    verify_parser.add_argument("--L", type=int, default=None, help="Label bound.")
    verify_parser.add_argument("--H", type=float, default=None, help="Time horizon.")
    verify_parser.add_argument("--budget", type=int, default=None, help="Max trace records.")
    verify_parser.add_argument("--output", default=None, help="Output JSON file path.")

    # Repair command
    repair_parser = subparsers.add_parser(
        "repair", help="Perform certificate-guided timing repair."
    )
    repair_parser.add_argument("--model", required=True, help="Path to model YAML file.")
    repair_parser.add_argument("--L", type=int, default=None, help="Label bound.")
    repair_parser.add_argument("--H", type=float, default=None, help="Time horizon.")
    repair_parser.add_argument("--budget", type=int, default=None, help="Max trace records.")
    repair_parser.add_argument("--output", default=None, help="Output JSON file path.")

    args = parser.parse_args()

    if args.command == "verify":
        return cmd_verify(args)
    elif args.command == "repair":
        return cmd_repair(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
