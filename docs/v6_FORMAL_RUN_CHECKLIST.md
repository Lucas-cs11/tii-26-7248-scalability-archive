# v6 formal 8,280-cell Modal run — launch checklist

Everything below is executed by the Lead after the Modal pilot, recovery and
budget gates pass. The formal matrix runs **only on Modal**; the independent
audit, statistics and Figure 6 run on GitHub Actions.

## 0. Gate check

```bash
cd /tmp/v6work
export TIIPY="/Users/lucas/Library/Mobile Documents/com~apple~CloudDocs/WorkingFiles/在投文章/TII_TPN/02_experiments/firing_interval_adjustment/.venv/bin/python"
"$TIIPY" -m compileall -q modal_app scripts   # no syntax drift
.venv/bin/python scripts/evaluate_v6_formal_gates.py   # must print decision GO
```

Expected inputs: `work/v6_differential_v2_acceptance/acceptance.json` (GO),
`work/v6_modal_pilot/{audit.json,pilot_summary.json}`,
`work/v6_modal_recovery/recovery_test.json`, the two Pilot audits, the budget
state `work/v6_modal_budget/budget.json` (cap $10, spent ≈ $0.02) and the
validated 8,280-task grid.

## 1. Freeze the protocol (only after GO)

```bash
"$TIIPY" scripts/build_v6_modal_shards.py --formal          # 120 x 69 + SHA256SUMS
"$TIIPY" scripts/build_v6_frozen_protocol.py                # writes the frozen YAML+JSON
git add protocol/v6_formal_tasks protocol/v6_formal_manifest.json \
        protocol/v6_modal_shards protocol/v6_modal_shards_manifest.json \
        protocol/scalability_v6_frozen_protocol.yaml protocol/scalability_v6_frozen_protocol.json
git commit -m "v6: freeze the formal 8,280-cell Modal protocol"
git push
```

## 2. Clean the formal volume prefix

The single-shard smoke test and the failed pilot attempts wrote under
`scalability_v6_formal` with older commits; the formal run must start on an
empty prefix. Their records are already downloaded as evidence
(`work/v6_modal_smoke_shard_000/`).

```bash
MV=".../firing_interval_adjustment/.venv/bin/modal"
"$MV" volume rm -r tii-v6-scalability scalability_v6_formal
"$MV" volume ls tii-v6-scalability                 # must no longer list it
```

## 3. Launch

```bash
"$TIIPY" scripts/run_v6_modal.py run \
  --confirm-budget --max-parallel 20 \
  --volume-prefix scalability_v6_formal \
  --run-id "formal-$(git rev-parse --short HEAD)" \
  > /tmp/v6_formal_run.log 2>&1 &
```

Expected: 43.24 CPU-hours ≈ **$3.43**, worst 138 CPU-hours ≈ $10.94; the budget
guard stops spawning new shards at **$9.50** and records `BUDGET_STOPPED`
(never an overspend). Wall clock ≈ 2.2 h expected / 6.9 h worst at 20 parallel.

## 4. Monitor (read-only)

```bash
"$TIIPY" scripts/run_v6_modal.py status --run-id <run id> --volume-prefix scalability_v6_formal
"$MV" app list --json | head
"$TIIPY" scripts/modal_budget_guard.py check          # metered spend delta
```

## 5. Download, publish and audit on GitHub Actions

```bash
"$TIIPY" scripts/run_v6_modal.py download --run-id <run id> \
  --volume-prefix scalability_v6_formal
# publish raw records as a release asset (never committed)
zip -qr /tmp/v6_formal_raw.zip work/v6_formal_run_<run id>
gh release create v6-formal-<run id> /tmp/v6_formal_raw.zip --draft \
  --title "v6 formal raw records (<run id>)"
# dispatch the audit/statistics/Figure-6 workflow
gh workflow run scalability_v6_formal_audit.yml \
  -f release_tag=v6-formal-<run id> -f run_id=<run id> \
  -f source_sha=$(git rev-parse HEAD) -f expected_tasks=8280
```

## 6. Package

```bash
"$TIIPY" scripts/build_v6_modal_package.py \
  --run-dir work/v6_formal_run_<run id> \
  --audit-dir work/v6_formal_audit \
  --analysis-dir work/v6_analysis_formal \
  --out outputs/TII_Scalability_Benchmark_v6_Modal_FullScale_Results.zip
```

The package builder writes `MANIFEST.sha256`, re-opens the ZIP and verifies every
hash (`self_check: PASS`).
