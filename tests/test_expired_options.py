"""Dhan expired index options in the local research store (`onelot.data.expired_options`).

The module's contract (its docstring, checked against the live API on 2026-11-07): rolling
series keyed by underlying / expiry code / CE|PE / strike relative to the minute's ATM; bars
stamped at their START; impossible strikes and post-close prints dropped, never repaired; a
block is one trading day of one series, an empty answer is stored too, a day still running is
fetched again.

No network: the source is a fake with Dhan's `rolling_option` signature.
"""

from __future__ import annotations

import datetime as dt
import math
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from onelot.clock import IST
from onelot.data.expired_options import (
    FIELDS,
    MAX_REL,
    Block,
    OptionBarStore,
    RollingKey,
    Underlying,
    ensure_blocks,
    fetch_days,
    ist_epoch,
    minute_index,
    parse_leg,
    sanitize,
)

D1 = dt.date(2026, 10, 1)  # a Thursday
D2 = D1 + dt.timedelta(days=1)
D3 = D1 + dt.timedelta(days=2)
LONG_AGO_TODAY = dt.date(2026, 10, 20)  # every test day has ended by then
UND = Underlying("NIFTY", "13", "NSE_FNO", 50.0)
KEY = RollingKey("NIFTY", 1, "CE", 0)


# ── builders ──────────────────────────────────────────────────────────────────────────────
def epoch(day: dt.date, hhmm: str) -> int:
    """An IST wall-clock time as the epoch second Dhan stamps a bar with."""
    h, m = (int(x) for x in hhmm.split(":"))
    return int(dt.datetime(day.year, day.month, day.day, h, m, tzinfo=IST).timestamp())


def hhmm(ts: int) -> str:
    return dt.datetime.fromtimestamp(int(ts), IST).strftime("%H:%M")


def bar(day: dt.date, at: str, o: float = 100.0, c: float | None = None, **over) -> dict:
    """One Dhan bar (a dict of the nine fields and its stamp); a sane NIFTY ATM call by default."""
    c = o if c is None else c
    row = {
        "ts": epoch(day, at),
        "open": o,
        "high": max(o, c) + 1.0,
        "low": min(o, c) - 1.0,
        "close": c,
        "iv": 12.5,
        "volume": 1000.0,
        "oi": 5000.0,
        "strike": 25000.0,
        "spot": 25010.0,
    }
    row.update(over)
    return row


def leg(*bars: dict) -> dict:
    """Dhan's column lists for a leg."""
    return {"timestamp": [b["ts"] for b in bars], **{f: [b[f] for b in bars] for f in FIELDS}}


def arrays(*bars: dict) -> dict[str, np.ndarray]:
    return parse_leg(leg(*bars))


def block(key: RollingKey, day: dt.date, *bars: dict, complete: bool = True) -> Block:
    return Block(key, day, complete, sanitize(arrays(*bars), day, day))


class FakeSource:
    """`rolling_option` with Dhan's signature; answers are keyed by the request's start day."""

    def __init__(self, legs: dict | None = None, fail_days=()) -> None:
        self.legs = legs or {}
        self.fail_days = set(fail_days)
        self.calls: list[tuple] = []

    def rolling_option(
        self,
        underlying_security_id,
        fno_segment,
        expiry_code,
        strike,
        option_type,
        start,
        end,
        expiry_flag="MONTH",
        interval_min=1,
    ) -> dict:
        self.calls.append(
            (underlying_security_id, fno_segment, expiry_code, strike, option_type, start, end,
             expiry_flag)
        )  # fmt: skip
        if start in self.fail_days:
            raise RuntimeError("DH-904 rate limit exceeded")
        return self.legs.get(start, {})

    @property
    def starts(self) -> list[dt.date]:
        return [c[5] for c in self.calls]


@pytest.fixture
def store(tmp_path):
    s = OptionBarStore(tmp_path / "nested" / "expired.db")
    yield s
    s.close()


# ── RollingKey ────────────────────────────────────────────────────────────────────────────
class TestRollingKey:
    @pytest.mark.parametrize("opt", ["CE", "PE"])
    @pytest.mark.parametrize("code", [1, 2, 3])
    @pytest.mark.parametrize("rel", [-MAX_REL, -1, 0, 1, MAX_REL])
    def test_valid_keys(self, opt, code, rel):
        k = RollingKey("NIFTY", code, opt, rel)
        assert (k.underlying, k.code, k.opt, k.rel, k.flag) == ("NIFTY", code, opt, rel, "MONTH")

    @pytest.mark.parametrize("opt", ["CALL", "PUT", "ce", "pe", "", "C"])
    def test_an_option_type_other_than_ce_or_pe_is_refused(self, opt):
        with pytest.raises(ValueError, match="CE or PE"):
            RollingKey("NIFTY", 1, opt, 0)

    @pytest.mark.parametrize("code", [0, 4, -1, 10])
    def test_an_expiry_code_outside_one_to_three_is_refused(self, code):
        with pytest.raises(ValueError, match="expiry code"):
            RollingKey("NIFTY", code, "CE", 0)

    @pytest.mark.parametrize("rel", [MAX_REL + 1, -MAX_REL - 1, 11, -11, 100])
    def test_a_strike_beyond_ten_steps_from_the_money_is_refused(self, rel):
        with pytest.raises(ValueError, match="ATM"):
            RollingKey("NIFTY", 1, "CE", rel)

    @pytest.mark.parametrize(
        ("rel", "arg"),
        [(0, "ATM"), (1, "ATM+1"), (3, "ATM+3"), (10, "ATM+10"), (-1, "ATM-1"), (-10, "ATM-10")],
    )
    def test_the_strike_argument_is_what_dhan_spells(self, rel, arg):
        assert RollingKey("NIFTY", 1, "CE", rel).strike_arg == arg

    def test_keys_are_hashable_value_objects(self):
        a, b = RollingKey("NIFTY", 1, "CE", 2), RollingKey("NIFTY", 1, "CE", 2)
        assert a == b and hash(a) == hash(b) and len({a, b}) == 1
        assert RollingKey("NIFTY", 2, "CE", 2) != a and RollingKey("NIFTY", 1, "PE", 2) != a
        assert RollingKey("NIFTY", 1, "CE", 2, flag="WEEK") != a
        with pytest.raises(AttributeError):
            a.rel = 3  # type: ignore[misc]


