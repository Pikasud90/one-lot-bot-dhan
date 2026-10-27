"""Broker adapters — READ ONLY BY CONSTRUCTION.

INVARIANT I1: the `BrokerAdapter` protocol exposes no method that can place,
modify or cancel an order, and no implementation imports a broker write API.
This is not a convention, it is enforced by
`tests/test_invariant_no_trading.py`, which greps the whole source tree.

The human places every order, manually, on their own terminal.
"""

from .base import (  # noqa: F401
    Bar,
    BrokerAdapter,
    BrokerError,
    ChainRow,
    ChainSnapshot,
    Contract,
    QuoteSnapshot,
    RateLimitError,
    TokenExpiredError,
)
