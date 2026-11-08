"""Owner feedback OF-5: the Home strategy scoreboard from the paper book.

    PaperLedger.by_strategy(*, grade=None, since=None, until=None) -> {strategy: StrategyTotals}
    PaperLedger.by_slot(*, grade=None, since=None, until=None) -> {Slot: StrategyTotals}
    scoreboard_view(totals, primary=(), open_counts=None) -> [row, ...]
    slot_scoreboard_view(totals, primary=(), open_counts=None) -> [row, ...]

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
    timeframe: str = "15m",
    kind: str = "STOCK",
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
        "kind": kind,
        "side": "BUY",
        "direction": "long",
        "strategy": strategy,
        "timeframe": timeframe,
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


def by_slot(engine, **kw):
    from onelot.paper.ledger import PaperLedger

    return PaperLedger(engine).by_slot(**kw)


def slot_view(totals, primary=(), opened=None):
    from onelot.paper.view import slot_scoreboard_view

    return slot_scoreboard_view(totals, primary, opened)


# the paper test's candidates (config/desk.yaml, 2026-11-08)
PAPER_TEST = [
    ("supertrend_vol", "30m", "INDEX"),
    ("supertrend_vol", "60m", "STOCK"),
    ("supertrend_adx", "30m", "STOCK"),
]


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


class TestBySlot:
    """The paper test (2026-11-08): one strategy name on several timeframes or markets is
    several slots, so an earlier setting's trades never count toward a candidate."""

    def test_one_strategy_on_two_timeframes_and_two_markets_is_three_slots(self, engine):
        from onelot.paper.ledger import Slot

        insert(engine, "old", "supertrend_vol", 5000, timeframe="15m")
        insert(engine, "s60", "supertrend_vol", -200, timeframe="60m")
        insert(engine, "i30", "supertrend_vol", 700, timeframe="30m", kind="INDEX")
        insert(engine, "nf", "supertrend_vol", None, status="NO_FILL", timeframe="60m")
        got = by_slot(engine)
        assert set(got) == {
            Slot("supertrend_vol", "15m", "STOCK"),
            Slot("supertrend_vol", "60m", "STOCK"),
            Slot("supertrend_vol", "30m", "INDEX"),
        }
        s60 = got[Slot("supertrend_vol", "60m", "STOCK")]
        assert (s60.trades, s60.losses, s60.net_paise, s60.no_fill) == (1, 1, -200, 1)
        assert got[("supertrend_vol", "30m", "INDEX")].net_paise == 700  # a plain tuple finds it
        assert got[Slot("supertrend_vol", "15m", "STOCK")].net_paise == 5000

    def test_the_slots_add_up_to_the_strategy(self, engine):
        insert(engine, "a", "supertrend_adx", 900, timeframe="30m")
        insert(engine, "b", "supertrend_adx", -300, timeframe="30m", risk=0)
        insert(engine, "c", "supertrend_adx", 450, timeframe="5m", kind="INDEX")
        insert(engine, "d", "supertrend_adx", None, status="NO_FILL", timeframe="5m")
        whole = by_strategy(engine)["supertrend_adx"]
        parts = list(by_slot(engine).values())
        for field in ("trades", "wins", "losses", "no_fill", "net_paise", "costs_paise"):
            assert sum(getattr(p, field) for p in parts) == getattr(whole, field), field
        assert sum(p.r_trades for p in parts) == whole.r_trades == 2
        assert sum(p.r_sum for p in parts) == pytest.approx(whole.r_sum)

    def test_the_window_and_the_grade_follow_by_strategy(self, engine):
        insert(engine, "early", "supertrend_adx", 100, timeframe="30m", exit_ts=ist(THU, 10, 0))
        insert(engine, "late", "supertrend_adx", 200, timeframe="30m", exit_ts=ist(THU, 12, 0))
        insert(engine, "d", "supertrend_adx", 400, timeframe="30m", grade="delayed")
        got = by_slot(engine, since=ist(THU, 10, 0), until=ist(THU, 12, 0), grade="live")
        assert list(got) == [("supertrend_adx", "30m", "STOCK")]
        assert got[("supertrend_adx", "30m", "STOCK")].net_paise == 100
        with pytest.raises(ValueError):
            by_slot(engine, grade="LIVE")
        with pytest.raises(ValueError):
            by_slot(engine, until=dt.datetime(2026, 11, 5, 10, 0))  # naive

    def test_an_empty_timeframe_is_a_slot_of_its_own(self, engine):
        insert(engine, "x", "supertrend_vol", 100, timeframe="")  # the column is NOT NULL
        insert(engine, "y", "supertrend_vol", 200, timeframe="60m")
        assert list(by_slot(engine)) == [
            ("supertrend_vol", "?", "STOCK"),
            ("supertrend_vol", "60m", "STOCK"),
        ]


class TestSlotScoreboardView:
    def test_the_paper_test_rows_never_count_an_earlier_timeframe(self, engine):
        insert(engine, "old1", "supertrend_vol", 5000, timeframe="15m")
        insert(engine, "old2", "supertrend_vol", 729, timeframe="15m")
        rows = slot_view(by_slot(engine), PAPER_TEST)
        assert [(r["strategy"], r["timeframe"], r["kind"], r["primary"]) for r in rows] == [
            ("supertrend_vol", "30m", "INDEX", True),
            ("supertrend_vol", "60m", "STOCK", True),
            ("supertrend_adx", "30m", "STOCK", True),
            ("supertrend_vol", "15m", "STOCK", False),
        ]
        assert all(r["trades"] == 0 and r["net"] is None for r in rows[:3])
        assert rows[3]["trades"] == 2 and rows[3]["net"] == "57.29"

    def test_a_slot_row_has_the_strategy_rows_numbers(self, engine):
        insert(engine, "w1", "ema_cross", 1001)
        insert(engine, "w2", "ema_cross", 1000)
        insert(engine, "l1", "ema_cross", -1000)
        (by_name,) = view(by_strategy(engine), ["ema_cross"], {"ema_cross": 1})
        (row,) = slot_view(
            by_slot(engine), [("ema_cross", "15m", "STOCK")], {("ema_cross", "15m", "STOCK"): 1}
        )
        assert {k: v for k, v in row.items() if k not in ("timeframe", "kind")} == by_name

    def test_open_trades_count_per_slot_and_a_slot_listed_twice_is_shown_once(self):
        from onelot.paper.ledger import Slot

        adx = Slot("supertrend_adx", "30m", "STOCK")
        opened = {adx: 1, Slot("supertrend_adx", "5m", "STOCK"): 4}
        rows = slot_view({}, [adx, tuple(adx)], opened)
        assert len(rows) == 1 and rows[0]["open"] == 1 and rows[0]["primary"]

    def test_other_slots_are_ordered_by_net(self, engine):
        insert(engine, "x", "orb", -100, timeframe="5m", kind="INDEX")
        insert(engine, "y", "orb", 300, timeframe="15m")
        rows = slot_view(by_slot(engine))
        assert [(r["timeframe"], r["kind"], r["primary"]) for r in rows] == [
            ("15m", "STOCK", False),
            ("5m", "INDEX", False),
        ]