# ── Underlying ────────────────────────────────────────────────────────────────────────────
class TestUnderlying:
    @staticmethod
    def specs(key="NIFTY", sid="13", exchange="NSE", step=50.0):
        return (
            SimpleNamespace(key=key, dhan_security_id=sid, exchange=exchange),
            SimpleNamespace(key=f"{key}_OPT", strike_step=step),
        )

    def test_built_from_the_universe_specs(self):
        series, inst = self.specs()
        assert Underlying.from_specs(series, inst) == Underlying("NIFTY", "13", "NSE_FNO", 50.0)

    def test_a_bse_index_trades_on_the_bse_fno_segment(self):
        series, inst = self.specs("SENSEX", "51", "BSE", 100.0)
        assert Underlying.from_specs(series, inst).fno_segment == "BSE_FNO"

    def test_the_security_id_is_a_string_and_the_step_a_float(self):
        series, inst = self.specs(sid=13, step=50)
        und = Underlying.from_specs(series, inst)
        assert und.security_id == "13" and und.strike_step == 50.0

    @pytest.mark.parametrize("sid", [None, ""])
    def test_an_index_without_a_dhan_security_id_is_refused_by_name(self, sid):
        series, inst = self.specs(sid=sid)
        with pytest.raises(ValueError, match="NIFTY.*security id"):
            Underlying.from_specs(series, inst)

    @pytest.mark.parametrize("step", [0, 0.0, None, -50.0])
    def test_an_option_without_a_strike_step_is_refused_by_name(self, step):
        series, inst = self.specs(step=step)
        with pytest.raises(ValueError, match="NIFTY_OPT.*strike step"):
            Underlying.from_specs(series, inst)

    @pytest.mark.parametrize("key", ["BANKNIFTY", "NIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"])
    def test_every_index_the_desk_watches_has_what_the_fetch_needs(self, key):
        from onelot.data.universe import load_specs

        series, instruments = load_specs()
        und = Underlying.from_specs(series[key], instruments[f"{key}_OPT"])
        assert und.key == key and und.security_id.isdigit()
        assert und.strike_step > 0
        assert und.fno_segment == ("BSE_FNO" if key == "SENSEX" else "NSE_FNO")
        assert und.security_id == str(series[key].dhan_security_id)


# ── parse_leg ─────────────────────────────────────────────────────────────────────────────
class TestParseLeg:
    def test_columns_become_float_arrays_and_the_stamps_int64(self):
        out = parse_leg(leg(bar(D1, "09:15", 100), bar(D1, "09:16", 101)))
        assert out["ts"].dtype == np.int64 and out["ts"].tolist() == [
            epoch(D1, "09:15"),
            epoch(D1, "09:16"),
        ]
        assert set(out) == {"ts", *FIELDS}
        for name in FIELDS:
            assert out[name].dtype == np.float64 and out[name].shape == (2,)
        assert out["open"].tolist() == [100.0, 101.0]
        assert out["strike"].tolist() == [25000.0, 25000.0]

    def test_integers_are_read_as_floats(self):
        out = parse_leg({"timestamp": [1, 2], "open": [5, 6], "volume": [100, 200]})
        assert out["open"].tolist() == [5.0, 6.0] and out["volume"].dtype == np.float64

    def test_a_list_shorter_than_the_stamps_is_padded_with_nan(self):
        out = parse_leg({"timestamp": [1, 2, 3], "open": [1.0, 2.0]})
        assert out["open"][:2].tolist() == [1.0, 2.0] and math.isnan(out["open"][2])
        assert out["open"].shape == (3,)

    def test_a_list_longer_than_the_stamps_is_cut(self):
        out = parse_leg({"timestamp": [1, 2], "open": [1.0, 2.0, 3.0, 4.0]})
        assert out["open"].tolist() == [1.0, 2.0]

    def test_a_missing_field_is_all_nan(self):
        out = parse_leg({"timestamp": [1, 2], "open": [1.0, 2.0]})
        for name in FIELDS:
            if name != "open":
                assert out[name].shape == (2,) and np.isnan(out[name]).all()

    @pytest.mark.parametrize("raw", [None, "12.5", 12.5, {"a": 1}, 7])
    def test_a_field_that_is_not_a_list_is_all_nan(self, raw):
        out = parse_leg({"timestamp": [1, 2], "iv": raw})
        assert np.isnan(out["iv"]).all() and out["iv"].shape == (2,)

    def test_unreadable_values_inside_a_list_are_nan_one_by_one(self):
        out = parse_leg({"timestamp": [1, 2, 3, 4], "close": [10.0, None, "x", 12.0]})
        assert out["close"][0] == 10.0 and out["close"][3] == 12.0
        assert math.isnan(out["close"][1]) and math.isnan(out["close"][2])

    def test_a_nan_sent_by_dhan_stays_nan(self):
        out = parse_leg({"timestamp": [1, 2], "iv": [float("nan"), 11.0]})
        assert math.isnan(out["iv"][0]) and out["iv"][1] == 11.0

    @pytest.mark.parametrize("raw", [{}, {"timestamp": None}, {"timestamp": []}])
    def test_an_empty_or_missing_leg_is_empty_arrays_with_every_field(self, raw):
        out = parse_leg(raw)
        assert set(out) == {"ts", *FIELDS}
        assert all(v.size == 0 for v in out.values()) and out["ts"].dtype == np.int64

    def test_every_column_is_as_long_as_the_stamps(self):
        out = parse_leg({"timestamp": list(range(7)), "open": [1.0] * 3, "high": [1.0] * 12})
        assert {v.shape for v in out.values()} == {(7,)}


