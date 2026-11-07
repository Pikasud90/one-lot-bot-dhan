"""US-002 mode B: AC1 to AC8 and the moved US-001 AC4 clause, end to end through the REAL Desk.

Everything here goes through `Desk.poll()`: a real Desk over an offline bar source, a real
PaperBook with the real instrument specs and cost tables, alerts raised by
`Desk._raise_signal`, the book stepped by the poll with the Desk's own `_paper_strategy_exit`
hook. The bars after the alert are SCRIPTED (the fill bar keeps its original print; every bar
after it is rewritten from the ALERT's own stop and target), so each expected number is derived
from the alert the owner was told and an exact oracle, never from the book.

`evaluate` is frozen after the warm-up poll, so the strategies cannot raise stray alerts or
rewrite the exit flags under the test; the exit hook is driven by setting the slot's flags.

The fixtures follow the US-001 desk harness: 2-minute bars on the NSE grid from 09:15, the alert
raised 41 bars before the end of the held bars, so it fills on the next bar and 39 bars follow.
"""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import math
from fractions import Fraction

import pandas as pd
import pytest
import us001_helpers as H
import us002_helpers as U
import us023_helpers as W

from onelot.config import AppConfig, RiskCfg
from onelot.data.universe import load_specs

FRAME_COLUMNS = ["open", "high", "low", "close"]
CHEAP = 0.0005  # the quiet bars wander +/- 0.05 % around the fill open: far from stop and target


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def app_cfg():
    return AppConfig.load()


@pytest.fixture
def desk_db(tmp_path):
    d = H.new_db(tmp_path, "desk.db", make_global=True)
    yield d
    H.dispose_db(d, was_global=True)


class Rig:
    """A warmed Desk with a real PaperBook and scripted bars after each alert."""

    def __init__(self, app_cfg, desk_db, *, cfg=None, served=None):
        self.cfg = cfg or app_cfg
        self.frames = H.desk_frames()
        self.original = {k: v.copy() for k, v in self.frames.items()}
        self.book = H.desk_book(self.cfg, desk_db.engine)
        self.desk, self.notifier = H.new_desk(
            self.cfg, desk_db, paper=self.book, frames=self.frames, served=served
        )
        self.desk.poll()
        assert not self.desk.signals, "harness: a stray signal fired during the first poll"
        for tracker in self.desk.trackers.values():  # strategies must not move under the test
            tracker.evaluate = lambda *args, **kwargs: []
        self.engine = desk_db.engine
        _, self.specs = load_specs()

    def alert(self, key="SBIN", direction="long"):
        sig = H.raise_alert(self.desk, key, now=H.mid_session(self.desk, key), direction=direction)
        self.desk.signals.clear()  # resolved: the instrument may raise another alert later
        return sig

    def index(self, key):
        return self.original[key].index

    def fill_bar(self, key):
        return self.index(key)[-40]

    def fill_open(self, key):
        return float(self.original[key]["open"].iloc[-40])

    def script(self, key, overrides=None, price=None):
        """Rewrite the 39 bars after the fill bar: quiet around `price` (default the fill open),
        with `overrides` {offset: (o, h, l, c)}, offset 0 being the bar right after the fill."""
        price = self.fill_open(key) if price is None else price
        step = round(price * CHEAP / 0.05) * 0.05
        quiet = (price, price + step, price - step, price)
        rows = [quiet] * 39
        for offset, row in (overrides or {}).items():
            rows[offset] = row
        frame = self.original[key].copy()
        frame.iloc[-39:, [frame.columns.get_loc(c) for c in FRAME_COLUMNS]] = rows
        self.desk.source.frames[key] = frame
        return frame

    def poll(self):
        self.desk.poll()

    def row(self, alert):
        (row,) = [r for r in H.trade_rows(self.engine) if r["alert_id"] == alert["id"]]
        return {**row, "status": H.val(row["status"])}

    def bar_ts(self, key, offset):
        return self.index(key)[-39 + offset]

    def slippage(self):
        return self.cfg.backtest.slippage_pct


