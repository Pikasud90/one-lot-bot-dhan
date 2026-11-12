"""US-067 D8 and the lead's rulings 2, 3 and 5: the rule's daily bars, their freshness, the
session-complete test and the top-up (injected: no broker, no network)."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine

sys.path.insert(0, str(Path(__file__).resolve().parent))

from delivery_helpers import at, bars_of, session_bars, weekdays  # noqa: E402

from onelot.clock import IST, SessionClock, TradingCalendar  # noqa: E402
from onelot.config import CostConfig, SessionCfg  # noqa: E402
from onelot.data.hygiene import clean_intraday, consistent_daily  # noqa: E402
from onelot.data.store import save_bars  # noqa: E402
from onelot.delivery.data import RuleHistory, join_live, problem, session_complete  # noqa: E402
from onelot.delivery.runner import DeliveryDesk  # noqa: E402
from onelot.desk.config import DeliveryCfg, DeskConfig, DeskInstrumentCfg, StrategySlotCfg  # noqa: E402
from onelot.desk.history import rule_daily  # noqa: E402
from onelot.store import init_database  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
CAL = TradingCalendar({dt.date(2026, 11, 10): "Diwali"}, verified=True)
CLOCK = SessionClock(SessionCfg(), CAL)
T1510 = dt.time(15, 10)
SETTLE = dt.timedelta(minutes=10)
DAY = dt.date(2026, 11, 13)  # a Friday


def complete(day, now, last, daily_only=False):
    return session_complete(day, now, last, CLOCK, CAL, T1510, SETTLE, daily_only)


# ── when a session is complete (ruling 2) ───────────────────────────────────────────────────


def test_an_fo_stock_whose_last_bar_is_15_14_is_complete_ten_minutes_after_the_close():
    assert not complete(DAY, at(DAY, "15:39"), dt.time(15, 14))
    assert complete(DAY, at(DAY, "15:40"), dt.time(15, 14))


def test_nifty_whose_last_bar_is_15_29_passes_the_same_test():
    assert complete(DAY, at(DAY, "15:40"), dt.time(15, 29))


def test_a_day_that_stops_at_14_00_is_not_complete_even_the_next_day():
    assert not complete(DAY, at(DAY, "16:00"), dt.time(14, 0))
    assert not complete(DAY, at(DAY + dt.timedelta(days=3), "10:00"), dt.time(14, 0))
    assert not complete(DAY, at(DAY, "16:00"), None)


def test_a_day_not_in_the_calendar_is_complete_the_next_day():
    muhurat = dt.date(2026, 11, 8)  # a Sunday session
    assert not complete(muhurat, at(muhurat, "23:00"), dt.time(19, 14))
    assert complete(muhurat, at(dt.date(2026, 11, 9), "00:01"), dt.time(19, 14))


def test_an_afternoon_special_session_on_a_calendar_holiday_is_complete_the_next_day():
    """2025-10-21 style: Muhurat bars 13:45 to 14:44 on a listed holiday."""
    holiday = dt.date(2026, 11, 10)  # Diwali in CAL
    assert not complete(holiday, at(holiday, "20:00"), dt.time(14, 44))
    assert complete(holiday, at(dt.date(2026, 11, 11), "00:01"), dt.time(14, 44))


def test_a_stored_daily_bar_without_minutes_is_a_whole_session_once_past():
    assert complete(DAY, at(DAY + dt.timedelta(days=1), "00:00"), None, daily_only=True)
    assert not complete(DAY, at(DAY, "20:00"), None, daily_only=True)


# ── the days (D8) ───────────────────────────────────────────────────────────────────────────


def daily(days: list[dt.date], base: float = 100.0) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(d).tz_localize(IST) for d in days])
    c = [base + i for i in range(len(days))]
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0}, index=idx)


def test_live_days_are_laid_on_the_store_except_a_first_live_day_the_store_has():
    stored_days = weekdays(dt.date(2026, 10, 1), 10)
    live = bars_of({stored_days[-1]: (1.0, 2.0), DAY: (500.0, 510.0)})
    live = live[live.index >= at(stored_days[-1], "12:00")]  # the first live day starts mid-session
    h = join_live("SBIN", daily(stored_days), {}, live)
    assert h.days.loc[stored_days[-1], "close"] == 109.0  # the store's whole day stands
    assert h.days.loc[DAY, "open"] == 500.0 and h.days.loc[DAY, "close"] == 510.0
    assert h.last_bar[DAY] == dt.time(15, 14) and h.stored_to == stored_days[-1]
    assert DAY not in h.daily_only and stored_days[0] in h.daily_only


def test_the_stored_day_stands_where_the_store_has_it_to_15_10_and_a_short_one_gives_way():
    """Lead's ruling 5: the stored day is used where the store has its one-minute bars to at
    least 15:10; the live day where the store has it short (not the first live day)."""
    days = weekdays(dt.date(2026, 10, 1), 5)
    live = bars_of({days[-3]: (40.0, 50.0), days[-2]: (50.0, 60.0), days[-1]: (70.0, 80.0)})
    last = {days[-3]: dt.time(14, 0), days[-2]: dt.time(15, 14), days[-1]: dt.time(14, 0)}
    h = join_live("SBIN", daily(days), last, live)
    assert h.days.loc[days[-1], "close"] == 80.0  # short in the store: the live day
    assert h.last_bar[days[-1]] == dt.time(15, 14)
    assert h.days.loc[days[-2], "close"] == 103.0  # whole in the store: it stands
    assert h.days.loc[days[-3], "close"] == 102.0  # the first live day: the store's stands


def test_a_stored_daily_bar_without_minutes_stands():
    days = weekdays(dt.date(2026, 10, 1), 5)
    h = join_live("SBIN", daily(days), {}, bars_of({days[-1]: (70.0, 80.0)}))
    assert h.days.loc[days[-1], "close"] == 104.0


def test_a_bar_with_a_missing_or_non_positive_price_is_left_out_and_noted():
    days = weekdays(dt.date(2026, 1, 5), 230)
    frame = daily(days)
    frame.iloc[10, frame.columns.get_loc("close")] = float("nan")
    frame.iloc[20, frame.columns.get_loc("open")] = 0.0
    h = join_live("SBIN", frame, {}, None)
    assert set(h.bad) == {days[10], days[20]} and len(h.days) == 228
    assert np.isfinite(h.days["close"].to_numpy(float)).all()


def hist(days: list[dt.date]) -> RuleHistory:
    return join_live("SBIN", daily(days), {}, None)


def test_problem_short_history_missing_session_and_no_bar_for_the_day():
    days = weekdays(dt.date(2025, 6, 2), 230)
    covers = TradingCalendar({dt.date(2025, 1, 1): "a", dt.date(2026, 1, 1): "b"}, verified=True)
    h = hist(days)
    assert problem(h, days[-1], covers) is None
    assert "201 needed" in problem(hist(days[-100:]), days[-1], CAL)  # the rule's own minimum
    holed = hist(days[:-5] + days[-4:])
    assert "missing" in problem(holed, days[-1], covers)
    assert problem(h, days[-1] + dt.timedelta(days=3), CAL).startswith("no daily bar")


def test_a_hole_in_a_year_the_calendar_does_not_cover_is_not_flagged():
    """config/nse_holidays.yaml lists 2026 on: 2025-12-25 (Christmas) is no hole."""
    days = [d for d in weekdays(dt.date(2025, 6, 2), 300) if d != dt.date(2025, 12, 25)]
    assert problem(hist(days), days[-1], CAL) is None


def test_a_calendar_holiday_is_not_a_missing_session():
    days = [d for d in weekdays(dt.date(2025, 12, 1), 240) if d != dt.date(2026, 11, 10)]
    assert problem(hist(days), days[-1], CAL) is None


def test_rule_daily_matches_the_research_construction_and_reports_last_bars(tmp_path):
    db = init_database(f"sqlite:///{tmp_path / 's.db'}")
    days = weekdays(dt.date(2026, 9, 1), 30)
    save_bars(db.engine, "SBIN", "1d", daily(days[:20]), "t")
    minutes = bars_of({d: (100.0 + i, 101.0 + i) for i, d in enumerate(days[15:])})
    save_bars(db.engine, "SBIN", "1m", minutes, "t")
    now = at(days[-1], "18:00")
    got, last = rule_daily(db.engine, "SBIN", now)
    want = consistent_daily(daily(days[:20]), clean_intraday("SBIN", minutes))
    assert list(got["close"]) == pytest.approx(list(want["close"]))
    assert last[days[-1]] == dt.time(15, 14) and days[0] not in last


# ── the top-up (ruling 3, lead's rulings after the review) ──────────────────────────────────


class TopUp:
    """An injected top-up: records the keys and its stop flag; may fail or report errors."""

    def __init__(self, fail: Exception | None = None, errors: list | None = None) -> None:
        self.calls: list[list[str]] = []
        self.stops: list = []
        self.fail, self.errors = fail, errors or []

    def __call__(self, keys, stop):
        self.calls.append(keys)
        self.stops.append(stop)
        if self.fail is not None:
            raise self.fail
        return type("Result", (), {"errors": self.errors})()


def delivery_desk(engine, top: TopUp, api=True, top_up=True) -> DeliveryDesk:
    slot = StrategySlotCfg("pullback", "1d", {}, True, "delivery")
    cfg = DeskConfig(
        stocks=(DeskInstrumentCfg("SBIN", "SBIN", "STOCK", (slot,)),),
        delivery=DeliveryCfg(top_up=top_up),
    )
    return DeliveryDesk(
        engine, cfg, CostConfig.load(ROOT / "config" / "costs.yaml"), SessionCfg(), CAL,
        notifier=None, started_at=at(DAY, "08:00"), top_up=top,
        data_api_active=lambda: api, background=False,
    )  # fmt: skip


@pytest.fixture
def engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'd.db'}")


def test_the_top_up_runs_after_hours_for_the_series_and_reloads_the_history(engine):
    top = TopUp()
    d = delivery_desk(engine, top)
    d.load_history(at(DAY, "16:00"))  # an empty store: behind
    d.maybe_top_up(at(DAY, "16:00"))
    assert top.calls == [["SBIN", "NIFTY"]]
    assert d._reload.is_set() and d.top_up_note.startswith("topped up")


def test_at_most_three_tries_in_a_window_at_least_20_minutes_apart(engine):
    top = TopUp(fail=ConnectionError("down"))
    d = delivery_desk(engine, top)
    for hhmm in ("16:00", "16:10", "16:20", "16:30", "16:45", "17:20", "18:00"):
        d.maybe_top_up(at(DAY, hhmm))
    assert len(top.calls) == 3  # 16:00, 16:20, 16:45; then the window is spent
    d.maybe_top_up(at(DAY + dt.timedelta(days=3), "16:00"))  # the next window
    assert len(top.calls) == 4


def test_the_top_up_reports_its_errors(engine):
    top = TopUp(errors=["SBIN 1m: timeout", "NIFTY 1m: 429"])
    d = delivery_desk(engine, top)
    d.maybe_top_up(at(DAY, "16:00"))
    assert "2 error(s)" in d.top_up_note and "SBIN 1m: timeout" in d.top_up_note


def test_the_top_up_stop_flag_turns_true_at_09_00(engine, monkeypatch):
    top = TopUp()
    d = delivery_desk(engine, top)
    d.maybe_top_up(at(DAY, "22:00"))
    (stop,) = top.stops
    monkeypatch.setattr(
        DeliveryDesk, "_wall", staticmethod(lambda: at(DAY + dt.timedelta(days=1), "08:59"))
    )
    assert stop() is False
    monkeypatch.setattr(
        DeliveryDesk, "_wall", staticmethod(lambda: at(DAY + dt.timedelta(days=1), "09:00"))
    )
    assert stop() is True


@pytest.mark.parametrize("hhmm", ["09:00", "11:00", "15:35"])
def test_the_top_up_never_runs_in_market_hours(engine, hhmm):
    top = TopUp()
    delivery_desk(engine, top).maybe_top_up(at(DAY, hhmm))
    assert top.calls == []


def test_the_top_up_needs_the_data_api_and_the_switch(engine):
    top = TopUp()
    delivery_desk(engine, top, api=False).maybe_top_up(at(DAY, "16:00"))
    delivery_desk(engine, top, top_up=False).maybe_top_up(at(DAY, "16:00"))
    assert top.calls == []


def test_a_store_that_is_up_to_date_is_not_topped_up(tmp_path):
    db = init_database(f"sqlite:///{tmp_path / 'u.db'}")
    for key in ("SBIN", "NIFTY"):
        save_bars(db.engine, key, "1m", session_bars(DAY, 1.0, 2.0), "t")
    top = TopUp()
    d = delivery_desk(db.engine, top)
    d.load_history(at(DAY, "16:00"))
    d.maybe_top_up(at(DAY, "16:00"))
    assert top.calls == []


def test_a_failing_top_up_is_logged_and_carries_on(engine, caplog):
    d = delivery_desk(engine, TopUp(fail=ConnectionError("no network")))
    d.maybe_top_up(at(DAY, "16:00"))
    assert "failed" in (d.top_up_note or "") and "top-up" in caplog.text


def test_in_market_hours_the_history_loads_in_the_background(engine, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "onelot.delivery.runner.rule_daily",
        lambda e, k, now: (calls.append(k), (pd.DataFrame(), {}))[1],
    )
    d = delivery_desk(engine, TopUp())
    d._background = True
    d.load_history(at(DAY, "10:00"))
    d._loader.join(5)
    assert calls == ["SBIN", "NIFTY"] and d._history_day == DAY
    assert d._loader.name == "delivery-history"
