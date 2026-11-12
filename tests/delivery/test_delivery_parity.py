"""US-067 R7: on the fixture bars the product's rule makes exactly the reference's 247 trades.

The fixtures (`tests/fixtures/pullback/`) are the store's cleaned daily bars of the 18 stocks
and NIFTY, 2017-01-02 to 2026-11-06, written with 4 decimals, and the trades the independent
reference (`scripts/alpha_pullback_check.py`, `trades()`) made on the unrounded bars.

Dates, the entry open and the exit price are compared exactly. The net % before the depository
charge is compared within 1e-6 plus what writing the prices with 4 decimals can move it by
(half of 0.0001 on each price): on a Rs 45 stock that is about 2e-4 percentage points, on a Rs
500 stock about 2e-5. The bound is computed per trade from its own prices, never a loose
constant; the largest gap seen is under half of it.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from onelot.config import CostConfig
from onelot.delivery.money import delivery_model
from onelot.delivery.rule import PullbackParams, entry_fill, exit_fill, net_pct, nifty_ok, simulate

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "pullback"


def load(key: str) -> pd.DataFrame:
    return pd.read_csv(FIX / f"daily_{key}.csv.gz")


@pytest.fixture(scope="module")
def rule() -> PullbackParams:
    import json

    raw = json.loads((FIX / "rule.json").read_text())
    return PullbackParams(
        depth=raw["depth"],
        high_lookback=raw["high_lookback"],
        trend_sma=raw["trend_sma"],
        exit_sma=raw["exit_sma"],
        max_sessions=raw["max_sessions"],
        stop=raw["stop"],
        slippage=raw["slippage_per_side"],
    )


@pytest.fixture(scope="module")
def made(rule) -> pd.DataFrame:
    model = delivery_model(CostConfig.load(ROOT / "config" / "costs.yaml"))
    nifty = load("NIFTY")
    rows = []
    for f in sorted(FIX.glob("daily_*.csv.gz")):
        key = f.name[len("daily_") : -len(".csv.gz")]
        if key == "NIFTY":
            continue
        b = load(key)
        ok = nifty_ok(list(b["date"]), list(nifty["date"]), nifty["close"].to_numpy(), rule)
        o, c = b["open"].to_numpy(float), b["close"].to_numpy(float)
        for tr in simulate(o, c, ok, rule):
            raw_exit = c[tr.exit] if tr.at_close else o[tr.exit]
            buy, sell = entry_fill(o[tr.entry], rule), exit_fill(raw_exit, rule)
            rows.append(
                {
                    "stock": key,
                    "signal_date": b["date"][tr.signal],
                    "entry_date": b["date"][tr.entry],
                    "entry_open": o[tr.entry],
                    "exit_date": b["date"][tr.exit],
                    "exit_price": raw_exit,
                    "open_at_end": int(tr.at_close),
                    "sessions": tr.exit - tr.entry,
                    "net_pct_before_dp": net_pct(buy, sell, model),
                }
            )
    return pd.DataFrame(rows)


@pytest.fixture(scope="module")
def expected() -> pd.DataFrame:
    return pd.read_csv(FIX / "expected_trades.csv")


def test_the_rule_makes_the_247_reference_trades_and_no_other(made, expected):
    assert len(expected) == 247
    key = ["stock", "signal_date"]
    assert sorted(map(tuple, made[key].to_numpy())) == sorted(map(tuple, expected[key].to_numpy()))


def test_dates_prices_and_sessions_are_equal(made, expected):
    m = expected.merge(made, on=["stock", "signal_date"], suffixes=("_ref", "_rule"))
    for col in ("entry_date", "exit_date", "open_at_end", "sessions"):
        assert (m[f"{col}_ref"].astype(str) == m[f"{col}_rule"].astype(str)).all(), col
    for col in ("entry_open", "exit_price"):
        assert np.array_equal(m[f"{col}_ref"].to_numpy(), m[f"{col}_rule"].to_numpy()), col


def test_net_before_the_depository_charge_within_the_rounding_of_the_fixture_prices(made, expected):
    m = expected.merge(made, on=["stock", "signal_date"], suffixes=("_ref", "_rule"))
    o, x = m["entry_open_ref"], m["exit_price_ref"]
    bound = 100 * (0.5e-4 / o + 0.5e-4 / x) * (x / o + 1) + 1e-6
    gap = (m["net_pct_before_dp_ref"] - m["net_pct_before_dp_rule"]).abs()
    assert (gap <= bound).all(), m.loc[gap > bound, ["stock", "signal_date"]]
    big = o > 500  # where the 4-decimal rounding is negligible, the 1e-6 of R7 itself
    assert (gap[big] <= 1e-6 + 100 * (1e-4 / o[big] + 1e-4 / x[big])).all()
