"""Live Desk: every watchlist instrument tracked by several strategies at once.

    config.py     config/desk.yaml -> DeskConfig
    data.py       bar sources (Dhan 1-minute when the Data API is active, else Yahoo 2-minute, delayed)
    economics.py  one-unit signal economics + the alert text (stock = 1 share, index = 1 option lot)
    tracker.py    one instrument: fine bars -> each strategy's closed bars -> verdicts, regime, health
    rules.py      pure position-management helpers shared with the paper book (US-002)
    desk.py       the orchestrator: polling thread, signals, paper positions, persistence, snapshot

Nothing here can place an order. Signals are alerts; positions exist only after the
operator says "taken" and are closed only when the operator says "closed".

The exports load lazily (PEP 562), so `import onelot.desk.rules` stays pure: it never
drags in the orchestrator, its notifiers or its data sources (US-002 NFR).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import DeskConfig, load_desk_config
    from .desk import Desk

__all__ = ["Desk", "DeskConfig", "load_desk_config"]


def __getattr__(name: str) -> Any:
    if name == "Desk":
        from .desk import Desk

        return Desk
    if name in ("DeskConfig", "load_desk_config"):
        from . import config

        return getattr(config, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
