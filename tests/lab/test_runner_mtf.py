"""The Lab's runner around the multi-timeframe Supertrend, and the known defects of the
stored history it repairs on the way in (2026-11-11).

    DataCache                 repairs what data/hygiene.py knows of as it loads bars;
                              clean=False serves the store as it is
    generate_for_instrument   hands a multi-timeframe strategy the whole one-minute history
                              and the cache's ladders, and a plain strategy neither
    run_backtest              the multi-timeframe Supertrend end to end, from a database

The world of the runner tests is the one the desk's tracker tests use: twelve sessions of
one-minute bars drifting one way, with a hundred stored days before them. Rising, it holds
one long entry (the 10:35 bar of 15 October); falling, one short (the 11:25 bar).
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest
from tests.lab.conftest import make_intraday

from onelot.data import hygiene
from onelot.data.resample import resample_bars
from onelot.data.store import save_bars
from onelot.lab.runner import BacktestConfig, DataCache, generate_for_instrument, run_backtest
from onelot.lab.simulator import simulate
from onelot.store import init_database
from onelot.strategies import mtf_support as ms
from onelot.strategies.base import Context
from onelot.strategies.registry import get_strategy

IST = "Asia/Kolkata"
FIRST_DAY = "2026-10-01"  # a Thursday; the sessions are 1, 2, 5-9 and 12-16 October
PRICES = list(hygiene.PRICE_COLUMNS)
#: series -> (seed, drift per minute) of its world
WORLDS = {"SBIN": (7, 0.00003), "NIFTY": (7, 0.00003), "RELIANCE": (5, -0.00003)}


def at(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz=IST)


def stored_days(
    first_price: float, step: float, days: int = 100, before: str = FIRST_DAY
) -> pd.DataFrame:
    """`days` stored daily bars ending the last weekday before `before`, drifting by `step`
    a day into `first_price`: stamped 09:15 IST, the way the store keeps them."""
    rng = np.random.default_rng(1)
    dates = pd.bdate_range(end=pd.Timestamp(before) - pd.Timedelta(days=1), periods=days)
    close = first_price * np.exp(-step * np.arange(days, 0, -1) + rng.normal(0, 0.002, days))
    idx = pd.DatetimeIndex(
        [pd.Timestamp(d.date(), tz=IST) + pd.Timedelta(hours=9, minutes=15) for d in dates],
        name="ts",
    )
    return pd.DataFrame(
        {
            "open": close * 0.999,
            "high": close * 1.004,
            "low": close * 0.996,
            "close": close,
            "volume": 1e6,
        },
        index=idx,
    )


def world(seed: int, drift: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(one-minute bars of twelve sessions, the stored days before them), drifting one way."""
    minute = make_intraday(days=12, seed=seed, drift=drift, vol=0.0003, freq_min=1, start=FIRST_DAY)
    step = 0.002 if drift > 0 else -0.002
    return minute, stored_days(float(minute["open"].iloc[0]), step)


def session_on(session: pd.DataFrame, day: str, factor: float = 1.0) -> pd.DataFrame:
    """The bars of one session moved to `day`, their prices multiplied by `factor`."""
    out = session.copy()
    out.index = out.index + (at(day) - out.index[0].normalize())
    out[PRICES] = out[PRICES] * factor
    return out


def first_session(bars: pd.DataFrame) -> pd.DataFrame:
    return bars[bars.index.normalize() == bars.index[0].normalize()]


def days_of(bars: pd.DataFrame) -> set[dt.date]:
    return set(bars.index.date)


def same_prices(got: pd.DataFrame, want: pd.DataFrame) -> bool:
    """The same bars, by value (the store's index and a test frame's carry the same zone from
    two libraries, so the frames themselves never compare equal)."""
    return len(got) == len(want) and bool(
        np.allclose(got[PRICES].to_numpy(float), want[PRICES].to_numpy(float))
        and (got.index.asi8 == want.index.asi8).all()
    )


