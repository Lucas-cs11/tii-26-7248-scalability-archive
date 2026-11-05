# Scalability benchmark v3 (TII-26-7248, Figure 6)

Fixes the v2 design defects (see ../00_v2_defect_audit/defect_root_cause_report.md):

- n_configs is a dead parameter in the generator (extra places/transitions are unreachable,
  declared completion markings unreachable); v3 fixes n_configs = 1 and scales structure by
  n_parts x ops_per_part, which changes reachable behaviour.
- WIP above n_parts does not add concurrency; v3 uses the n_parts = 3 family, with untimed
  reachability evidence that WIP 1/2/3 give 1/2/3 parts in process.
- L sweep runs continuous L over longer routes in two modes: "first" (production verifier)
  and "complete" (complete_set=True).

Protocol: protocol/manifest.json (486 cells, 120 shards, 120 s per cell).
Results: results/raw/shard_NNN.jsonl (one JSON object per cell).
