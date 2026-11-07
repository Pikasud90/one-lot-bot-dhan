"""Owner feedback OF-5: the Home strategy scoreboard from the paper book.

    PaperLedger.by_strategy(*, grade=None, since=None, until=None) -> {strategy: StrategyTotals}
    scoreboard_view(totals, primary=(), open_counts=None) -> [row, ...]

The same rules as `totals()` (exact integer paise, a corrupt cell warned about and left out),
split by strategy, with NO_FILL counted apart and the R multiple averaged where risk is known.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from sqlalchemy import text

from onelot.store.schema import paper_trades

from .conftest import THU, ist


def insert(
    engine,
    alert_id: str,
    strategy: str,
    net: int | None,
    *,
    status: str = "CLOSED",
    gross: int | None = None,
    costs: int | None = 100,
    risk: int | None = 500,
    exit_ts: dt.datetime | None = None,
    grade: str = "live",
) -> None:
    ts = exit_ts or ist(THU, 11, 0)
    closed = status == "CLOSED"
    gross = (net + (costs or 0)) if gross is None and net is not None else gross
    row: dict[str, Any] = {
        "alert_id": alert_id,
        "status": status,
        "close_reason": "TARGET" if closed else "NO_FILL",
        "grade": grade,
        "data_source": "dhan",
        "instrument_key": "RELIANCE",
        "series_key": "RELIANCE",
        "kind": "STOCK",
        "side": "BUY",
        "direction": "long",
        "strategy": strategy,
        "timeframe": "15m",
        "confidence": 60.0,
        "product": "equity_intraday",
        "qty": 1,
        "entry_ts": ts if closed else None,
        "fill_bar_ts": ts if closed else None,
        "exit_ts": ts if closed else None,
        "last_bar_ts": ts if closed else None,
        "resolution_min": 1,
        "entry_paise": 100_000 if closed else None,
        "exit_paise": 100_000 + (gross or 0) if closed else None,
        "gross_paise": gross if closed else 0,
        "costs_paise": costs if closed else 0,
        "net_paise": net if closed else 0,
        "risk_paise": risk if closed else None,
        "created_at": ist(THU, 10, 5),
        "expires_at": ist(THU, 10, 15),
        "plan": {"alert": {}, "levels": {}},
        "updated_at": ts,
    }
    with engine.begin() as conn:
        conn.execute(paper_trades.insert().values(**row))


def by_strategy(engine, **kw):
    from onelot.paper.ledger import PaperLedger

    return PaperLedger(engine).by_strategy(**kw)


def view(totals, primary=(), opened=None):
    from onelot.paper.view import scoreboard_view

    return scoreboard_view(totals, primary, opened)


class TestByStrategy:
    def test_each_strategy_gets_its_own_exact_sums(self, engine):
        insert(engine, "a1", "ema_cross", 900)
        insert(engine, "a2", "ema_cross", -300)
        insert(engine, "a3", "ema_cross", 0)  # break-even: a trade, neither win nor loss
        insert(engine, "b1", "gap_fade", -250)
        insert(engine, "b2", "gap_fade", None, status="NO_FILL")
        got = by_strategy(engine)
        ema, gap = got["ema_cross"], got["gap_fade"]
        assert (ema.trades, ema.wins, ema.losses, ema.net_paise) == (3, 1, 1, 600)
        assert (ema.won_paise, ema.lost_paise) == (900, -300)
        assert ema.costs_paise == 300 and type(ema.net_paise) is int
        assert (gap.trades, gap.no_fill, gap.net_paise) == (1, 1, -250)
        assert ema.r_trades == 3 and ema.r_sum == pytest.approx((900 - 300 + 0) / 500)

    def test_a_close_without_a_priced_exit_is_not_a_trade(self, engine):
        insert(engine, "u", "ema_cross", None, costs=None, gross=None)
        assert by_strategy(engine)["ema_cross"].trades == 0

    def test_a_corrupt_net_is_left_out_never_truncated(self, engine):
        insert(engine, "ok", "ema_cross", 863)
        insert(engine, "bad", "ema_cross", 863)
        with engine.begin() as conn:
            conn.execute(text("UPDATE paper_trades SET net_paise = 862.5 WHERE alert_id = 'bad'"))
        ema = by_strategy(engine)["ema_cross"]
        assert (ema.trades, ema.net_paise) == (1, 863)

    def test_a_zero_or_missing_risk_counts_the_money_but_not_the_r(self, engine):
        insert(engine, "z", "ema_cross", 400, risk=0)
        insert(engine, "n", "ema_cross", 400, risk=None)
        ema = by_strategy(engine)["ema_cross"]
        assert ema.trades == 2 and ema.net_paise == 800 and ema.r_trades == 0

    def test_the_window_is_half_open_on_the_close_time(self, engine):
        insert(engine, "early", "ema_cross", 100, exit_ts=ist(THU, 10, 0))
        insert(engine, "late", "ema_cross", 200, exit_ts=ist(THU, 12, 0))
        got = by_strategy(engine, since=ist(THU, 10, 0), until=ist(THU, 12, 0))
        assert got["ema_cross"].net_paise == 100

    def test_the_grade_filter_and_its_validation(self, engine):
        insert(engine, "l", "ema_cross", 100)
        insert(engine, "d", "ema_cross", 200, grade="delayed")
        assert by_strategy(engine, grade="delayed")["ema_cross"].net_paise == 200
        with pytest.raises(ValueError):
            by_strategy(engine, grade="LIVE")
        with pytest.raises(ValueError):
            by_strategy(engine, since=dt.datetime(2026, 11, 5, 10, 0))  # naive


class TestScoreboardView:
    def test_primary_strategies_come_first_in_the_owners_order_even_without_trades(self, engine):
        insert(engine, "m1", "mean_reversion", 5000)
        insert(engine, "e1", "ema_cross", 900)
        rows = view(by_strategy(engine), ["supertrend_vol", "ema_cross"], {"ema_cross": 2})
        assert [r["strategy"] for r in rows] == ["supertrend_vol", "ema_cross", "mean_reversion"]
        st, ema, mr = rows
        assert st["primary"] and st["trades"] == 0 and st["win_rate"] is None
        assert st["net"] is None and st["avg_net"] is None and st["profit_factor"] is None
        assert ema["open"] == 2 and ema["net"] == "9.00" and ema["win_rate"] == 100.0
        assert not mr["primary"] and mr["net"] == "50.00"

    def test_money_is_exact_and_the_ratios_are_rounded(self, engine):
        insert(engine, "w1", "ema_cross", 1001)
        insert(engine, "w2", "ema_cross", 1000)
        insert(engine, "l1", "ema_cross", -1000)
        (row,) = view(by_strategy(engine), ["ema_cross"])
        assert row["net"] == "10.01" and row["costs"] == "3.00"
        assert row["avg_net"] == "3.34"  # 1001 / 3 = 333.67 paise -> 334, half-even
        assert row["win_rate"] == 66.7 and row["profit_factor"] == 2.0
        assert row["avg_r"] == pytest.approx(round(1001 / 3 / 500, 2))

    def test_other_strategies_are_ordered_by_net(self, engine):
        insert(engine, "x", "orb", -100)
        insert(engine, "y", "squeeze", 300)
        rows = view(by_strategy(engine))
        assert [r["strategy"] for r in rows] == ["squeeze", "orb"]

    def test_a_strategy_listed_twice_is_shown_once(self, engine):
        rows = view({}, ["ema_cross", "ema_cross"])
        assert [r["strategy"] for r in rows] == ["ema_cross"]