# ── sanitize ──────────────────────────────────────────────────────────────────────────────
class TestSanitize:
    def kept(self, *bars, first=D1, last=D1) -> list[str]:
        return [hhmm(t) for t in sanitize(arrays(*bars), first, last)["ts"]]

    def test_the_session_is_0915_inclusive_to_1530_exclusive(self):
        times = ["03:00", "09:14", "09:15", "12:00", "15:29", "15:30", "15:31", "23:59"]
        assert self.kept(*(bar(D1, t) for t in times)) == ["09:15", "12:00", "15:29"]

    @pytest.mark.parametrize(
        "bad",
        [
            {"open": 0.0, "low": 0.0},
            {"open": -5.0, "low": -6.0},
            {"low": 0.0},
            {"close": float("nan")},
            {"open": float("nan")},
            {"high": float("inf")},
            {"low": float("nan")},
            {"low": 100.5},  # above the open: not a real range
            {"high": 99.5},  # below the open
            {"strike": float("nan")},
            {"spot": float("nan")},
            {"strike": 0.0},
            {"spot": 0.0},
            {"strike": -25000.0},
        ],
        ids=lambda d: ",".join(f"{k}={v}" for k, v in d.items()),
    )
    def test_an_impossible_bar_is_dropped_and_its_neighbours_stay(self, bad):
        good = bar(D1, "10:00", 100.0, 100.0)
        broken = bar(D1, "10:01", 100.0, 100.0, **bad)
        assert self.kept(good, broken, bar(D1, "10:02")) == ["10:00", "10:02"]

    def test_a_doji_whose_low_and_high_touch_the_body_is_kept(self):
        flat = bar(D1, "10:00", 100.0, 100.0, high=100.0, low=100.0)
        body = bar(D1, "10:01", 100.0, 102.0, high=102.0, low=100.0)
        assert self.kept(flat, body) == ["10:00", "10:01"]

    @pytest.mark.parametrize(
        ("strike", "spot", "keep"),
        [
            (44000.0, 44000.0, True),
            (57000.0, 44000.0, True),  # 29.5 % above
            (31000.0, 44000.0, True),  # 29.5 % below
            (58000.0, 44000.0, False),  # 31.8 % above
            (30000.0, 44000.0, False),  # 31.8 % below
            (7550.0, 44000.0, False),  # the impossible print Dhan really sent (BANKNIFTY)
        ],
    )
    def test_a_strike_more_than_thirty_percent_from_the_spot_is_a_bad_print(
        self, strike, spot, keep
    ):
        out = self.kept(bar(D1, "10:00", strike=strike, spot=spot))
        assert out == (["10:00"] if keep else [])

    def test_one_bar_per_stamp_and_it_is_the_last_one_dhan_sent(self):
        out = sanitize(
            arrays(
                bar(D1, "10:00", 100.0, 100.0),
                bar(D1, "10:00", 101.0, 101.0),
                bar(D1, "10:00", 102.0, 102.0),
                bar(D1, "10:01", 50.0, 50.0),
            ),
            D1,
            D1,
        )
        assert [hhmm(t) for t in out["ts"]] == ["10:00", "10:01"]
        assert out["close"].tolist() == [102.0, 50.0]

    def test_an_earlier_valid_duplicate_survives_an_invalid_later_one(self):
        good = bar(D1, "10:00", 100.0, 100.0)
        broken = bar(D1, "10:00", 100.0, 100.0, low=0.0)  # a printed zero low
        out = sanitize(arrays(good, broken), D1, D1)
        assert out["open"].tolist() == [100.0] and out["low"].tolist() == [99.0]

    def test_the_output_is_sorted_whatever_the_order_dhan_sent(self):
        times = ["10:03", "10:01", "10:05", "10:02", "10:04"]
        out = sanitize(arrays(*(bar(D1, t, 100.0 + int(t[-1])) for t in times)), D1, D1)
        assert [hhmm(t) for t in out["ts"]] == sorted(times)
        # every column moved with its stamp
        assert out["open"].tolist() == [101.0, 102.0, 103.0, 104.0, 105.0]
        assert out["close"].tolist() == out["open"].tolist()

    def test_only_the_requested_ist_days_survive(self):
        bars = [bar(d, "10:00") for d in (D1, D2, D3)]
        assert len(sanitize(arrays(*bars), D1, D3)["ts"]) == 3
        assert len(sanitize(arrays(*bars), D1, D2)["ts"]) == 2
        only_d2 = sanitize(arrays(*bars), D2, D2)
        assert only_d2["ts"].tolist() == [epoch(D2, "10:00")]

    def test_a_window_whose_first_day_is_after_its_last_is_empty(self):
        assert sanitize(arrays(bar(D2, "10:00")), D3, D1)["ts"].size == 0

    def test_a_bar_on_the_edge_of_the_ist_day_is_clipped_by_its_ist_date(self):
        # 09:15 IST on D2 is the previous UTC day's evening for nobody, but 00:30 IST on D2 is
        # D1 in UTC: it must be dropped by the session window, never counted as D1.
        late_utc_d1 = int(dt.datetime(2026, 10, 1, 19, 0, tzinfo=dt.timezone.utc).timestamp())
        row = bar(D2, "10:00", ts=late_utc_d1)
        assert hhmm(late_utc_d1) == "00:30"
        assert sanitize(arrays(row), D1, D1)["ts"].size == 0
        assert sanitize(arrays(row), D2, D2)["ts"].size == 0  # 00:30 is outside the session

    @pytest.mark.parametrize("bars", [[], [bar(D1, "03:00")], [bar(D1, "10:00", low=0.0)]])
    def test_nothing_usable_gives_empty_arrays_with_every_field(self, bars):
        out = sanitize(arrays(*bars), D1, D1)
        assert set(out) == {"ts", *FIELDS}
        assert all(v.size == 0 for v in out.values()) and out["ts"].dtype == np.int64

    def test_the_input_arrays_are_left_untouched(self):
        arr = arrays(bar(D1, "10:02"), bar(D1, "10:01"), bar(D1, "15:40"), bar(D1, "10:01"))
        before = {k: v.copy() for k, v in arr.items()}
        sanitize(arr, D1, D1)
        for k in arr:
            np.testing.assert_array_equal(arr[k], before[k])

    def test_the_result_keeps_every_column_equally_long(self):
        out = sanitize(arrays(bar(D1, "10:00"), bar(D1, "10:00"), bar(D1, "10:01")), D1, D1)
        assert {v.shape for v in out.values()} == {(2,)}

    def test_iv_volume_and_oi_are_never_a_reason_to_drop_a_bar(self):
        nan = float("nan")
        out = sanitize(arrays(bar(D1, "10:00", iv=nan, volume=nan, oi=nan)), D1, D1)
        assert out["ts"].size == 1 and math.isnan(out["iv"][0])


