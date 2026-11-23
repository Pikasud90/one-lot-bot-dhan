"""US-079 AC8: the Lab's `simulate()` and the paper book manage a `ratchet` strategy alike.

One fixture: three quiet sessions of one-minute index bars (the 60m ATR's history) and a
Thursday whose 3m signal bar 10:03 closes at 10:06. The Lab fills at the 10:06 open (slippage
0, so its fill is the index open, as the paper book's option trade is anchored); the paper
alert carries the first risk `manage/live.first_risk` reads at 10:06 and is created at
10:05:59, so the paper fill is the same 10:06 bar. (On the live desk the alert comes seconds
after the close, so the paper fill is the next minute: that is the book's fill rule, not this
story's.) Both then walk the same one-minute bars with `manage/ratchet.py`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np
import pandas as pd
import pytest

from onelot.clock import IST
from onelot.data.resample import resample_bars
from onelot.lab.simulator import simulate
from onelot.manage import live as rlive
from onelot.paper.money import to_paise
from onelot.strategies.base import StrategyParams

from .conftest import THU, ist, make_alert
from .test_book_manage import feed

ENTRY = 45210.0
SETTINGS = rlive.RatchetSettings()


def session(day: dt.date, closes: np.ndarray) -> pd.DataFrame:
    idx = pd.date_range(dt.datetime.combine(day, dt.time(9, 15), IST), periods=375, freq="1min")
    o = np.r_[closes[0], closes[:-1]]
    return pd.DataFrame(
        {"open": o, "high": np.maximum(o, closes) + 8.0, "low": np.minimum(o, closes) - 8.0,
         "close": closes, "volume": 1000.0},
        index=idx,
    )  # fmt: skip


def history() -> pd.DataFrame:
    days = [THU - dt.timedelta(days=k) for k in (3, 2, 1)]
    t = np.arange(375)
    return pd.concat([session(d, ENTRY + 60.0 * np.sin(t / 20.0 + k)) for k, d in enumerate(days)])


def trade_day(move: float, path: list[tuple[float, float, float]]) -> pd.DataFrame:
    """THU: flat at ENTRY until 10:05, then `path` from 10:06 (open, high, low as shares of
    the planned move from ENTRY), then flat at its last close until 15:29."""
    rows: list[tuple[float, float, float, float]] = [(ENTRY, ENTRY + 1, ENTRY - 1, ENTRY)] * 51
    for o, h, low in path:
        rows.append((ENTRY + o * move, ENTRY + h * move, ENTRY + low * move, ENTRY + o * move))
    last = rows[-1][3]
    rows += [(last, last + 1, last - 1, last)] * (375 - len(rows))
    idx = pd.date_range(ist(THU, 9, 15), periods=375, freq="1min")
    o, h, low, c = (list(col) for col in zip(*rows, strict=True))
    return pd.DataFrame({"open": o, "high": h, "low": low, "close": c, "volume": 1e3}, index=idx)


def frame(fine: pd.DataFrame) -> pd.DataFrame:
    """The strategy frame on 3m bars: one long entry on the 10:03 bar, nothing else."""
    f = resample_bars(fine, "3m")
    sig = f.index == ist(THU, 10, 3)
    f["atr"] = f["atr_trail"] = 30.0
    f["long_entry"], f["short_entry"] = sig, False
    f["long_stop"], f["short_stop"] = f["close"] - 50.0, np.nan
    f["long_score"] = f["short_score"] = np.nan
    f["long_exit"] = f["short_exit"] = False
    return f


PARAMS = StrategyParams(
    timeframe="3m", ratchet=True, target_r=0.0, trail_atr_mult=0.0, stop_atr_min=0.0,
    stop_atr_max=1000.0, min_risk_pct=0.0, max_risk_pct=10.0, cooldown_bars=0,
    max_trades_per_day=99, exit_time="15:15",
)  # fmt: skip

PATHS = {
    "trail": [(0, 0.06, -0.03), (0.03, 0.27, 0.02), (0.25, 0.52, 0.23), (0.5, 0.84, 0.48),
              (0.8, 0.87, 0.75), (0.78, 0.8, 0.6)],
    "breakeven": [(0, 0.05, -0.02), (0.02, 0.28, 0.0), (0.2, 0.22, 0.01)],
    "lock": [(0, 0.05, -0.02), (0.1, 0.53, 0.05), (0.4, 0.42, 0.3)],
    "first stop": [(0, 0.05, -0.02), (-0.1, 0.0, -0.4)],
    "target": [(0, 0.05, -0.02), (0.3, 1.02, 0.25)],
}  # fmt: skip


@pytest.mark.parametrize("name", list(PATHS))
def test_the_lab_and_the_paper_book_agree(make_book, engine, name: str) -> None:
    past = history()
    risk = rlive.first_risk(past, ist(THU, 10, 6), "3m", SETTINGS)
    assert rlive.usable(risk)
    day = trade_day(SETTINGS.target_r * risk, PATHS[name])
    fine = pd.concat([past, day])
    # the ATR read at the signal uses no bar closing after it: the trade day changes nothing
    assert rlive.first_risk(fine, ist(THU, 10, 6), "3m", SETTINGS) == risk

    (lab,) = simulate(frame(fine), PARAMS, "BANKNIFTY", "test", slippage_pct=0.0, fine=fine)

    alert: dict[str, Any] = make_alert("INDEX", created=ist(THU, 10, 5, 59))
    alert["manage"] |= {"target_r": 3.0, "trail_atr_mult": 0.0, "ratchet": {
        "stop_atr": 1.5, "atr_tf": "60m", "target_r": 3.0, "extend": True, "risk": risk,
        "cost": 0.0}}  # fmt: skip
    book = make_book(clock=lambda: ist(THU, 15, 31))
    book.on_alert(alert, ist(THU, 10, 6))
    events = book.step(ist(THU, 15, 31), feed(BANKNIFTY=day))
    (filled,) = [e for e in events if e.kind == "FILLED"]
    (closed,) = [e for e in events if e.kind == "CLOSED"]

    assert (lab.entry_ts, lab.entry_price) == (filled.ts, filled.detail["bar_open_paise"] / 100)
    bin_of = pd.Timestamp(closed.ts).floor("3min")
    assert (lab.exit_ts, lab.exit_reason) == (bin_of, closed.detail["reason"].lower())
    assert to_paise(lab.legs[-1].price) == closed.detail["price_paise"]
