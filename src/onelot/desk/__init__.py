"""Live Desk: every watchlist instrument tracked by several strategies at once.

    config.py     config/desk.yaml -> DeskConfig
    data.py       bar sources (Dhan 1-minute when the Data API is active, else Yahoo 2-minute, delayed)
    economics.py  one-unit signal economics + the alert text (stock = 1 share, index = 1 option lot)
    tracker.py    one instrument: fine bars -> each strategy's closed bars -> verdicts, regime, health
    desk.py       the orchestrator: polling thread, signals, paper positions, persistence, snapshot

Nothing here can place an order. Signals are alerts; positions exist only after the
operator says "taken" and are closed only when the operator says "closed".
"""

from .config import DeskConfig, load_desk_config
from .desk import Desk

__all__ = ["Desk", "DeskConfig", "load_desk_config"]