class TestSanitizeAtScale:
    def test_a_year_of_minute_stamps_is_cleaned_in_one_pass(self):
        n = 400_000  # about 277 calendar days of minutes, 24 hours a day
        start = epoch(dt.date(2025, 1, 6), "00:00")
        ts = start + 60 * np.arange(n, dtype="int64")
        arr = {"ts": ts}
        price = 100.0 + (np.arange(n) % 7)
        arr |= {"open": price, "high": price + 1, "low": price - 1, "close": price}
        arr |= {f: np.full(n, 25000.0 if f in ("strike", "spot") else 1.0) for f in FIELDS[4:]}
        out = sanitize(arr, dt.date(2025, 1, 6), dt.date(2025, 12, 31))
        minute_of_day = ((ts + 19800) % 86400) // 60
        want = int(((minute_of_day >= 555) & (minute_of_day < 930)).sum())
        assert out["ts"].size == want and want > 100_000
        assert np.all(np.diff(out["ts"]) > 0)
        assert {v.shape for v in out.values()} == {(want,)}


def oracle(rows: list[dict], first: dt.date, last: dt.date) -> list[dict]:
    """An independent restatement of `sanitize`'s docstring, in plain Python."""
    kept: dict[int, dict] = {}
    for r in rows:
        when = dt.datetime.fromtimestamp(r["ts"], IST)
        minute = when.hour * 60 + when.minute
        if not (first <= when.date() <= last and 9 * 60 + 15 <= minute < 15 * 60 + 30):
            continue
        px = [r["open"], r["high"], r["low"], r["close"]]
        if not all(math.isfinite(v) and v > 0 for v in px):
            continue
        if r["low"] > min(r["open"], r["close"]) or r["high"] < max(r["open"], r["close"]):
            continue
        if not (math.isfinite(r["strike"]) and math.isfinite(r["spot"])):
            continue
        if r["strike"] <= 0 or r["spot"] <= 0 or abs(r["strike"] - r["spot"]) > 0.3 * r["spot"]:
            continue
        kept[r["ts"]] = r  # the last valid one wins
    return [kept[t] for t in sorted(kept)]


GOOD = st.sampled_from([99.95, 100.0, 100.05, 101.0, 102.5])
PAD = st.sampled_from([0.0, 0.05, 1.0])
BAD = st.sampled_from([0.0, -1.0, 0.01, 500.0, float("nan"), float("inf")])
SPOTS = st.sampled_from([100.0, 25000.0])
GAPS = st.sampled_from([1.0, 1.0, 1.0, 0.71, 1.29, 0.69, 1.31, 0.5, 2.0])  # never exactly 30 %
# minutes of the IST day: mostly in the 09:15-15:30 session, with the edges and some outside
MINUTES = st.sampled_from(
    [555, 556, 600, 720, 929, 555, 556, 600, 720, 929, 180, 540, 554, 930, 931, 1000]
)
DAYS = st.integers(0, 3)