@pytest.fixture(scope="module")
def mtf_engine(tmp_path_factory):
    """A store holding the three worlds: one-minute bars and the stored days of each."""
    db = init_database(f"sqlite:///{tmp_path_factory.mktemp('mtf') / 'lab.db'}")
    for key, (seed, drift) in WORLDS.items():
        minute, history = world(seed, drift)
        save_bars(db.engine, key, "1m", minute, "dhan")
        save_bars(db.engine, key, "1d", history, "dhan")
    yield db.engine
    db.dispose()


def backtest(cache: DataCache, instrument: str = "SBIN", **over):
    cfg = BacktestConfig("supertrend_mtf", instrument, timeframe="5m", **over)
    return run_backtest(cfg, cache)


# ── the stored history's known defects ───────────────────────────────────────────────


def test_a_mock_saturday_is_not_served(lab_engine):
    week = make_intraday(days=5, start="2026-09-01")  # Tuesday 1 to Monday 7 September
    mock = session_on(first_session(week), "2026-09-05", 1.10)  # test prices, 10 % away
    save_bars(lab_engine, "SBIN", "5m", pd.concat([week, mock]).sort_index(), "dhan")
    saturday = dt.date(2026, 9, 5)

    clean = DataCache(lab_engine)
    raw = DataCache(lab_engine, clean=False)

    assert saturday in days_of(raw.bars("SBIN", "5m"))
    assert len(raw.bars("SBIN", "5m")) == len(week) + len(mock)
    assert same_prices(clean.bars("SBIN", "5m"), week)
    assert saturday not in days_of(clean.bars("SBIN", "15m")), "nor in a timeframe made of it"
    assert saturday in days_of(raw.bars("SBIN", "15m"))


@pytest.mark.parametrize(
    "day, real",
    [
        ("2026-11-08", True),  # the Muhurat Sunday of 2026: a real session
        ("2024-05-18", True),  # the special Saturday session of May 2024
        ("2026-11-07", False),  # any other Saturday is a mock session
        ("2021-11-04", False),  # a Thursday the store holds as flat afternoon bars
    ],
)
def test_a_weekend_is_kept_only_when_the_exchange_really_traded(lab_engine, day, real):
    week = make_intraday(days=5, start="2026-09-01")
    bars = pd.concat([week, session_on(first_session(week), day)]).sort_index()
    save_bars(lab_engine, "SBIN", "5m", bars, "dhan")

    served = days_of(DataCache(lab_engine).bars("SBIN", "5m"))

    assert (dt.date.fromisoformat(day) in served) is real
    assert hygiene.is_junk_session(dt.date.fromisoformat(day)) is not real
    assert len(served) == (6 if real else 5)


def test_prices_before_an_unadjusted_demerger_are_put_on_the_scale_after_it(lab_engine):
    bars = make_intraday(days=4, start="2025-01-02", base=480.0)  # 2, 3, 6 and 7 January 2025
    save_bars(lab_engine, "ITC", "5m", bars, "dhan")
    save_bars(lab_engine, "SBIN", "5m", bars, "dhan")
    ((ex_date, factor),) = hygiene.INTRADAY_BACK_ADJUST["ITC"]

    clean = DataCache(lab_engine)
    itc, raw = clean.bars("ITC", "5m"), DataCache(lab_engine, clean=False).bars("ITC", "5m")

    before = np.array([d < ex_date for d in itc.index.date])
    assert ex_date == dt.date(2025, 1, 6) and int(before.sum()) == 150 and factor < 1
    for col in PRICES:
        stored = bars[col].to_numpy(float)
        assert np.allclose(itc[col].to_numpy(float)[before], stored[before] * factor)
        assert np.allclose(itc[col].to_numpy(float)[~before], stored[~before])
    assert np.allclose(itc["volume"].to_numpy(float), bars["volume"].to_numpy(float))
    assert same_prices(raw, bars), "clean=False serves the store as it is"
    assert same_prices(clean.bars("SBIN", "5m"), bars), "a series with no such step is untouched"


