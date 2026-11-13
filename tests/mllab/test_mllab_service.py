"""US-069: the Machine Learning Lab's matrix: bars, closed bars only, the cache, failures."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from onelot.clock import IST
from onelot.mllab import models as m
from onelot.mllab import service as svc

INDICES = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"]
STOCKS = [f"STOCK{i:02d}" for i in range(18)]
INSTRUMENTS = [{"key": k, "label": k.title(), "kind": "INDEX"} for k in INDICES] + [
    {"key": k, "label": k.title(), "kind": "STOCK"} for k in STOCKS
]


def sessions(last_day: str, n: int) -> list[pd.Timestamp]:
    days = pd.bdate_range(end=last_day, periods=n)
    return [pd.Timestamp(d).tz_localize(IST) for d in days]


def minute_bars(last_day: str = "2026-11-12", days: int = 48, seed: int = 0, until=None):
    """One-minute bars 09:15..15:29 for `days` sessions (the desk keeps about 70 days)."""
    idx = pd.DatetimeIndex(
        [
            d + pd.Timedelta(hours=9, minutes=15 + i)
            for d in sessions(last_day, days)
            for i in range(375)
        ]
    )
    if until is not None:
        idx = idx[idx <= until]
    rng = np.random.default_rng(seed)
    close = 1000 * np.exp(np.cumsum(rng.normal(0, 0.0008, len(idx))))
    return pd.DataFrame(
        {
            "open": close,
            "high": close * 1.0005,
            "low": close * 0.9995,
            "close": close,
            "volume": 1.0,
        },
        index=idx,
    )


def daily_bars(last_day: str = "2026-11-12", days: int = 500, seed: int = 0) -> pd.DataFrame:
    idx = pd.DatetimeIndex(
        [d + pd.Timedelta(hours=9, minutes=15) for d in sessions(last_day, days)]
    )
    close = 1000 * np.exp(np.cumsum(np.random.default_rng(seed).normal(0, 0.012, days)))
    return pd.DataFrame(
        {"open": close, "high": close, "low": close, "close": close, "volume": 1.0}, index=idx
    )


def ist(text: str) -> pd.Timestamp:
    return pd.Timestamp(text).tz_localize(IST)


class Clock:
    def __init__(self, at: str) -> None:
        self.now = ist(at).to_pydatetime()

    def __call__(self):
        return self.now


def lab_for(bars: dict, clock: Clock, instruments=INSTRUMENTS) -> svc.MlLab:
    def getter(key):
        got = bars[key]
        if isinstance(got, Exception):
            raise got
        return got

    return svc.MlLab(getter, instruments, clock)


@pytest.fixture(scope="module")
def desk_bars() -> dict:
    return {
        r["key"]: (
            minute_bars("2026-11-13", seed=i, until=ist("2026-11-13 11:00")),
            daily_bars(seed=i),
        )
        for i, r in enumerate(INSTRUMENTS)
    }


class TestMatrix:
    @pytest.mark.parametrize("tf", svc.TIMEFRAMES)
    def test_every_timeframe_gives_23_rows_by_5_models(self, desk_bars, tf):
        lab = lab_for(desk_bars, Clock("2026-11-13 11:01"))
        out = lab.matrix(tf)
        json.dumps(out, allow_nan=False)
        assert out["timeframe"] == tf and [x["key"] for x in out["models"]] == list(m.MODELS)
        assert len(out["rows"]) == 23
        assert [r["kind"] for r in out["rows"][:5]] == ["INDEX"] * 5
        for row in out["rows"]:
            assert set(row["cells"]) == set(m.MODELS)
            for model, cell in row["cells"].items():
                assert cell["note"] is None, (tf, model, cell["note"])
                assert cell["lo95"] < cell["lo68"] < cell["mean"] < cell["hi68"] < cell["hi95"]
                # 48 sessions give 94 closed four-hour bars: the backtest starts once a
                # model has its minimum behind it (60 bars, 30 for ewma and empirical)
                need = m.MIN_BARS_SIMPLE if model in ("ewma", "empirical") else m.MIN_BARS
                assert cell["backtest"]["steps"] == min(100, cell["n"] - need)

    def test_the_first_matrix_is_quick_enough(self, desk_bars):
        """23 instruments, 48 sessions of one-minute bars, 15m: the first matrix's time."""
        lab = lab_for(desk_bars, Clock("2026-11-13 11:01"))
        t0 = time.perf_counter()
        lab.matrix("15m")
        took = time.perf_counter() - t0
        print(f"first 15m matrix for 23 instruments: {took:.2f} s")
        assert took < 30

    def test_the_last_bar_and_the_next_one(self, desk_bars):
        lab = lab_for(desk_bars, Clock("2026-11-13 11:01"))
        row = lab.matrix("60m")["rows"][0]
        # the 10:15 hour closes at 11:15: not yet; the last closed one is 09:15 today
        assert row["last_bar_ts"] == "2026-11-13T09:15:00+05:30"
        assert row["next_bar_ts"] == "2026-11-13T10:15:00+05:30"
        day = lab.matrix("1d")["rows"][0]
        assert day["last_bar_ts"] == "2026-11-12T09:15:00+05:30"  # today closes at midnight
        assert day["next_bar_ts"] == "2026-11-13T09:15:00+05:30"