def maybe_nan(top: float):
    return st.one_of(st.floats(0, top), st.just(float("nan")))


@st.composite
def messy_bars(draw):
    """A bar that is usually sane and now and then has one broken field or a bad stamp."""
    o, c = draw(GOOD), draw(GOOD)
    row = {"open": o, "close": c, "low": min(o, c) - draw(PAD), "high": max(o, c) + draw(PAD)}
    if draw(st.booleans()) and draw(st.booleans()):
        row[draw(st.sampled_from(["open", "high", "low", "close"]))] = draw(BAD)
    spot = draw(SPOTS)
    when = dt.datetime(2026, 10, 1, tzinfo=IST) + dt.timedelta(
        days=draw(DAYS), minutes=draw(MINUTES)
    )
    return {
        **row,
        "ts": int(when.timestamp()),
        "iv": draw(maybe_nan(80)),
        "volume": draw(maybe_nan(1e6)),
        "oi": draw(maybe_nan(1e7)),
        "strike": spot * draw(GAPS),
        "spot": spot,
    }


class TestSanitizeProperties:
    @settings(derandomize=True, max_examples=250, deadline=None)
    @given(
        rows=st.lists(messy_bars(), max_size=30),
        first=st.integers(0, 1),
        span=st.integers(1, 3),
    )
    def test_matches_an_independent_oracle_and_is_idempotent(self, rows, first, span):
        a, b = D1 + dt.timedelta(days=first), D1 + dt.timedelta(days=first + span)
        arr = {"ts": np.array([r["ts"] for r in rows], dtype="int64")}
        arr |= {f: np.array([r[f] for r in rows], dtype=float) for f in FIELDS}
        out = sanitize(arr, a, b)
        want = oracle(rows, a, b)
        assert out["ts"].tolist() == [r["ts"] for r in want]
        for f in FIELDS:
            np.testing.assert_array_equal(out[f], np.array([r[f] for r in want], dtype=float))
        assert np.all(np.diff(out["ts"]) > 0)  # strictly ascending: one bar per stamp
        again = sanitize(out, a, b)
        for k in out:
            np.testing.assert_array_equal(again[k], out[k])


