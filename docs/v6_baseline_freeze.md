# v6 baseline freeze

The v6 repair branch starts from delivery commit
`97b5d6db32279797580560ffcb82eccfdafd9b32`.  The v5 formal GitHub run used
`dc7cae34310edacd6fdf3df9c2c4382ad715bd77`.

The only changes between those commits are post-run analysis, audit, packaging,
and manuscript-guidance files.  No verifier, SCG, generator, runner, workflow,
or frozen task file changed after the formal run.

Complete source archives for both commits are stored under `work/v6_freeze/`
and are identified by SHA-256 in `protocol/v6_baseline_fingerprints.json`.
The v5 120-shard artifact tree, 8,280 audited records, frozen task manifest,
and delivery ZIP remain unchanged in their existing `work/` and `outputs/`
locations; v6 uses separate paths and model/configuration identities.