def test_the_days_are_those_of_the_minute_bars_with_the_stored_days_as_the_warm_up(lab_engine):
    """A daily level must be on the price scale of the intraday bars it is compared with: the
    days the minute bars cover are made of them, and the older stored days are rescaled."""
    minute = make_intraday(days=3, freq_min=1, start=FIRST_DAY)  # 1, 2 and 5 October
    own = resample_bars(minute, "1d")
    older = stored_days(float(minute["open"].iloc[0]), 0.002, days=10)
    # (one zone object for both parts: the store's zone and a test frame's are two libraries'
    # and two of them in one index would no longer be a time index)
    stored = pd.concat([older, own.tz_convert(older.index.tz)])
    stored[PRICES] = stored[PRICES] * 1.25  # another vendor's scale, a quarter higher
    save_bars(lab_engine, "SBIN", "1m", minute, "dhan")
    save_bars(lab_engine, "SBIN", "1d", stored, "yahoo")

    daily = DataCache(lab_engine).bars("SBIN", "1d")
    raw = DataCache(lab_engine, clean=False).bars("SBIN", "1d")

    assert len(daily) == 13
    assert same_prices(daily.iloc[-3:], own), "the days of the minute bars, exactly"
    assert same_prices(daily.iloc[:10], older), "the older days, back on the intraday scale"
    assert same_prices(raw, stored), "as stored: a quarter away from the intraday bars"


def test_stored_days_alone_are_served_as_they_are(lab_engine):
    days = stored_days(15.0, 0.0, days=30)
    save_bars(lab_engine, "INDIAVIX", "1d", days, "yahoo")

    for clean in (True, False):
        cache = DataCache(lab_engine, clean=clean)
        assert same_prices(cache.bars("INDIAVIX", "1d"), days)
        assert np.allclose(cache.vix().to_numpy(float), days["close"].to_numpy(float))


def test_an_index_still_has_no_volume_after_the_repair(mtf_engine):
    cache = DataCache(mtf_engine)

    assert (cache.bars("NIFTY", "1m")["volume"] == 0).all()
    assert (cache.bars("NIFTY", "1d")["volume"] == 0).all()
    assert (cache.bars("SBIN", "1m")["volume"] > 0).all()


# ── what the runner hands a strategy ─────────────────────────────────────────────────


class Spy:
    """A strategy that keeps what the runner hands it."""

    def __init__(self, name: str) -> None:
        self.real = get_strategy(name, {"timeframe": "5m"})
        self.p = self.real.p
        self.multi_timeframe = self.real.multi_timeframe
        self.seen: list[tuple[pd.DataFrame, Context]] = []

    def generate(self, df, ctx=None):
        self.seen.append((df, ctx))
        return self.real.generate(df, ctx)


def test_a_multi_timeframe_strategy_gets_the_whole_minute_history_and_the_ladders(mtf_engine):
    cache = DataCache(mtf_engine)
    spy = Spy("supertrend_mtf")
    cfg = BacktestConfig("supertrend_mtf", "SBIN", timeframe="5m", start="2026-10-14")

    _, _, frame, _, start_day = generate_for_instrument(cfg, cache, spy)

    ((window, ctx),) = spy.seen
    assert spy.multi_timeframe is True and frame is not None
    assert start_day == at("2026-10-14")
    assert window.index[0] == at("2026-10-09 09:15"), "the window: three warm-up sessions"
    minute = ctx.extras["minute_bars"]
    assert minute is cache.bars("SBIN", "1m")
    assert minute.index[0] == at("2026-10-01 09:15"), "the ladder: everything the store has"
    assert ctx.extras["mtf_cache"] is cache.ladders
    assert ctx.daily is cache.bars("SBIN", "1d") and len(ctx.daily) == 112
    assert ctx.extras["has_volume"] is True and ctx.symbol == "SBIN"


