# Minimal proposed revision for Supplementary S7-B (v6)

Status marker: **FORMAL**. Guidance only; not inserted into the manuscript
source.

> **S7-B. Scalability protocol, environment and audit (v6).** The protocol was
> frozen before execution (`scalability-v6-frozen-protocol-1`,
> manifest SHA-256 `06cf692d6a6a74586d7fab9697ec785acc9d7fabb38603ca6f5bc116e7210b3d`)
> and executed only on Modal CPU Functions with a pinned container image
> (Python 3.13, NumPy
> 2.3.4, SciPy
> 1.16.2, Modal
> 1.6.3) and a per-task wall-clock
> cap of 60 s;
> container start-up, image pulls and Volume uploads are excluded from that cap
> and reported as cloud overhead. Each task writes one atomic record plus a
> ledger entry with a byte-level SHA-256; interrupted containers resume from the
> ledger without re-running completed tasks. The independent audit reproduced
> 8280 audited records (8280 unique task IDs) with
> 0 missing and 0 duplicates.
>
> Status fields are orthogonal. `execution_status=TIMEOUT_WALL_CLOCK` means the
> external cap expired before analysis completed; `verdict=INC` is emitted only
> after a completed run reports an internal limit (`SCG_STATE_LIMIT` or a
> bounded-analysis limit); `VER`/`AMB` carry certificates. All three are
> reported separately and never merged.
