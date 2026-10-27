"""Paper fill model.

Fills are pessimistic on purpose: you BUY THE ASK and SELL THE BID, plus
configured slippage. An optimistic fill model is the most common way a
backtest manufactures an edge that evaporates live (v1 defect A2).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..costs import Quote, effective_entry, effective_exit


@dataclass
class Fill:
    price: float
    basis: str
    note: str = ""


class PaperFillModel:
    def __init__(self, exec_cfg, tick: float = 0.05):
        self.exec_cfg = exec_cfg
        self.tick = tick

    def entry(self, quote: Quote) -> Fill | None:
        if not quote.is_valid:
            return None
        return Fill(effective_entry(quote, self.exec_cfg, self.tick), "ask+slippage")

    def exit(self, quote: Quote, level: float | None = None) -> Fill | None:
        """Exit at the bid. When a stop level is given, fill at the WORSE of the
        bid and the level — a gap through the stop does not fill at the stop."""
        if not quote.is_valid:
            return None
        bid_fill = effective_exit(quote.bid, self.exec_cfg, self.tick)
        if level is None:
            return Fill(bid_fill, "bid-slippage")
        return Fill(min(bid_fill, effective_exit(level, self.exec_cfg, self.tick)),
                    "worse of bid and level",
                    "a gap through the stop does not fill at the stop")