def test_a_plain_strategy_gets_neither(mtf_engine):
    cache = DataCache(mtf_engine)
    spy = Spy("supertrend_vol")

    generate_for_instrument(BacktestConfig("supertrend_vol", "SBIN", timeframe="5m"), cache, spy)

    ((_, ctx),) = spy.seen
    assert spy.multi_timeframe is False
    assert "minute_bars" not in ctx.extras and "mtf_cache" not in ctx.extras
    assert len(cache.ladders) == 0


def test_an_index_is_read_without_volume(mtf_engine):
    cache = DataCache(mtf_engine)
    spy = Spy("supertrend_mtf")

    generate_for_instrument(
        BacktestConfig("supertrend_mtf", "NIFTY_OPT", timeframe="5m"), cache, spy
    )

    ((_, ctx),) = spy.seen
    assert ctx.symbol == "NIFTY" and ctx.extras["has_volume"] is False
    (ladder,) = cache.ladders.values()
    assert ladder.has_volume is False and np.isnan(ladder.inputs("30m").rvol).all()


# ── a backtest, end to end ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "instrument, side, signal, entry",
    [
        ("SBIN", "LONG", "2026-10-15 10:35", "2026-10-15 10:40"),
        ("RELIANCE", "SHORT", "2026-10-15 11:25", "2026-10-15 11:30"),
    ],
)
def test_a_backtest_finds_the_one_entry_of_its_world(mtf_engine, instrument, side, signal, entry):
    res = backtest(DataCache(mtf_engine), instrument)

    (trade,) = res.trades.to_dict("records")
    assert trade["strategy"] == "supertrend_mtf" and trade["side"] == side
    assert trade["signal_ts"] == at(signal)
    assert trade["entry_ts"] == at(entry), "filled at the next bar's open"
    assert trade["exit_ts"] == at("2026-10-15 15:15") and trade["exit_reason"] == "square_off"
    assert trade["qty"] == 1, "one share"
    counts = {k: res.metrics[k] for k in ("signals_long", "signals_short", "candidates")}
    assert counts == {
        "signals_long": int(side == "LONG"),
        "signals_short": int(side == "SHORT"),
        "candidates": 1,
    }


def test_the_backtest_is_the_strategy_and_the_simulator_on_the_caches_bars(mtf_engine):
    """By hand, with the context the runner builds: the same trade, to the paisa."""
    cache = DataCache(mtf_engine)
    strategy = get_strategy("supertrend_mtf", {"timeframe": "5m"})
    ctx = Context(
        symbol="SBIN",
        daily=cache.bars("SBIN", "1d"),
        extras={"minute_bars": cache.bars("SBIN", "1m"), "has_volume": True},
    )
    frame = strategy.generate(cache.bars("SBIN", "5m"), ctx)
    fine, start = cache.bars("SBIN", "1m"), at("2026-10-06")  # after three warm-up sessions
    (by_hand,) = simulate(frame, strategy.p, "SBIN", "supertrend_mtf", 0.03, fine, start)

    (trade,) = backtest(cache).trades.to_dict("records")

    assert trade["signal_ts"] == by_hand.signal_ts and trade["entry_ts"] == by_hand.entry_ts
    assert trade["entry_price"] == pytest.approx(by_hand.entry_price, abs=1e-4)
    assert trade["stop"] == pytest.approx(by_hand.stop_initial)
    assert trade["exit_ts"] == by_hand.exit_ts and trade["exit_reason"] == by_hand.exit_reason
    assert trade["exit_price"] == pytest.approx(by_hand.avg_exit, abs=1e-4)
    assert by_hand.entry_price > by_hand.stop_initial > 0


