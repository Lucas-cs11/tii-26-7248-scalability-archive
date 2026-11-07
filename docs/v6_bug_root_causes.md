# v6 strong-time root cause and repair

## Root cause

The v5 state-class DBM stores candidate firing dates relative to class entry.
`_compute_successor()` projected the selected transition without first adding
the necessary next-event constraints `theta_t <= theta_u` for every other
enabled transition `u`. Consequently, firing a transition could wait beyond a
competitor's finite latest firing time when the selected firing disabled that
competitor. The downstream DBM projection could not recover the discarded
deadline.

This is why the old SCG accepted the normal branch `[2,3]` against an urgent
fault `[0,1]`, while the independent strong-time executor rejected it.

## SCG repair

Before projecting a successor, v6 intersects the current firing domain with
`theta_t - theta_u <= 0` for every simultaneously enabled competitor, computes
exact closure, and rejects a negative-cycle domain. Persistent firing dates are
then shifted by the selected date; newly enabled and self-reenabling
transitions receive fresh static firing domains.

The repair does not change strong-time or single-server semantics.

## Generator repair

Optional inspection and rework formerly placed a delayed transition in direct
competition with an immediate skip/continuation. v6 uses two unobservable
immediate route choices. Only after the inspection or rework route is selected
does its positive-time action become enabled. No observation label was added to
the route-choice transitions.

## Independent evidence

- `tests/test_strong_time_scg.py` contains minimal regressions A--F and
  first-completion truncation.
- `src/semantics/fraction_oracle.py` translates concrete executions into exact
  rational difference constraints and does not import the SCG DBM.
- `tests/test_fraction_oracle.py` compares bounded SCG and oracle trace
  languages for urgency, legal competition, persistent/reset clocks,
  self-reenabling, equality, zero-time, infinity, recovery, concurrency,
  normalization, and first completion.
- `scripts/validate_v6_model_semantics.py` builds exact timed witnesses and
  checks resource, WIP, and workpiece invariants for all 12 families.

The first committed repair is `f9bac23`. The GitHub semantics-validation run
and final frozen semantics SHA are recorded after remote validation succeeds.