def levels(alert):
    """The alert's stop and target in paise. A STOCK target lives on the tick grid, snapped once
    against the trader (long floors, short ceils: review 1 MINOR-3); an option's underlying
    target and every stop are the alert's own levels."""
    eco = alert["economics"]
    stop = H.to_paise(eco["underlying_stop"])
    if eco.get("underlying_target") is None:
        return stop, None
    target = H.to_paise(eco["underlying_target"])
    if alert["kind"] == "STOCK":
        target = U.snap_target(target, alert["direction"])
    return stop, target


def rupees(paise):
    return paise / 100.0


@pytest.fixture
def rig(app_cfg, desk_db):
    return Rig(app_cfg, desk_db)


class TestStockThroughTheDeskPoll:
    def test_the_alert_opens_a_paper_trade_that_fills_next_bar_and_is_managed_in_the_same_poll(
        self, rig
    ):
        alert = rig.alert("SBIN")
        rig.script("SBIN")
        rig.poll()
        row = rig.row(alert)
        assert row["entry_paise"] == H.expected_stock_entry(
            rig.fill_open("SBIN"), "long", rig.slippage()
        )
        assert H.same_instant(row["fill_bar_ts"], rig.fill_bar("SBIN"))
        assert row["status"] == "CLOSED" and row["close_reason"] == "SESSION_END"

    def test_ac1_a_touch_of_the_stop_closes_stop_at_the_alerts_stop_slipped(self, rig):
        alert = rig.alert("SBIN")
        stop, _ = levels(alert)
        o = rig.fill_open("SBIN")
        rig.script("SBIN", {2: (o, o, rupees(stop) - 0.5, rupees(stop) + 0.2)})
        rig.poll()
        row = rig.row(alert)
        assert row["status"] == "CLOSED" and row["close_reason"] == "STOP"
        assert row["exit_paise"] == U.expected_stock_exit(stop, "long", "STOP", rig.slippage())
        assert H.same_instant(row["exit_ts"], rig.bar_ts("SBIN", 2)), "the bar's OPEN time"
        assert row["gross_paise"] == row["exit_paise"] - row["entry_paise"]
        # US-023 books the two legs' charges with the close (was: NULL until then)
        costs = W.expected_costs(
            W.model_for("SBIN"), "STOCK", "long", row["entry_paise"], row["exit_paise"], 1, True
        )
        assert (row["costs_paise"], row["net_paise"]) == (costs, row["gross_paise"] - costs)

    def test_ac1_a_gap_through_the_stop_exits_at_the_open_slipped(self, rig):
        alert = rig.alert("SBIN")
        stop, _ = levels(alert)
        gap_open = rupees(stop) - 1.0
        rig.script("SBIN", {1: (gap_open, gap_open + 0.1, gap_open - 0.1, gap_open)})
        rig.poll()
        row = rig.row(alert)
        gap = H.to_paise(gap_open)
        assert row["close_reason"] == "STOP"
        assert row["exit_paise"] == U.expected_stock_exit(gap, "long", "STOP", rig.slippage())
        assert H.same_instant(row["exit_ts"], rig.bar_ts("SBIN", 1))

    def test_ac1_stop_beats_target_on_a_bar_that_touches_both(self, rig):
        alert = rig.alert("SBIN")
        stop, target = levels(alert)
        o = rig.fill_open("SBIN")
        rig.script("SBIN", {0: (o, rupees(target) + 0.5, rupees(stop) - 0.5, o)})
        rig.poll()
        assert rig.row(alert)["close_reason"] == "STOP"

    def test_ac2_a_target_traded_through_exits_at_exactly_the_alerts_target_unslipped(self, rig):
        alert = rig.alert("SBIN")
        _, target = levels(alert)
        assert target is not None, "the desk's alert must carry a target for this strategy"
        o = rig.fill_open("SBIN")
        rig.script("SBIN", {3: (o, rupees(target) + 0.05, o - 0.05, rupees(target))})
        rig.poll()
        row = rig.row(alert)
        assert row["close_reason"] == "TARGET" and row["exit_paise"] == target
        assert H.same_instant(row["exit_ts"], rig.bar_ts("SBIN", 3))

    def test_ac2_reaching_the_target_exactly_does_not_fill_it(self, rig):
        alert = rig.alert("SBIN")
        _, target = levels(alert)
        o = rig.fill_open("SBIN")
        # the high lands EXACTLY on the target price (to the paise): not traded through
        rig.script("SBIN", {3: (o, rupees(target), o - 0.05, o)})
        rig.poll()
        assert rig.row(alert)["close_reason"] == "SESSION_END"

    def test_ac5_the_session_end_is_the_first_open_from_exit_time_slipped(self, rig):
        alert = rig.alert("SBIN")
        rig.script("SBIN")
        rig.poll()
        row = rig.row(alert)
        exit_bar = [t for t in rig.index("SBIN")[-39:] if t.time() >= dt.time(15, 15)][0]
        assert row["close_reason"] == "SESSION_END" and H.same_instant(row["exit_ts"], exit_bar)
        level = H.to_paise(float(rig.desk.source.frames["SBIN"].loc[exit_bar, "open"]))
        assert row["exit_paise"] == U.expected_stock_exit(
            level, "long", "SESSION_END", rig.slippage()
        )

    def test_ac5_the_poll_runs_on_a_later_day_and_still_closes_at_the_sessions_own_exit(self, rig):
        """The bars end on a session that is over; the wall clock is days later. The close is
        at the session's 15:15 open, never at a later day's price (nothing is carried)."""
        alert = rig.alert("SBIN")
        rig.script("SBIN")
        rig.poll()
        row = rig.row(alert)
        assert dt.datetime.now(tz=H.IST).date() > row["exit_ts"].astimezone(H.IST).date()
        assert row["exit_ts"].astimezone(H.IST).time() == dt.time(15, 15)

    def test_ac6_a_second_poll_changes_nothing_and_books_nothing(self, rig):
        alert = rig.alert("SBIN")
        stop, _ = levels(alert)
        o = rig.fill_open("SBIN")
        rig.script("SBIN", {2: (o, o, rupees(stop) - 0.5, o)})
        rig.poll()
        before = (H.trade_rows(rig.engine), H.event_rows(rig.engine))
        rig.poll()
        rig.poll()
        after = (H.trade_rows(rig.engine), H.event_rows(rig.engine))
        strip = lambda rows: [{k: v for k, v in r.items() if k != "updated_at"} for r in rows]  # noqa: E731
        assert (strip(before[0]), before[1]) == (strip(after[0]), after[1])

    def test_ac6_the_snapshot_counts_the_close_and_no_hook_error(self, rig):
        alert = rig.alert("SBIN")
        rig.script("SBIN")
        rig.poll()
        block = rig.desk.snapshot()["paper"]
        assert block["closed"] == 1 and block["hook_errors"] == 0 and block["desk_hook_errors"] == 0
        assert rig.row(alert)["status"] == "CLOSED"

    def first_strategy_bar_close(self, rig, key, after, tf_min, res=2):
        """The first fine bar at or after offset `after` that closes a strategy bar, found by
        minute arithmetic on the 09:15 anchor (independent of the book)."""
        for offset in range(after, 38):
            ts = rig.bar_ts(key, offset)
            minutes = (ts.hour * 60 + ts.minute) - (9 * 60 + 15)
            if (minutes + res) % tf_min == 0:
                return offset
        raise AssertionError("no strategy-bar close among the scripted bars")

    def test_ac3_the_trail_arms_at_a_strategy_bar_close_with_the_alerts_frozen_trail_atr(self, rig):
        """The alert freezes the strategy's TRAIL ATR (manage.atr_trail, a tracker feature the desk
        now records). Two fine bars before a strategy-bar close the price closes in profit (the
        old per-fine-bar trail would arm there) and dips to that would-be stop: nothing happens.
        The strategy-bar close then arms the trail, and the NEXT bar's dip is a TRAIL_STOP at
        close - mult x atr_trail snapped down to the tick, hand-derived here with Fractions."""
        alert = rig.alert("SBIN")
        stop, target = levels(alert)
        manage = alert["manage"]
        assert manage["tf_min"] == 10 and manage["trail_atr_mult"] > 0
        atr = manage.get("atr_trail")
        assert isinstance(atr, float) and atr > 0, "the desk must freeze atr_trail in the alert"
        o = rig.fill_open("SBIN")
        entry = H.expected_stock_entry(o, "long", rig.slippage())
        risk = entry - stop
        distance = Fraction(str(atr)) * 100 * Fraction(str(manage["trail_atr_mult"]))
        arm_close = max(
            entry + math.ceil(manage["trail_activate_r"] * risk) + 40,
            stop + math.ceil(distance) + 120,
        )
        trailed = 5 * math.floor((arm_close - distance) / 5)
        assert arm_close < target and trailed - 50 > stop, "the scenario must trail, not target"
        k = self.first_strategy_bar_close(rig, "SBIN", after=3, tf_min=10)
        up = (o, rupees(arm_close) + 0.05, o - 0.05, rupees(arm_close))
        dip = (
            rupees(arm_close),
            rupees(arm_close) + 0.05,
            rupees(trailed) - 0.5,
            rupees(arm_close),
        )
        arms = (
            rupees(arm_close),
            rupees(arm_close) + 0.05,
            rupees(trailed) + 0.5,
            rupees(arm_close),
        )
        stops = (
            rupees(arm_close),
            rupees(arm_close) + 0.05,
            rupees(trailed) - 0.5,
            rupees(trailed),
        )
        rig.script("SBIN", {k - 2: up, k - 1: dip, k: arms, k + 1: stops})
        rig.poll()
        row = rig.row(alert)
        assert row["close_reason"] == "TRAIL_STOP", row["close_reason"]
        assert H.same_instant(row["exit_ts"], rig.bar_ts("SBIN", k + 1)), "not at the early dip"
        assert row["exit_paise"] == U.expected_stock_exit(
            trailed, "long", "TRAIL_STOP", rig.slippage()
        )
        moved = [e for e in H.event_rows(rig.engine, alert["id"]) if e["kind"] == "STOP_MOVED"]
        assert len(moved) == 1 and U.mentions(moved[0]["detail"], trailed)
        assert H.same_instant(moved[0]["ts"], rig.bar_ts("SBIN", k) + dt.timedelta(minutes=2))

    def test_ac3_the_book_equals_manage_step_with_the_rules_read_from_the_alert(self, rig):
        alert = rig.alert("SBIN")
        stop, target = levels(alert)
        manage = alert["manage"]
        o = rig.fill_open("SBIN")
        entry = H.expected_stock_entry(o, "long", rig.slippage())
        k = self.first_strategy_bar_close(rig, "SBIN", after=2, tf_min=10)
        gain = max(math.ceil(manage["trail_activate_r"] * (entry - stop)) + 40, 1300)
        up = (o, rupees(entry + gain) + 0.05, o - 0.05, rupees(entry + gain))
        dip = (rupees(entry + gain), rupees(entry + gain) + 0.05, o - 0.05, o)
        frame = rig.script("SBIN", {k: up, k + 1: dip})
        rig.poll()
        row = rig.row(alert)
        bars = frame.iloc[-40:]
        rules = U.manage().ManageRules(
            target_r=manage["target_r"],
            trail_atr_mult=manage["trail_atr_mult"],
            trail_activate_r=manage["trail_activate_r"],
            atr=manage.get("atr_trail", manage["atr"]),
            max_bars=manage["max_bars"],
            tf_min=manage["tf_min"],
            exit_time=dt.time(15, 15),
            tick_paise=5,
        )
        state = U.mk_state(
            "long",
            entry=entry,
            stop=stop,
            target=target,
            opened_at=rig.fill_bar("SBIN").to_pydatetime(),
        )
        end = (bars.index[-1] + pd.Timedelta(minutes=2)).to_pydatetime()
        _, events = U.manage().manage_step(state, bars, rules, end)
        close = U.close_of(events)
        assert close is not None and row["close_reason"] == close.reason
        assert H.same_instant(row["exit_ts"], close.at)
        assert row["exit_paise"] == U.expected_stock_exit(
            close.price, "long", close.reason, rig.slippage()
        )