class TestClosedBarsOnly:
    def test_the_forming_bar_is_left_out(self):
        until = ist("2026-11-12 10:21")
        fine = minute_bars(last_day="2026-11-12", days=10, until=until)
        now = ist("2026-11-12 10:22").to_pydatetime()
        bars = svc.closed_bars(fine, None, "15m", now)
        assert bars.index[-1] == ist("2026-11-12 10:00")  # the 10:15 bar closes at 10:30
        wild = fine.copy()
        wild.loc[wild.index >= ist("2026-11-12 10:15"), "close"] *= 3
        lab_a = lab_for({"X": (fine, None)}, Clock("2026-11-12 10:22"), [{"key": "X"}])
        lab_b = lab_for({"X": (wild, None)}, Clock("2026-11-12 10:22"), [{"key": "X"}])
        assert lab_a.matrix("15m")["rows"] == lab_b.matrix("15m")["rows"]

    def test_next_bar_after_the_session_is_the_next_trading_day(self):
        friday = ist("2026-11-13 15:15")
        assert svc.next_bar_start(friday, "15m") == ist("2026-11-16 09:15")
        assert svc.next_bar_start(ist("2026-11-13 13:15"), "4h") == ist("2026-11-16 09:15")
        assert svc.next_bar_start(ist("2026-11-12 09:15"), "4h") == ist("2026-11-12 13:15")
        assert svc.next_bar_start(friday.normalize(), "1d") == ist("2026-11-16 09:15")


class TestCache:
    def test_no_new_bar_no_model_work(self, desk_bars, monkeypatch):
        clock = Clock("2026-11-13 11:01")
        lab = lab_for(desk_bars, clock)
        calls = []
        real = m.forecast
        monkeypatch.setattr(m, "forecast", lambda *a, **k: calls.append(a[0]) or real(*a, **k))
        first = lab.matrix("15m")
        done = len(calls)
        assert done >= 23 * 5
        clock.now += pd.Timedelta(minutes=10)  # 11:11: the 11:00 bar has not closed yet
        assert lab.matrix("15m")["rows"] == first["rows"]
        assert len(calls) == done
        clock.now += pd.Timedelta(minutes=5)  # 11:16: the clock is past 11:15, but the data
        lab.matrix("15m")  # ends at 11:01: a bar is closed only when the bars reach its end
        assert len(calls) == done
        lab.matrix("30m")
        assert len(calls) > done  # another timeframe is its own entry

    def test_a_new_closed_bar_recomputes_that_instrument(self):
        fine = minute_bars(days=10, until=ist("2026-11-12 10:29"))
        bars = {"X": (fine, None)}
        clock = Clock("2026-11-12 10:29")
        lab = lab_for(bars, clock, [{"key": "X"}])
        lab.matrix("15m")
        assert lab.model_runs == 1
        bars["X"] = (minute_bars(days=10, until=ist("2026-11-12 10:30")), None)
        clock.now = ist("2026-11-12 10:31").to_pydatetime()
        row = lab.matrix("15m")["rows"][0]
        assert lab.model_runs == 2 and row["last_bar_ts"] == "2026-11-12T10:15:00+05:30"


