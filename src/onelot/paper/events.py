"""The booked-event value type of the paper book, shared by `book` and `manage`.

It lives in its own module so that `manage` (pure) and `book` (persistence) can both use
it without importing each other. `onelot.paper.book` re-exports it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PaperEvent:
    """One booked event of a paper trade (a `paper_events` row)."""

    trade_id: int
    ts: dt.datetime  # bar or decision time (data time), never wall time
    kind: str  # OPENED | FILLED | NO_FILL | PRICING_UNAVAILABLE | REGRADED | CLOSED | ...
    detail: Mapping[str, Any]