class TestAShortStockThroughTheDeskPoll:
    def test_a_short_stop_and_target_mirror_through_the_poll(self, rig):
        alert = rig.alert("SBIN", "short")
        stop, target = levels(alert)
        o = rig.fill_open("SBIN")
        rig.script("SBIN", {2: (o, rupees(stop) + 0.5, o - 0.05, o)})
        rig.poll()
        row = rig.row(alert)
        assert row["entry_paise"] == H.expected_stock_entry(o, "short", rig.slippage())
        assert row["close_reason"] == "STOP"
        assert row["exit_paise"] == U.expected_stock_exit(stop, "short", "STOP", rig.slippage())
        assert row["gross_paise"] == -(row["exit_paise"] - row["entry_paise"])

    def test_a_short_target_is_a_profit_unslipped(self, rig):
        alert = rig.alert("SBIN", "short")
        _, target = levels(alert)
        if target is None:
            pytest.skip("this strategy sets no target for shorts")  # pragma: no cover
        o = rig.fill_open("SBIN")
        rig.script("SBIN", {3: (o, o + 0.05, rupees(target) - 0.05, rupees(target))})
        rig.poll()
        row = rig.row(alert)
        assert row["close_reason"] == "TARGET" and row["exit_paise"] == target
        assert row["gross_paise"] > 0


