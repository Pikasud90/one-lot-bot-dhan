"""Replay and backtesting.

*** Requirement R9 — the biggest gap in v1 ***
A strategy that only ever runs forward, live, once, cannot be validated, tuned,
or trusted. v1 had no way to answer "would this have made money?" or even
"would this have fired?" — which makes every parameter in it an unfalsifiable
guess.

The replay harness drives the IDENTICAL alpha and guardian code paths that run
live. There is no parallel backtest implementation to drift out of sync: same
gates, same cost model, same sizer, same state machine.
"""

from .replay import ReplayConfig, ReplayResult, ReplayRunner  # noqa: F401