# ── the store ─────────────────────────────────────────────────────────────────────────────
class TestOptionBarStore:
    def test_a_block_round_trips_through_the_file(self, tmp_path):
        path = tmp_path / "rt.db"
        first = OptionBarStore(path)
        b = block(
            KEY,
            D1,
            bar(D1, "09:15", 312.35, 313.4, iv=13.1234, strike=55000.0, spot=55012.35),
            bar(D1, "09:16", 313.4, 311.05, strike=55000.0, spot=55001.25),
        )
        first.put(b)
        first.close()
        second = OptionBarStore(path)  # a fresh process: nothing is in memory
        try:
            got = second.get(KEY, D1)
            assert got is not None and got.complete is True and got.rows == 2
            assert got.key == KEY and got.day == D1
            assert got.data["ts"].tolist() == b.data["ts"].tolist()
            assert got.data["open"].tolist() == [312.35, 313.4]  # to the paisa
            assert got.data["close"].tolist() == [313.4, 311.05]
            assert got.data["strike"].tolist() == [55000.0, 55000.0]
            assert got.data["spot"].tolist() == [55012.35, 55001.25]
            assert got.data["iv"][0] == pytest.approx(13.1234, rel=1e-6)
            assert got.data["ts"].dtype == np.int64 and got.data["open"].dtype == np.float64
        finally:
            second.close()

    def test_nothing_is_stored_until_put(self, store):
        assert store.get(KEY, D1) is None and store.has(KEY, D1) is False
        assert store.summary() == []

    def test_has_sees_a_block_in_the_file_not_only_in_memory(self, tmp_path):
        path = tmp_path / "has.db"
        s = OptionBarStore(path)
        s.put(block(KEY, D1, bar(D1, "10:00")))
        s.close()
        again = OptionBarStore(path)
        try:
            assert again.has(KEY, D1) is True
            assert again.has(KEY, D2) is False
        finally:
            again.close()

    def test_an_empty_day_is_stored_so_it_is_not_asked_again(self, tmp_path):
        path = tmp_path / "empty.db"
        s = OptionBarStore(path)
        s.put(Block(KEY, D1, True, parse_leg({})))
        s.close()
        again = OptionBarStore(path)
        try:
            got = again.get(KEY, D1)
            assert got is not None and got.rows == 0 and got.complete is True
            assert set(got.data) == {"ts", *FIELDS} and got.data["ts"].dtype == np.int64
            assert again.has(KEY, D1) is True
        finally:
            again.close()

    def test_the_complete_flag_is_stored_not_assumed(self, tmp_path):
        path = tmp_path / "flag.db"
        s = OptionBarStore(path)
        s.put(block(KEY, D1, bar(D1, "10:00"), complete=False))
        s.put(block(KEY, D2, bar(D2, "10:00"), complete=True))
        s.close()
        again = OptionBarStore(path)
        try:
            assert again.get(KEY, D1).complete is False  # type: ignore[union-attr]
            assert again.get(KEY, D2).complete is True  # type: ignore[union-attr]
        finally:
            again.close()

    def test_putting_a_block_again_replaces_it(self, tmp_path):
        path = tmp_path / "replace.db"
        s = OptionBarStore(path)
        s.put(block(KEY, D1, bar(D1, "10:00"), complete=False))
        s.put(block(KEY, D1, bar(D1, "10:00"), bar(D1, "10:01"), bar(D1, "10:02")))
        s.close()
        again = OptionBarStore(path)
        try:
            got = again.get(KEY, D1)
            assert got is not None and got.complete is True and got.rows == 3
            assert again.summary() == [("NIFTY", 1, "CE", 1, 0, 3)]
        finally:
            again.close()

    def test_every_part_of_the_key_separates_blocks(self, store):
        keys = [
            RollingKey("NIFTY", 1, "CE", 0),
            RollingKey("NIFTY", 1, "CE", 1),
            RollingKey("NIFTY", 1, "PE", 0),
            RollingKey("NIFTY", 2, "CE", 0),
            RollingKey("BANKNIFTY", 1, "CE", 0),
            RollingKey("NIFTY", 1, "CE", 0, flag="WEEK"),
        ]
        for i, k in enumerate(keys):
            store.put(block(k, D1, bar(D1, "10:00", 100.0 + i, strike=25000.0)))
        store.put(block(KEY, D2, bar(D2, "10:00", 999.0)))
        for i, k in enumerate(keys):
            got = store.get(k, D1)
            assert got is not None and got.data["open"].tolist() == [100.0 + i]
        got2 = store.get(KEY, D2)
        assert got2 is not None and got2.data["open"].tolist() == [999.0]
        assert store.get(RollingKey("NIFTY", 3, "CE", 0), D1) is None

    def test_the_summary_counts_blocks_empties_and_bars_per_series_family(self, store):
        store.put(block(RollingKey("NIFTY", 1, "CE", 0), D1, bar(D1, "10:00"), bar(D1, "10:01")))
        store.put(block(RollingKey("NIFTY", 1, "CE", 3), D1, bar(D1, "10:00")))
        store.put(Block(RollingKey("NIFTY", 1, "CE", 3), D2, True, parse_leg({})))
        store.put(block(RollingKey("NIFTY", 1, "PE", 0), D1, bar(D1, "10:00")))
        store.put(block(RollingKey("BANKNIFTY", 2, "CE", 0), D1, bar(D1, "10:00")))
        assert store.summary() == [
            ("BANKNIFTY", 2, "CE", 1, 0, 1),
            ("NIFTY", 1, "CE", 3, 1, 3),
            ("NIFTY", 1, "PE", 1, 0, 1),
        ]

    def test_the_parent_folder_is_created(self, tmp_path):
        path = tmp_path / "a" / "b" / "c.db"
        s = OptionBarStore(path)
        try:
            assert path.exists()
        finally:
            s.close()

    def test_blocks_written_from_several_threads_all_land(self, store):
        keys = [RollingKey("NIFTY", 1, "CE", rel) for rel in range(-5, 6)]
        errors: list[BaseException] = []

        def work(k: RollingKey) -> None:
            try:
                for d in range(6):
                    day = D1 + dt.timedelta(days=d)
                    store.put(block(k, day, bar(day, "10:00", 100.0 + d)))
                    assert store.get(k, day) is not None
            except BaseException as exc:  # noqa: BLE001 - the test reports it
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(k,)) for k in keys]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not errors
        assert sum(row[3] for row in store.summary()) == len(keys) * 6