class TestTheDesksStrategyExitHook:
    def prepare(self, rig, direction="long", flags=None):
        alert = rig.alert("SBIN", direction)
        slot = rig.desk.trackers["SBIN"].slots[0]
        slot.last_flags = flags
        # the strategy's last closed 10-minute bar opened 5 fine bars after the fill bar
        slot.last_bar_ts = rig.index("SBIN")[-35].to_pydatetime()
        rig.script("SBIN")
        return alert, slot

    def exit_bar(self, rig, slot):
        due = slot.last_bar_ts + dt.timedelta(minutes=slot.tf_min)
        return [t for t in rig.index("SBIN")[-39:] if t >= due][0]

    @pytest.mark.parametrize(
        "direction, flags, reason",
        [
            ("long", {"long_exit": True}, "SIGNAL_EXIT"),
            ("long", {"short_entry": True}, "OPPOSITE_SIGNAL"),
            ("short", {"short_exit": True}, "SIGNAL_EXIT"),
            ("short", {"long_entry": True}, "OPPOSITE_SIGNAL"),
        ],
    )
    def test_the_strategys_flags_on_its_last_closed_bar_close_the_paper_trade_at_the_next_open(
        self, rig, direction, flags, reason
    ):
        alert, slot = self.prepare(rig, direction, flags)
        rig.poll()
        row = rig.row(alert)
        bar = self.exit_bar(rig, slot)
        assert row["close_reason"] == reason and H.same_instant(row["exit_ts"], bar)
        level = H.to_paise(float(rig.desk.source.frames["SBIN"].loc[bar, "open"]))
        assert row["exit_paise"] == U.expected_stock_exit(level, direction, reason, rig.slippage())
        assert rig.book.status()["hook_errors"] == 0 and rig.desk.hook_errors == 0

    def test_the_hook_answers_a_plain_tuple_with_the_strategy_bars_close_time(self, rig):
        alert, slot = self.prepare(rig, "long", {"long_exit": True})
        rig.poll()  # fills and closes; rebuild an OPEN trade object from the closed row's fields
        trade = rig.book.open_trades()
        assert trade == [], "the harness trade is closed; build one from the stored row instead"
        stored = H.trade_rows(rig.engine)[0]
        from onelot.paper.book import PaperTrade

        fake = PaperTrade.from_row({**stored, "status": "OPEN", "plan": stored["plan"]})
        answer = rig.desk._paper_strategy_exit(fake, dt.datetime.now(tz=H.IST))
        assert answer == ("SIGNAL_EXIT", slot.last_bar_ts + dt.timedelta(minutes=slot.tf_min))
        assert alert["id"] == fake.alert_id

    @pytest.mark.parametrize("change", ["unknown-series", "unknown-strategy", "wrong-timeframe"])
    def test_the_hook_says_nothing_for_a_trade_it_cannot_place(self, rig, change):
        alert, slot = self.prepare(rig, "long", {"long_exit": True})
        rig.poll()
        stored = H.trade_rows(rig.engine)[0]
        from onelot.paper.book import PaperTrade

        trade = PaperTrade.from_row({**stored, "status": "OPEN"})
        trade = dataclasses.replace(
            trade,
            **{
                "unknown-series": {"series_key": "NOSUCH"},
                "unknown-strategy": {"strategy": "no_such_strategy"},
                "wrong-timeframe": {"timeframe": "7m"},
            }[change],
        )
        assert rig.desk._paper_strategy_exit(trade, dt.datetime.now(tz=H.IST)) is None

    def test_the_hook_says_nothing_before_the_strategy_has_a_closed_bar_or_with_no_flags(self, rig):
        alert, slot = self.prepare(rig, "long", {})
        rig.poll()
        stored = H.trade_rows(rig.engine)[0]
        from onelot.paper.book import PaperTrade

        trade = PaperTrade.from_row({**stored, "status": "OPEN"})
        assert rig.desk._paper_strategy_exit(trade, dt.datetime.now(tz=H.IST)) is None
        slot.last_flags = {"long_exit": True}
        slot.last_bar_ts = None
        assert rig.desk._paper_strategy_exit(trade, dt.datetime.now(tz=H.IST)) is None
        assert alert["id"] == trade.alert_id

    def test_a_flag_left_over_from_before_the_fill_never_closes_the_trade(self, rig):
        """The strategy's last closed bar closed BEFORE the fill; its flags are still the desk's
        answer. The book ignores an exit older than the trade."""
        alert = rig.alert("SBIN")
        slot = rig.desk.trackers["SBIN"].slots[0]
        slot.last_flags = {"long_exit": True}
        slot.last_bar_ts = rig.index("SBIN")[-45].to_pydatetime()  # closed before the fill bar
        rig.script("SBIN")
        rig.poll()
        assert rig.row(alert)["close_reason"] == "SESSION_END"


