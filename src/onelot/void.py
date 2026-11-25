"""US-105: which alerts named a contract nobody trades.

One rule, read from the alert's own stored facts, so the desk, the paper book, the API and
the study scripts all answer the same. This module imports nothing heavy on purpose.

An INDEX alert whose economics came from a live chain quote (`economics.synthetic` is
exactly False) and whose recorded open interest is a number equal to 0 named a contract
with no open interest: no one trades it, so its paper result says nothing about the
strategy. Such a trade is marked and left out of every score. The alert, its prices and
its exit stay as recorded.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

VOID_REASON = "contract not tradeable: no open interest when the alert was raised"
LEFT_OUT_NOTE = "contracts that were not tradeable (no open interest)"
ROW_TAG = "not counted: the contract had no open interest"


def void_reason(alert: object) -> str | None:
    """The plain reason an alert is not counted, or None. Never raises."""
    try:
        if not isinstance(alert, Mapping) or alert.get("kind") != "INDEX":
            return None
        economics = alert.get("economics")
        if not isinstance(economics, Mapping) or economics.get("synthetic") is not False:
            return None
        option = economics.get("option")
        if not isinstance(option, Mapping):
            return None
        oi: Any = option.get("oi")
        if isinstance(oi, bool) or not isinstance(oi, int | float):
            return None
        if math.isnan(oi) or oi != 0:
            return None
        return VOID_REASON
    except Exception:  # noqa: BLE001 - a malformed payload is simply not void
        return None


def left_out_view(trades: int, net: float | str | None) -> dict[str, Any]:
    """The one answer every score carries when something was left out."""
    return {"trades": int(trades), "net": net, "reason": LEFT_OUT_NOTE}