class TestFailures:
    def test_one_failing_instrument_leaves_the_others(self, desk_bars):
        bars = dict(desk_bars)
        bars["STOCK03"] = RuntimeError("feed down")
        bars["STOCK04"] = (None, None)
        out = lab_for(bars, Clock("2026-11-13 11:01")).matrix("15m")
        rows = {r["key"]: r for r in out["rows"]}
        assert rows["STOCK03"]["cells"]["garch"]["note"] == "bars unavailable: RuntimeError"
        assert rows["STOCK04"]["cells"]["arima"]["note"] == "no closed bars yet"
        assert rows["STOCK05"]["cells"]["arima"]["note"] is None
        json.dumps(out, allow_nan=False)

    def test_one_failing_model_leaves_the_others(self, desk_bars, monkeypatch):
        real = m.forecast

        def flaky(model, *a, **k):
            if model == "garch":
                raise FloatingPointError("boom")
            return real(model, *a, **k)

        monkeypatch.setattr(m, "forecast", flaky)
        out = lab_for(desk_bars, Clock("2026-11-13 11:01"), INSTRUMENTS[:2]).matrix("60m")
        cells = out["rows"][0]["cells"]
        assert cells["garch"]["note"] == "garch failed: FloatingPointError"
        assert cells["arima"]["note"] is None

    def test_short_history_says_so_and_keeps_the_simple_models(self):
        fine = minute_bars(days=12)  # 24 four-hour bars, 12 days
        out = lab_for({"X": (fine, daily_bars(days=40))}, Clock("2026-11-13 11:01"), [{"key": "X"}])
        cells = out.matrix("1d")["rows"][0]["cells"]
        assert cells["garch"]["note"] == "too few bars to trust: 40 of 60"
        assert cells["ewma"]["note"] is None and cells["empirical"]["note"] is None

    def test_unknown_timeframe(self, desk_bars):
        with pytest.raises(ValueError):
            lab_for(desk_bars, Clock("2026-11-13 11:01")).matrix("5m")


def test_desk_bars_getter_reads_the_trackers_frames_and_keeps_them():
    fine, daily = minute_bars(days=10), daily_bars(days=100)
    tracker = SimpleNamespace(
        bars=fine,
        daily_history=daily,
        series=SimpleNamespace(key="NIFTY", label="Nifty 50"),
        cfg=SimpleNamespace(kind="INDEX"),
    )
    desk = SimpleNamespace(trackers={"NIFTY": tracker})
    get = svc.desk_bars_getter(desk)
    a = get("NIFTY")
    assert len(a[0]) == len(fine) and a[1].index[-1] == fine.index[-1].normalize() + pd.Timedelta(
        hours=9, minutes=15
    )
    assert get("NIFTY") is a  # unchanged frames: the cleaned ones are kept
    assert get("MISSING") == (None, None)
    assert svc.desk_instruments(desk) == [{"key": "NIFTY", "label": "Nifty 50", "kind": "INDEX"}]