class TestAnOptionThroughTheDeskPoll:
    def oracle(self, rig, alert, direction, underlying_exit, exit_ts):
        _, specs = load_specs()
        return U.oracle_option_roundtrip(
            bt=rig.cfg.backtest,
            spec=specs[alert["instrument_key"]],
            opt_type="CE" if direction == "long" else "PE",
            underlying_entry=rig.fill_open("BANKNIFTY"),
            underlying_exit=underlying_exit,
            entry_ts=rig.fill_bar("BANKNIFTY").to_pydatetime(),
            exit_ts=exit_ts,
            pricer=lambda option, underlying, ts: 100.0,  # the harness book's constant pricer
            stop=rupees(levels(alert)[0]),
        )

    @pytest.mark.parametrize("direction", ["long", "short"])
    def test_a_stop_on_the_underlying_is_a_loss_priced_by_the_lab_exit_fill(self, rig, direction):
        alert = rig.alert("BANKNIFTY", direction)
        stop, _ = levels(alert)
        o = rig.fill_open("BANKNIFTY")
        sign = 1 if direction == "long" else -1
        breach = rupees(stop) - sign * 5.0
        row_bar = (o, o + 5, o - 5, o)
        row_bar = (o, max(o, breach) + 1, min(o, breach) - 1, o)
        rig.script("BANKNIFTY", {2: row_bar})
        rig.poll()
        row = rig.row(alert)
        entry, exit_, gross = self.oracle(
            rig, alert, direction, rupees(stop), rig.bar_ts("BANKNIFTY", 2)
        )
        assert row["close_reason"] == "STOP" and row["status"] == "CLOSED"
        assert (row["entry_paise"], row["exit_paise"], row["gross_paise"]) == (entry, exit_, gross)
        assert row["gross_paise"] < 0, "long premium: a stop is a loss for a CE and for a PE alike"
        assert row["qty"] == rig.specs["BANKNIFTY_OPT"].lot_size

    @pytest.mark.parametrize("direction", ["long", "short"])
    def test_a_target_on_the_underlying_is_a_gain_for_a_ce_and_for_a_pe(self, rig, direction):
        alert = rig.alert("BANKNIFTY", direction)
        _, target = levels(alert)
        assert target is not None
        o = rig.fill_open("BANKNIFTY")
        sign = 1 if direction == "long" else -1
        through = rupees(target) + sign * 0.5
        bar = (o, max(o, through), min(o, through), o)
        rig.script("BANKNIFTY", {3: bar})
        rig.poll()
        row = rig.row(alert)
        entry, exit_, gross = self.oracle(
            rig, alert, direction, rupees(target), rig.bar_ts("BANKNIFTY", 3)
        )
        assert row["close_reason"] == "TARGET"
        assert (row["entry_paise"], row["exit_paise"], row["gross_paise"]) == (entry, exit_, gross)

    def test_the_session_end_of_an_option_is_priced_at_the_exit_bars_open_level(self, rig):
        alert = rig.alert("BANKNIFTY", "long")
        rig.script("BANKNIFTY")
        rig.poll()
        row = rig.row(alert)
        bar = [t for t in rig.index("BANKNIFTY")[-39:] if t.time() >= dt.time(15, 15)][0]
        level = float(rig.desk.source.frames["BANKNIFTY"].loc[bar, "open"])
        entry, exit_, gross = self.oracle(rig, alert, "long", level, bar.to_pydatetime())
        assert row["close_reason"] == "SESSION_END"
        assert (row["exit_paise"], row["gross_paise"]) == (exit_, gross)