# ── fetch_days ────────────────────────────────────────────────────────────────────────────
class TestFetchDays:
    def test_one_request_asks_for_the_day_and_the_next_and_nothing_else(self):
        src = FakeSource()
        fetch_days(src, UND, RollingKey("NIFTY", 2, "PE", 3), D1, today=LONG_AGO_TODAY)
        assert src.calls == [("13", "NSE_FNO", 2, "ATM+3", "PUT", D1, D2, "MONTH")]

    def test_a_call_series_asks_for_call_and_a_negative_rel_for_atm_minus(self):
        src = FakeSource()
        fetch_days(src, UND, RollingKey("NIFTY", 1, "CE", -4), D1, today=LONG_AGO_TODAY)
        assert src.calls[0][3:5] == ("ATM-4", "CALL")

    def test_the_answer_is_split_into_the_two_days_it_holds(self):
        src = FakeSource(
            {
                D1: leg(
                    bar(D1, "09:15", 100),
                    bar(D1, "09:16", 101),
                    bar(D2, "09:15", 200),
                )  # fmt: skip
            }
        )
        b1, b2 = fetch_days(src, UND, KEY, D1, today=LONG_AGO_TODAY)
        assert (b1.day, b2.day) == (D1, D2) and (b1.rows, b2.rows) == (2, 1)
        assert b1.data["open"].tolist() == [100.0, 101.0] and b2.data["open"].tolist() == [200.0]
        assert b1.key == KEY and b2.key == KEY

    def test_bars_of_any_other_day_are_dropped(self):
        src = FakeSource({D1: leg(bar(D1 - dt.timedelta(days=1), "10:00"), bar(D1, "10:00"),
                                  bar(D3, "10:00"))})  # fmt: skip
        b1, b2 = fetch_days(src, UND, KEY, D1, today=LONG_AGO_TODAY)
        assert (b1.rows, b2.rows) == (1, 0)

    def test_dhans_bad_prints_are_dropped_on_the_way_in(self):
        src = FakeSource(
            {
                D1: leg(
                    bar(D1, "10:00", strike=7550.0, spot=25000.0),  # impossible strike
                    bar(D1, "15:30"),  # a closing print
                    bar(D1, "10:01"),
                )
            }
        )
        b1, _ = fetch_days(src, UND, KEY, D1, today=LONG_AGO_TODAY)
        assert [hhmm(t) for t in b1.data["ts"]] == ["10:01"]

    def test_a_day_without_bars_is_an_empty_complete_block(self):
        b1, b2 = fetch_days(FakeSource(), UND, KEY, D1, today=LONG_AGO_TODAY)
        assert (b1.rows, b2.rows) == (0, 0) and b1.complete and b2.complete

    def test_a_none_answer_is_treated_as_no_bars(self):
        class NoneSource(FakeSource):
            def rolling_option(self, *a, **k):
                return None

        b1, _ = fetch_days(NoneSource(), UND, KEY, D1, today=LONG_AGO_TODAY)
        assert b1.rows == 0

    def test_a_day_is_complete_only_once_it_has_ended(self):
        b1, b2 = fetch_days(FakeSource(), UND, KEY, D1, today=D2)
        assert (b1.day, b1.complete) == (D1, True)
        assert (b2.day, b2.complete) == (D2, False)  # today is still running

    def test_the_day_that_is_today_is_never_complete(self):
        blocks = fetch_days(FakeSource(), UND, KEY, D2, today=D2)
        assert [(b.day, b.complete) for b in blocks] == [(D2, False)]

    def test_a_future_day_is_not_returned(self):
        blocks = fetch_days(FakeSource(), UND, KEY, D1, today=D1)
        assert [b.day for b in blocks] == [D1]  # D1 + 1 has not happened yet

    def test_a_request_for_a_day_after_today_returns_no_block(self):
        src = FakeSource()
        assert fetch_days(src, UND, KEY, D3, today=D1) == []
        assert src.calls == []  # nothing has traded yet: Dhan is not even asked

    def test_a_key_for_another_underlying_is_refused_before_any_request(self):
        src = FakeSource()
        with pytest.raises(ValueError, match="does not match"):
            fetch_days(src, UND, RollingKey("BANKNIFTY", 1, "CE", 0), D1, today=D3)
        assert src.calls == []

    def test_a_failing_source_propagates_for_the_caller_to_count(self):
        with pytest.raises(RuntimeError, match="DH-904"):
            fetch_days(FakeSource(fail_days={D1}), UND, KEY, D1, today=D3)


# ── ensure_blocks ─────────────────────────────────────────────────────────────────────────
UNDS = {"NIFTY": UND}