class TestStoredHistory:
    """60m and 4h: the store's one-minute bars of the last 180 days joined with the live ones."""

    LIVE_FROM = ist("2026-10-08 09:15")  # the desk keeps about 25 sessions

    def setup_lab(self, reader, n_instruments=1):
        live = minute_bars("2026-11-13", days=48, until=ist("2026-11-13 11:00"))
        live = live[live.index >= self.LIVE_FROM]
        insts = INSTRUMENTS[:n_instruments]
        bars = {r["key"]: (live, None) for r in insts}
        return svc.MlLab(lambda k: bars[k], insts, Clock("2026-11-13 11:01"), reader), live

    @staticmethod
    def stored(reads=None, fail=False):
        """125 sessions of stored one-minute bars (about 180 calendar days), up to the 9th."""
        frame = minute_bars("2026-11-09", days=125, seed=7)

        def read(key, now):
            if reads is not None:
                reads.append(key)
            if fail:
                raise OSError("database is locked")
            return frame

        return read

    def test_a_4h_matrix_from_stored_and_live_bars_forecasts_with_all_five_models(self):
        lab, _ = self.setup_lab(self.stored())
        row = lab.matrix("4h")["rows"][0]
        assert row["note"] is None
        for model, cell in row["cells"].items():
            assert cell["note"] is None, (model, cell["note"])
            assert cell["n"] >= 60
        assert row["cells"]["garch"]["backtest"]["steps"] == 100

    def test_the_store_is_read_once(self):
        reads: list[str] = []
        lab, _ = self.setup_lab(self.stored(reads))
        lab.matrix("4h")
        lab.matrix("60m")
        lab.matrix("4h")
        assert reads == [INSTRUMENTS[0]["key"]] and lab.stored_reads == 1
        lab.matrix("15m")  # 15m and 30m stay on the live bars
        assert reads == [INSTRUMENTS[0]["key"]]

    def test_a_failing_read_falls_back_to_the_live_bars(self):
        lab, _ = self.setup_lab(self.stored(fail=True))
        row = lab.matrix("4h")["rows"][0]
        assert row["note"] == "stored bars unreadable (OSError): live bars only"
        assert row["cells"]["garch"]["note"].startswith("too few bars to trust")
        assert row["cells"]["ewma"]["note"] is None
        json.dumps(row, allow_nan=False)

    def test_the_joined_series_is_sorted_without_duplicates_and_live_wins(self):
        stored = minute_bars("2026-11-09", days=125, seed=7)
        live = minute_bars("2026-11-13", days=10, seed=8)  # overlaps the stored 4 sessions
        joined = svc.join_bars(stored, live)
        assert joined.index.is_monotonic_increasing and not joined.index.has_duplicates
        both = stored.index.intersection(live.index)
        assert len(both) > 0
        assert (joined.loc[both, "close"] == live.loc[both, "close"]).all()
        assert len(joined) == len(stored.index.union(live.index))
        assert svc.join_bars(None, live) is live and svc.join_bars(stored, None) is stored

    def test_the_first_4h_matrix_with_the_stored_read_is_quick_enough(self):
        from onelot.data.hygiene import clean_intraday

        frame = minute_bars("2026-11-09", days=125, seed=7)
        lab, _ = self.setup_lab(lambda key, now: clean_intraday(key, frame), n_instruments=23)
        t0 = time.perf_counter()
        out = lab.matrix("4h")
        took = time.perf_counter() - t0
        print(f"first 4h matrix with the stored read, 23 instruments: {took:.2f} s")
        assert all(r["cells"]["arima"]["note"] is None for r in out["rows"])
        assert took < 30