def test_without_the_minute_bars_the_runner_gives_the_same_strategy_finds_nothing(mtf_engine):
    """The owner's rule wants every timeframe to agree, the one-minute one included: five-
    minute bars alone cannot say what it reads, and a timeframe that is not there never
    agrees."""
    cache = DataCache(mtf_engine)
    strategy = get_strategy("supertrend_mtf", {"timeframe": "5m"})
    alone = Context(symbol="SBIN", daily=cache.bars("SBIN", "1d"), extras={"has_volume": True})

    frame = strategy.generate(cache.bars("SBIN", "5m"), alone)

    assert not frame["long_entry"].any() and not frame["short_entry"].any()
    assert len(backtest(cache).trades) == 1


def test_a_late_start_does_not_cut_the_warm_up_of_the_slow_timeframes(mtf_engine):
    """The window of a run that starts on the 15th holds three sessions before it: 39 thirty-
    minute bars, where that timeframe's Supertrend wants 70. The ladder is built on the whole
    one-minute history, so the entry of the 15th is still found."""
    cache = DataCache(mtf_engine)

    on_the_day = backtest(cache, start="2026-10-15")
    after_it = backtest(cache, start="2026-10-16")

    (trade,) = on_the_day.trades.to_dict("records")
    assert trade["signal_ts"] == at("2026-10-15 10:35")
    assert after_it.trades.empty and after_it.metrics["signals_long"] == 0


def test_an_index_option_backtest_buys_the_call_at_the_underlyings_open(mtf_engine):
    res = backtest(DataCache(mtf_engine), "NIFTY_OPT")

    (trade,) = res.trades.to_dict("records")
    detail = trade["detail"]
    assert trade["side"] == "LONG" and detail["opt_type"] == "CE"
    assert trade["signal_ts"] == at("2026-10-15 10:35")
    minute, _ = world(*WORLDS["NIFTY"])
    # an option's friction is taken on its premium: the underlying fills at the bare open
    assert detail["underlying_entry"] == float(minute.loc[at("2026-10-15 10:40"), "open"])
    assert res.metrics["pricing"] == "black76_synthetic"
    assert trade["qty"] == res.metrics["unit"] > 1, "one lot"


# ── the ladder is built once ─────────────────────────────────────────────────────────


@pytest.fixture
def frames_built(monkeypatch) -> list[str]:
    """Every time the bars of the ladder's timeframes are resampled, by base resolution."""
    built: list[str] = []
    real = ms.rung_frames

    def counting(base_bars, base, daily):
        built.append(base)
        return real(base_bars, base, daily)

    monkeypatch.setattr(ms, "rung_frames", counting)
    return built


def test_the_ladder_of_an_instrument_is_built_once_for_a_setting(mtf_engine, frames_built):
    cache = DataCache(mtf_engine)

    backtest(cache)
    (first,) = cache.ladders.values()
    backtest(cache)  # the same run again
    backtest(cache, params={"flip_window": 2, "trend_adx_min": 20})  # filters, not the ladder
    backtest(cache, start="2026-10-12")  # another window of the same history

    assert list(cache.ladders.values()) == [first]
    assert frames_built == ["1m"]
    assert (first.period, first.mult, first.base) == (14, 1.7, "1m")


def test_another_supertrend_setting_reuses_the_resampled_bars(mtf_engine, frames_built):
    cache = DataCache(mtf_engine)

    backtest(cache)
    backtest(cache, params={"st_mult": 2.0})
    backtest(cache, params={"st_period": 10})

    assert [(lad.period, lad.mult) for lad in cache.ladders.values()] == [
        (14, 1.7),
        (14, 2.0),
        (10, 1.7),
    ]
    assert frames_built == ["1m"], "one resampling of the minute bars serves every setting"


def test_another_instrument_gets_its_own_ladder(mtf_engine, frames_built):
    cache = DataCache(mtf_engine)

    backtest(cache, "SBIN")
    backtest(cache, "RELIANCE")
    backtest(cache, "SBIN")  # back to the first: its ladder is still there

    assert len(cache.ladders) == 2
    assert frames_built == ["1m", "1m"]
