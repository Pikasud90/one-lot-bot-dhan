"""Bank Nifty Trade Assistant — signal-only, human-in-the-loop.

INVARIANT I1: this package contains no order-placement code path. No broker
write API is imported anywhere under `src/`. Enforced by
`tests/test_invariant_no_trading.py`.
"""

__version__ = "2.0.0"