class TestEnsureBlocks:
    def counts(self, **over) -> dict[str, int]:
        return {"requests": 0, "empty": 0, "cached": 0, "failed": 0} | over

    def test_a_missing_block_is_fetched_and_stored(self, store):
        src = FakeSource({D1: leg(bar(D1, "10:00", 111.0))})
        out = ensure_blocks(src, store, UNDS, [(KEY, D1)], today=LONG_AGO_TODAY)
        assert out == self.counts(requests=1, empty=1)  # D1 has a bar; D1 + 1 came back empty
        got = store.get(KEY, D1)
        assert got is not None and got.data["open"].tolist() == [111.0] and got.complete

    def test_a_second_run_asks_for_nothing(self, store):
        src = FakeSource({D1: leg(bar(D1, "10:00"))})
        ensure_blocks(src, store, UNDS, [(KEY, D1)], today=LONG_AGO_TODAY)
        again = ensure_blocks(src, store, UNDS, [(KEY, D1)], today=LONG_AGO_TODAY)
        assert again == self.counts(cached=1)
        assert len(src.calls) == 1

    def test_one_request_covers_the_next_day_too(self, store):
        src = FakeSource({D1: leg(bar(D1, "10:00"), bar(D2, "10:00"))})
        out = ensure_blocks(src, store, UNDS, [(KEY, D1), (KEY, D2)], today=LONG_AGO_TODAY)
        assert out == self.counts(requests=1, cached=1)
        assert src.starts == [D1]
        assert store.get(KEY, D2) is not None

    def test_days_two_apart_need_two_requests(self, store):
        src = FakeSource()
        out = ensure_blocks(src, store, UNDS, [(KEY, D1), (KEY, D3)], today=LONG_AGO_TODAY)
        assert out["requests"] == 2 and out["cached"] == 0
        assert src.starts == [D1, D3]

    def test_a_day_without_trades_is_stored_empty_and_not_asked_again(self, store):
        src = FakeSource()  # every answer is empty: a weekend, a holiday
        first = ensure_blocks(src, store, UNDS, [(KEY, D1)], today=LONG_AGO_TODAY)
        assert first == self.counts(requests=1, empty=2)  # the day and the next
        second = ensure_blocks(src, store, UNDS, [(KEY, D1), (KEY, D2)], today=LONG_AGO_TODAY)
        assert second == self.counts(cached=2) and len(src.calls) == 1

    def test_a_day_still_running_is_fetched_again_every_time(self, store):
        src = FakeSource({D1: leg(bar(D1, "10:00"))})
        for run in (1, 2, 3):
            out = ensure_blocks(src, store, UNDS, [(KEY, D1)], today=D1)
            assert out["requests"] == 1 and out["cached"] == 0
            assert len(src.calls) == run
        assert store.get(KEY, D1).complete is False  # type: ignore[union-attr]

    def test_an_incomplete_block_is_replaced_once_the_day_has_ended(self, store):
        store.put(block(KEY, D1, bar(D1, "10:00", 101.0), complete=False))
        src = FakeSource({D1: leg(bar(D1, "10:00", 101.0), bar(D1, "10:01", 102.0))})
        out = ensure_blocks(src, store, UNDS, [(KEY, D1)], today=D2)
        assert out["requests"] == 1
        got = store.get(KEY, D1)
        assert got is not None and got.complete is True and got.rows == 2

    def test_a_complete_block_is_never_refetched_even_if_empty(self, store):
        store.put(Block(KEY, D1, True, parse_leg({})))
        src = FakeSource({D1: leg(bar(D1, "10:00"))})
        out = ensure_blocks(src, store, UNDS, [(KEY, D1)], today=LONG_AGO_TODAY)
        assert out == self.counts(cached=1) and src.calls == []

    def test_one_failing_day_does_not_stop_the_rest(self, store):
        days = [D1, D1 + dt.timedelta(days=3), D1 + dt.timedelta(days=6)]
        src = FakeSource(fail_days={days[1]})
        out = ensure_blocks(src, store, UNDS, [(KEY, d) for d in days], today=LONG_AGO_TODAY)
        assert out["failed"] == 1 and out["requests"] == 2
        assert store.get(KEY, days[0]) is not None and store.get(KEY, days[2]) is not None
        assert store.get(KEY, days[1]) is None  # a failure stores nothing: it is asked again
        retry = ensure_blocks(
            FakeSource(), store, UNDS, [(KEY, d) for d in days], today=D3 + dt.timedelta(days=30)
        )
        assert retry["requests"] == 1 and retry["failed"] == 0

    def test_a_need_of_an_unknown_underlying_is_a_failure_not_a_crash(self, store):
        odd = RollingKey("FINNIFTY", 1, "CE", 0)
        src = FakeSource()
        out = ensure_blocks(src, store, UNDS, [(odd, D1), (KEY, D1)], today=LONG_AGO_TODAY)
        assert out["failed"] == 1 and out["requests"] == 1
        assert store.get(KEY, D1) is not None

    def test_duplicate_needs_are_asked_for_once(self, store):
        src = FakeSource()
        out = ensure_blocks(src, store, UNDS, [(KEY, D1)] * 4, today=LONG_AGO_TODAY)
        assert out["requests"] == 1 and len(src.calls) == 1

    def test_needs_may_be_a_generator(self, store):
        src = FakeSource()
        out = ensure_blocks(
            src, store, UNDS, ((KEY, D1 + dt.timedelta(days=3 * i)) for i in range(3)),
            today=LONG_AGO_TODAY,
        )  # fmt: skip
        assert out["requests"] == 3

    def test_progress_is_reported_per_request_and_names_a_failure(self, store):
        notes: list[tuple] = []
        src = FakeSource(fail_days={D3})
        ensure_blocks(
            src, store, UNDS, [(KEY, D1), (KEY, D3)],
            progress=lambda i, n, msg: notes.append((i, n, msg)), today=LONG_AGO_TODAY,
        )  # fmt: skip
        assert [(i, n) for i, n, _ in notes] == [(1, 2), (2, 2)]
        assert "bars" in notes[0][2] and "FAILED" in notes[1][2] and "DH-904" in notes[1][2]

    def test_the_cached_count_includes_blocks_a_request_just_brought(self, store):
        needs = [(KEY, D1 + dt.timedelta(days=i)) for i in (0, 1)]
        assert ensure_blocks(FakeSource(), store, UNDS, needs, today=LONG_AGO_TODAY) == self.counts(
            requests=1, empty=2, cached=1
        )

    def test_several_series_are_fetched_independently(self, store):
        ce, pe = KEY, RollingKey("NIFTY", 1, "PE", 0)
        src = FakeSource()
        out = ensure_blocks(src, store, UNDS, [(ce, D1), (pe, D1)], today=LONG_AGO_TODAY)
        assert out["requests"] == 2
        assert {c[4] for c in src.calls} == {"CALL", "PUT"}


# ── small helpers ─────────────────────────────────────────────────────────────────────────
class TestHelpers:
    def test_minute_index_maps_a_bars_stamp_to_its_row(self):
        b = block(KEY, D1, bar(D1, "10:00"), bar(D1, "10:01"), bar(D1, "10:05"))
        assert minute_index(b) == {
            epoch(D1, "10:00"): 0,
            epoch(D1, "10:01"): 1,
            epoch(D1, "10:05"): 2,
        }

    def test_the_index_of_an_empty_block_is_empty(self):
        assert minute_index(Block(KEY, D1, True, parse_leg({}))) == {}

    def test_ist_epoch_of_an_aware_datetime_and_timestamp(self):
        import pandas as pd

        when = dt.datetime(2026, 10, 1, 9, 15, tzinfo=IST)
        assert ist_epoch(when) == epoch(D1, "09:15")
        assert ist_epoch(pd.Timestamp(when)) == epoch(D1, "09:15")
        utc = dt.datetime(2026, 10, 1, 3, 45, tzinfo=dt.timezone.utc)
        assert ist_epoch(utc) == epoch(D1, "09:15")

    @pytest.mark.parametrize("bad", ["2026-10-01", 1790000000, None])
    def test_ist_epoch_refuses_what_is_not_a_timestamp(self, bad):
        with pytest.raises(TypeError, match="not a timestamp"):
            ist_epoch(bad)
