<!--
Caption template for Figure 6 of the v6 scalability study.

`scripts/plot_figure6_v6.py` substitutes every {{placeholder}} from the
analysis output (work/v6_analysis_*/statistics.json) and stamps the rendered
copy with a `PILOT-SELFCHECK — NOT FORMAL DATA` banner whenever the analysis
protocol is a Pilot.  The formal 8,280-cell run therefore gets exactly this
wording with the formal denominators and ratios filled in; no manual edit is
needed.  Keep this file as the single source of the published caption.
-->

**Figure 6. Structural scalability of the bounded ambiguity analysis on the v6 matrix.**
(a) Per-cell analysis time (SCG construction plus verification) against model
size, measured as places + transitions, for the pre-registered structural grid
({{a_clause}}); the rank correlation on completed cells is Spearman
$\rho = {{rho_size_time}}$. (b) Paired WIP sensitivity for {{n_b_models}} base
models that differ only in the WIP bound ({{b_clause}}): thin grey segments are
per-model trajectories through completed cells and the black line is the median
of completed cells at each WIP. Where both the WIP-1 reference and the WIP-$k$
cell completed, the median paired growth is {{wip2_ratio}}$\times$ for WIP 2/WIP 1
($n = {{wip2_pairs}}$ pairs) and {{wip3_ratio}}$\times$ for WIP 3/WIP 1
($n = {{wip3_pairs}}$ pairs); a pair with a censored WIP-2/3 cell only provides a
lower bound and is never treated as a measured ratio. (c) Depth-limit $L$
sensitivity at fixed model and horizon $H$ for {{n_l_models}} sweeps
({{c_clause}}); filled markers flag cells whose depth-limited synchronizer
produced candidate histories, i.e. cells that actually reached L-sensitive work,
which happens in {{n_l_candidate_models}} of the {{n_l_models}} sweeps at some
$L$. Time axes are logarithmic.

Right-censoring and inconclusive cells are shown explicitly in every panel. A
TIMEOUT cell is an observation right-censored at the {{cap}} s cell cap: it is
drawn as an open triangle on the dashed ceiling line, is never plotted at its
wall-clock value as if it were a completion time, and is never imputed. INC
denotes a completed but inconclusive analysis (e.g. the SCG state-space limit);
it is drawn as a hollow diamond at its measured — non-zero — analysis time and
is counted separately from VER and AMB rather than being folded into them. All
P50/P90/P95 statistics and all medians are computed over COMPLETED cells only;
each panel reports the total $n$, the number of completed, censored and INC
cells. At the full 8,280-cell scale panels (b) and (c) switch to median + IQR of
completed cells together with an explicit censored-share line, so censored cells
contribute to the figure without being imputed as times. Cell analysis time is
an implementation- and runner-dependent measurement, used only for within-run
paired contrasts and never as an intrinsic model property. Data: protocol
{{protocol}}; all plotted values are distributed as `normalized_tasks.csv` /
`figure6_data.csv` and every statistic in the figure is recomputable from
`statistics.json`.
