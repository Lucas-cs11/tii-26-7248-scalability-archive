> **Archived companion repository.** The canonical, reviewer-facing package for TII-26-7248 lives at **[Lucas-cs11/tii-26-7248-reproducibility](https://github.com/Lucas-cs11/tii-26-7248-reproducibility)** (frozen protocols, execution code, result summaries and claim-to-evidence map). This repository is kept as the historical archive with the full raw data.

---

# Scalability benchmark v2 (TII-26-7248, Figure 6)

Frozen controlled experiments for the bounded-verification scalability study.

- `protocol/manifest.json` — frozen task matrix (1,260 tasks)
- `protocol/tasks/shard_NNN.csv` — per-shard task lists (140 shards)
- `runner/run_cell.py` — one verification cell (verifier + counters)
- `runner/run_shard.py` — shard executor, 120 s per-cell timeout, append-only JSONL

Experiments
- `dense_L_sweep` (660 tasks): 12 fixed TPNs, consecutive L from L0-2 to L0+8,
  H fixed at 9*(n_parts*ops), 5 replicates.
- `size_wip` (600 tasks): fixed base parameters, n_configs in {2,3,4,5} x
  wip in {1,2,3}, seeds 0-9, 5 replicates, L=18, H=72.

Result of record: `results/raw/shard_NNN.jsonl`, one JSON object per cell.