class TestSessionEnd:
    """The last bar of a session counts once the session is over (owner, 2026-11-13)."""

    @staticmethod
    def lab(until: str, at: str, is_trading_day=None):
        fine = minute_bars("2026-11-13", days=30, until=ist(until))
        return svc.MlLab(
            lambda k: (fine, None), [{"key": "X"}], Clock(at), is_trading_day=is_trading_day
        )

    def test_after_the_close_the_last_bar_is_in_and_next_is_the_next_session(self):
        lab = self.lab("2026-11-13 15:29", "2026-11-13 15:40")  # a Friday
        row = lab.matrix("60m")["rows"][0]
        assert row["last_bar_ts"] == "2026-11-13T15:15:00+05:30"
        assert row["next_bar_ts"] == "2026-11-16T09:15:00+05:30"
        assert lab.matrix("4h")["rows"][0]["last_bar_ts"] == "2026-11-13T13:15:00+05:30"
        assert lab.matrix("30m")["rows"][0]["last_bar_ts"] == "2026-11-13T15:15:00+05:30"
        assert lab.matrix("1d")["rows"][0]["last_bar_ts"] == "2026-11-13T09:15:00+05:30"

    def test_earlier_sessions_last_bars_are_in(self):
        bars = svc.closed_bars(
            minute_bars("2026-11-13", days=5), None, "60m", ist("2026-11-13 10:00")
        )
        assert ist("2026-11-12 15:15") in bars.index and ist("2026-11-11 15:15") in bars.index

    def test_during_the_session_the_forming_bar_is_not_used(self):
        row = self.lab("2026-11-13 13:59", "2026-11-13 14:00").matrix("60m")["rows"][0]
        assert row["last_bar_ts"] == "2026-11-13T12:15:00+05:30"  # 13:15 closes at 14:15
        assert row["next_bar_ts"] == "2026-11-13T13:15:00+05:30"
        row = self.lab("2026-11-13 13:59", "2026-11-13 14:00").matrix("4h")["rows"][0]
        assert row["last_bar_ts"] == "2026-11-13T09:15:00+05:30"

    def test_a_feed_that_stops_early_closes_the_bar_after_the_settle_time(self):
        assert (
            self.lab("2026-11-13 15:20", "2026-11-13 15:35").matrix("60m")["rows"][0]["last_bar_ts"]
            == "2026-11-13T14:15:00+05:30"
        )
        assert (
            self.lab("2026-11-13 15:20", "2026-11-13 15:41").matrix("60m")["rows"][0]["last_bar_ts"]
            == "2026-11-13T15:15:00+05:30"
        )

    def test_an_exchange_holiday_is_skipped(self):
        holiday = pd.Timestamp("2026-11-16").date()
        lab = self.lab("2026-11-13 15:29", "2026-11-13 15:40", lambda d: d != holiday)
        assert lab.matrix("60m")["rows"][0]["next_bar_ts"] == "2026-11-17T09:15:00+05:30"
        assert lab.matrix("1d")["rows"][0]["next_bar_ts"] == "2026-11-17T09:15:00+05:30"

    def test_the_desk_session_clock_and_calendar_are_used(self):
        from onelot.clock import SessionClock, TradingCalendar

        cal = TradingCalendar({pd.Timestamp("2026-11-16").date(): "test holiday"})
        t = pd.Timestamp
        cfg = SimpleNamespace(
            market_open=t("09:15").time(),
            market_close=t("13:00").time(),  # a short special session
            first_entry_after=t("09:30").time(),
            no_new_entry_after=t("12:30").time(),
            eod_advisory=t("12:45").time(),
            mandatory_squareoff=t("12:50").time(),
        )
        desk = SimpleNamespace(calendar=cal, session_clock=SessionClock(cfg, cal))
        close = svc.desk_session_close(desk)
        assert close is not None
        assert pd.Timestamp(close(t("2026-11-13").date())) == ist("2026-11-13 13:00")
        lab = svc.MlLab(
            lambda k: (minute_bars("2026-11-13", days=20, until=ist("2026-11-13 12:59")), None),
            [{"key": "X"}],
            Clock("2026-11-13 13:05"),
            session_close=close,
            is_trading_day=svc.desk_trading_day(desk),
        )
        row = lab.matrix("60m")["rows"][0]
        assert row["last_bar_ts"] == "2026-11-13T12:15:00+05:30"  # closed with the session
        assert row["next_bar_ts"] == "2026-11-17T09:15:00+05:30"  # Monday 16th is a holiday
        trading = svc.desk_trading_day(desk)
        assert trading is not None and not trading(pd.Timestamp("2026-11-16").date())
        assert svc.desk_session_close(SimpleNamespace()) is None
        assert svc.desk_trading_day(SimpleNamespace(calendar=None)) is None

    def test_the_row_says_how_many_bars_the_forecast_used(self):
        row = self.lab("2026-11-13 15:29", "2026-11-13 15:40").matrix("4h")["rows"][0]
        assert row["bars"] == 60 and row["cells"]["ewma"]["n"] == 60
