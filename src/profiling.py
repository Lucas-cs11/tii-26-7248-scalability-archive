"""Optional low-overhead instrumentation for the bounded verifier.

Profiling is strictly opt-in.  Every hook is guarded by ``profile is not None``
so that the normal verification path keeps its exact behaviour and no
additional function-call side effects leak into results.

A profile is a plain ``dict`` of counters (int/float).  Timings are accumulated
under ``runtime_*`` keys with :func:`time.perf_counter`, and counts under
``*_calls`` / ``*_count`` keys.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Dict, Optional

Profile = Dict[str, float]


def bump(profile: Optional[Profile], key: str, amount: float = 1) -> None:
    """Increment a counter if profiling is enabled."""
    if profile is not None:
        profile[key] = profile.get(key, 0) + amount


@contextmanager
def timed(profile: Optional[Profile], key: str):
    """Accumulate elapsed wall time in ``profile[key]`` if profiling is enabled."""
    if profile is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        profile[key] = profile.get(key, 0.0) + (time.perf_counter() - started)