class TestEveryPollReadsEachSeriesOnce:
    def test_bars_is_asked_once_per_series_per_poll_whatever_the_number_of_trades(self, rig):
        first = rig.alert("SBIN")
        # Since owner feedback OF-5 the desk keeps one live alert per instrument, so the second
        # trade on the same series is opened on the book directly: two trades is what this
        # test needs, whoever opened them.
        second = {**copy.deepcopy(first), "id": f"{first['id']}b"}
        rig.desk.paper.on_alert(second, H.mid_session(rig.desk, "SBIN"))
        option = rig.alert("BANKNIFTY")
        assert first["id"] != second["id"]
        calls = []
        original = rig.desk._paper_bars

        def counting(series_key):
            calls.append(series_key)
            return original(series_key)

        rig.desk._paper_bars = counting
        rig.script("SBIN")
        rig.script("BANKNIFTY")
        rig.poll()
        assert sorted(calls) == ["BANKNIFTY", "SBIN"], calls
        assert {rig.row(a)["status"] for a in (first, second, option)} <= {
            "CLOSED",
            "OPEN",
            "NO_FILL",
        }


class TestTheMovedUs001Ac4Clause:
    """A paper LOSS larger than risk.daily_loss_limit_inr closes, and governor.snapshot().as_dict()
    is identical before and after (the paper book never touches the RiskGovernor)."""

    def tiny(self, app_cfg):
        risk = RiskCfg(**{**app_cfg.risk.model_dump(), "daily_loss_limit_inr": 1.0})
        return app_cfg.model_copy(update={"risk": risk})

    @pytest.mark.parametrize(
        "key, direction",
        [("SBIN", "long"), ("SBIN", "short"), ("BANKNIFTY", "long"), ("BANKNIFTY", "short")],
    )
    def test_a_losing_paper_trade_closes_beyond_the_limit_and_the_governor_does_not_move(
        self, app_cfg, desk_db, monkeypatch, key, direction
    ):
        rig = Rig(app_cfg, desk_db, cfg=self.tiny(app_cfg))
        before = copy.deepcopy(rig.desk.governor.snapshot().as_dict())
        U.forbid_governor_mutation(monkeypatch)
        alert = rig.alert(key, direction)
        stop, _ = levels(alert)
        sign = 1 if direction == "long" else -1
        o = rig.fill_open(key)
        breach = rupees(stop) - sign * 1.0
        rig.script(key, {2: (o, max(o, breach) + 0.5, min(o, breach) - 0.5, o)})
        rig.poll()
        row = rig.row(alert)
        assert row["status"] == "CLOSED" and row["close_reason"] == "STOP"
        assert row["gross_paise"] < -100, "a loss larger than the Rs 1 daily limit"
        assert rig.desk.governor.snapshot().as_dict() == before
        assert rig.desk.snapshot()["risk"] == before
        assert rig.desk.governor.may_open(dt.datetime.now(tz=H.IST))[0] is True
        assert rig.desk.positions == {} and H.count_rows(rig.engine, "desk_positions") == 0


class TestADelayedTradeThroughTheDeskPoll:
    def test_a_yahoo_served_series_is_graded_delayed_and_still_managed_to_a_close(
        self, app_cfg, desk_db
    ):
        rig = Rig(app_cfg, desk_db, served={"BANKNIFTY": "dhan", "SBIN": "yahoo"})
        alert = rig.alert("SBIN")
        stop, _ = levels(alert)
        o = rig.fill_open("SBIN")
        rig.script("SBIN", {2: (o, o, rupees(stop) - 0.5, o)})
        rig.poll()
        row = rig.row(alert)
        assert row["grade"] == "delayed"
        assert row["close_reason"] == "STOP"
        assert row["exit_paise"] == U.expected_stock_exit(stop, "long", "STOP", rig.slippage())
        assert rig.book.status()["hook_errors"] == 0
