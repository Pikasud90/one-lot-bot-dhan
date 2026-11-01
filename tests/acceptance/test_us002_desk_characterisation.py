"""CHARACTERISATION of the desk's CURRENT advice (US-002 NFR; protects US-025).

The desk (`Desk._manage_positions`) advises a manually taken position from ONE live price: it
never closes anything, a touch counts, nothing is bar-aware. This story leaves the desk UNTOUCHED,
so every test here must pass today and keep passing when US-025 makes `_manage_positions` call
`onelot/desk/rules.py` (ratchet_stop, r_multiple, bars_held, sessions_held, session_due,
exit_priority): "every characterised advice is unchanged" (US-025 AC1).

Advice values pinned: STOP, TRAIL_STOP, TARGET, SIGNAL_EXIT, OPPOSITE_SIGNAL, SQUARE_OFF (the
desk's own label: the paper book's enum says SESSION_END), TIME_STOP (intraday max_bars and
swing sessions). Precedence in the desk, as it is today:

    STOP / TRAIL_STOP  >  TARGET  >  SIGNAL_EXIT / OPPOSITE_SIGNAL  >  SQUARE_OFF  >  TIME_STOP

NOTE for US-025: this is NOT `exit_priority` (STOP > TARGET > SESSION_END > SIGNAL_EXIT > ...):
the desk lets a strategy exit shadow SQUARE_OFF. `test_a_strategy_exit_shadows_square_off` pins
the desk's order, so adopting `exit_priority` there is a conscious decision, not an accident.

Known desk behaviours deliberately NOT pinned (listed in the TEST REPORT): the square-off check is
not date-aware (the paper rules fix that), and `sessions_held` drops the opening session when
`opened_at` carries seconds.
"""

from __future__ import annotations

import datetime as dt

import pytest
import us001_helpers as H
import us002_helpers as U

from onelot.config import AppConfig

NOW = H.at(11, 0)


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


class Harness:
    """A real Desk over an offline source; the tracker's last close is the live price."""

    def __init__(self, app_cfg, desk_db):
        self.desk, self.notifier = H.new_desk(app_cfg, desk_db)

    def advise(
        self, position, closes=None, *, key="SBIN", now=NOW, window=True, flags=None, regime=None
    ):
        tracker = self.desk.trackers[key]
        if closes is not None:
            tracker.bars = U.close_series(closes, now - dt.timedelta(minutes=2 * len(closes)))
        tracker.live_quote = None
        if flags is not None:
            tracker.slots[0].last_flags = flags
        if regime is not None:
            tracker.regime = regime
        self.desk.positions[position["id"]] = position
        self.desk._manage_positions(now, H.OpenClock().current_window(now) if window else None)
        return position

    def exits(self):
        return [n for n in self.notifier.sent if n.level.value == "EXIT"]


@pytest.fixture
def harness(app_cfg, desk_db):
    return Harness(app_cfg, desk_db)


def pos(**kw):
    return U.desk_position(**kw)


class TestStopAndTarget:
    @pytest.mark.parametrize(
        "last, advice", [(1000.0, None), (990.05, None), (990.0, "STOP"), (989.5, "STOP")]
    )
    def test_a_long_is_advised_to_stop_when_the_price_touches_or_passes_the_stop(
        self, harness, last, advice
    ):
        p = harness.advise(pos(), [1000.0, last])
        assert p["exit_advice"] == advice

    @pytest.mark.parametrize(
        "last, advice", [(1000.0, None), (1009.95, None), (1010.0, "STOP"), (1011.0, "STOP")]
    )
    def test_a_short_is_advised_to_stop_when_the_price_touches_or_passes_the_stop(
        self, harness, last, advice
    ):
        assert harness.advise(pos(direction="short"), [1000.0, last])["exit_advice"] == advice

    @pytest.mark.parametrize(
        "last, advice", [(1019.95, None), (1020.0, "TARGET"), (1030.0, "TARGET")]
    )
    def test_a_long_target_is_advised_on_a_touch(self, harness, last, advice):
        assert harness.advise(pos(), [1000.0, last])["exit_advice"] == advice

    @pytest.mark.parametrize("last, advice", [(980.05, None), (980.0, "TARGET"), (970.0, "TARGET")])
    def test_a_short_target_is_advised_on_a_touch(self, harness, last, advice):
        assert harness.advise(pos(direction="short"), [1000.0, last])["exit_advice"] == advice

    def test_no_target_means_no_target_advice(self, harness):
        assert harness.advise(pos(target=None), [1000.0, 5000.0])["exit_advice"] is None

    def test_the_desk_only_advises_the_position_stays_open_and_an_exit_alert_goes_out(
        self, harness
    ):
        p = harness.advise(pos(), [1000.0, 989.0])
        assert p["exit_advice"] == "STOP" and p["status"] == "OPEN"
        assert p["id"] in harness.desk.positions
        (alert,) = harness.exits()
        assert alert.payload["advice"] == "STOP" and alert.position_id == p["id"]

    def test_the_marks_are_refreshed_every_poll(self, harness):
        p = harness.advise(pos(), [1000.0, 1005.0])
        assert p["last_underlying"] == 1005.0 and p["r_now"] == 0.5 and p["exit_advice"] is None
        assert "HOLD" in p["hold_check"]


class TestTheTrail:
    TRAIL = {"trail_atr_mult": 1.5, "atr": 4.0, "trail_activate_r": 1.0}

    def test_at_plus_one_r_the_stop_ratchets_to_price_minus_mult_atr(self, harness):
        p = harness.advise(pos(manage=self.TRAIL), [1000.0, 1010.0])
        assert p["stop_underlying"] == 1004.0 and p["trail_active"] is True
        assert p["exit_advice"] is None

    def test_just_below_the_activation_nothing_moves(self, harness):
        p = harness.advise(pos(manage=self.TRAIL), [1000.0, 1009.95])
        assert p["stop_underlying"] == 990.0 and p["trail_active"] is False

    def test_a_short_ratchets_down(self, harness):
        p = harness.advise(pos(direction="short", manage=self.TRAIL), [1000.0, 990.0])
        assert p["stop_underlying"] == 996.0 and p["trail_active"] is True

    def test_the_stop_never_loosens_and_only_tightens_on_a_better_price(self, harness):
        p = pos(manage=self.TRAIL)
        harness.advise(p, [1000.0, 1010.0])
        assert p["stop_underlying"] == 1004.0
        harness.advise(p, [1000.0, 1006.0])  # still above the stop, below +1 R
        assert p["stop_underlying"] == 1004.0 and p["exit_advice"] is None
        harness.advise(p, [1000.0, 1011.0])  # a better price tightens it
        assert p["stop_underlying"] == 1005.0
        harness.advise(p, [1000.0, 1007.0])  # falling back never loosens
        assert p["stop_underlying"] == 1005.0

    def test_a_moved_stop_that_is_touched_is_advised_trail_stop(self, harness):
        p = pos(manage=self.TRAIL)
        harness.advise(p, [1000.0, 1010.0])
        harness.advise(p, [1000.0, 1004.0])
        assert p["exit_advice"] == "TRAIL_STOP"

    def test_a_stop_touched_before_the_trail_ever_moved_is_plain_stop(self, harness):
        p = pos(manage=self.TRAIL)
        harness.advise(p, [1000.0, 1005.0])
        harness.advise(p, [1000.0, 989.0])
        assert p["trail_active"] is False and p["exit_advice"] == "STOP"

    @pytest.mark.parametrize("manage", [{"trail_atr_mult": 0.0, "atr": 4.0}], ids=["zero-multiple"])
    def test_a_zero_multiple_never_trails(self, harness, manage):
        p = harness.advise(pos(manage=manage), [1000.0, 1100.0])
        assert p["stop_underlying"] == 990.0 and p["trail_active"] is False

    def test_the_desk_FALLS_BACK_to_the_live_regime_atr_when_the_alert_has_none(self, harness):
        """The paper book deliberately does NOT (ATR frozen at the alert, ADR-001 amendment 7)."""
        manage = {**self.TRAIL, "atr": None}
        p = harness.advise(pos(manage=manage), [1000.0, 1010.0], regime={"atr": 4.0})
        assert p["stop_underlying"] == 1004.0 and p["trail_active"] is True

    def test_with_no_atr_anywhere_the_desk_does_not_trail(self, harness):
        manage = {**self.TRAIL, "atr": None}
        p = harness.advise(pos(manage=manage), [1000.0, 1100.0], regime={})
        assert p["stop_underlying"] == 990.0 and p["trail_active"] is False


class TestStrategyExits:
    @pytest.mark.parametrize(
        "direction, flags, advice",
        [
            ("long", {"long_exit": True}, "SIGNAL_EXIT"),
            ("long", {"short_entry": True}, "OPPOSITE_SIGNAL"),
            ("short", {"short_exit": True}, "SIGNAL_EXIT"),
            ("short", {"long_entry": True}, "OPPOSITE_SIGNAL"),
            ("long", {"long_entry": True}, None),
            ("short", {"short_entry": True}, None),
            ("long", {"short_exit": True}, None),
        ],
    )
    def test_the_strategys_latest_closed_bar_flags_become_the_advice(
        self, harness, direction, flags, advice
    ):
        p = harness.advise(pos(direction=direction), [1000.0, 1000.0], flags=flags)
        assert p["exit_advice"] == advice

    def test_an_exit_flag_wins_over_an_opposite_flag(self, harness):
        p = harness.advise(pos(), [1000.0, 1000.0], flags={"long_exit": True, "short_entry": True})
        assert p["exit_advice"] == "SIGNAL_EXIT"

    def test_a_stop_shadows_a_strategy_exit(self, harness):
        p = harness.advise(pos(), [1000.0, 989.0], flags={"long_exit": True})
        assert p["exit_advice"] == "STOP"

    def test_a_target_shadows_a_strategy_exit(self, harness):
        p = harness.advise(pos(), [1000.0, 1021.0], flags={"short_entry": True})
        assert p["exit_advice"] == "TARGET"

    def test_only_the_slot_of_the_positions_own_strategy_and_timeframe_speaks(self, harness):
        tracker = harness.desk.trackers["SBIN"]
        other = tracker.slots[1]  # supertrend_vol 6m
        other.last_flags = {"long_exit": True}
        p = harness.advise(pos(), [1000.0, 1000.0])
        assert p["exit_advice"] is None, "ema_cross 10m has no flag; supertrend's must not leak"


class TestSquareOff:
    @pytest.mark.parametrize(
        "now, advice",
        [
            (H.at(15, 14, 59), None),
            (H.at(15, 15), "SQUARE_OFF"),
            (H.at(15, 29), "SQUARE_OFF"),
        ],
    )
    def test_an_intraday_position_is_told_to_square_off_from_exit_time(self, harness, now, advice):
        assert harness.advise(pos(), [1000.0, 1000.0], now=now)["exit_advice"] == advice

    def test_a_custom_exit_time_in_the_manage_block_is_honoured(self, harness):
        p = pos(manage={"exit_time": "14:45"})
        assert harness.advise(p, [1000.0, 1000.0], now=H.at(14, 45))["exit_advice"] == "SQUARE_OFF"

    def test_without_a_session_window_there_is_no_square_off_advice(self, harness):
        assert (
            harness.advise(pos(), [1000.0, 1000.0], now=H.at(15, 20), window=False)["exit_advice"]
            is None
        )

    def test_a_swing_position_is_never_told_to_square_off(self, harness):
        p = pos(horizon="swing")
        assert harness.advise(p, [1000.0, 1000.0], now=H.at(15, 20))["exit_advice"] is None

    def test_a_strategy_exit_shadows_square_off_unlike_exit_priority(self, harness):
        """The desk order: SIGNAL_EXIT before SQUARE_OFF. `exit_priority` ranks SESSION_END first."""
        p = harness.advise(pos(), [1000.0, 1000.0], now=H.at(15, 20), flags={"long_exit": True})
        assert p["exit_advice"] == "SIGNAL_EXIT"

    def test_a_stop_and_a_target_still_shadow_square_off(self, harness):
        assert harness.advise(pos(), [1000.0, 989.0], now=H.at(15, 20))["exit_advice"] == "STOP"
        assert harness.advise(pos(), [1000.0, 1021.0], now=H.at(15, 20))["exit_advice"] == "TARGET"


class TestTimeStops:
    def opened_at(self):
        return H.at(10, 0)  # on the minute: sessions_held then counts the opening session

    @pytest.mark.parametrize(
        "minutes, bars, advice", [(29, 2, None), (30, 3, "TIME_STOP"), (45, 4, "TIME_STOP")]
    )
    def test_intraday_max_bars_is_counted_in_strategy_bars_since_the_open(
        self, harness, minutes, bars, advice
    ):
        p = pos(manage={"max_bars": 3, "tf_min": 10}, opened_at=self.opened_at())
        now = self.opened_at() + dt.timedelta(minutes=minutes, seconds=59)
        harness.advise(p, [1000.0, 1000.0], now=now)
        assert p["bars_held"] == bars and p["exit_advice"] == advice

    def test_max_bars_zero_never_times_out(self, harness):
        p = pos(manage={"max_bars": 0, "tf_min": 10}, opened_at=self.opened_at())
        harness.advise(p, [1000.0, 1000.0], now=self.opened_at() + dt.timedelta(hours=4))
        assert p["exit_advice"] is None

    def test_max_bars_does_not_apply_to_a_swing_position(self, harness):
        p = pos(horizon="swing", manage={"max_bars": 3, "tf_min": 10}, opened_at=self.opened_at())
        harness.advise(p, [1000.0, 1000.0], now=self.opened_at() + dt.timedelta(hours=4))
        assert p["exit_advice"] is None

    def test_square_off_shadows_the_intraday_time_stop(self, harness):
        p = pos(manage={"max_bars": 3, "tf_min": 10}, opened_at=self.opened_at())
        harness.advise(p, [1000.0, 1000.0], now=H.at(15, 20))
        assert p["exit_advice"] == "SQUARE_OFF"

    def days(self, n):
        """A close per day for `n` consecutive days from the opening day, 10:00 each."""
        return U.close_series([1000.0] * n, H.at(10, 0), step=dt.timedelta(days=1))

    def test_a_swing_position_times_out_only_after_more_than_swing_max_sessions(self, harness):
        limit = harness.desk.cfg.swing_max_sessions
        p = pos(horizon="swing", opened_at=self.opened_at())
        tracker = harness.desk.trackers["SBIN"]
        tracker.bars = self.days(limit)
        harness.advise(p, now=H.at(10, 0) + dt.timedelta(days=limit - 1, minutes=5))
        assert p["sessions_held"] == limit and p["exit_advice"] is None
        tracker.bars = self.days(limit + 1)
        harness.advise(p, now=H.at(10, 0) + dt.timedelta(days=limit, minutes=5))
        assert p["sessions_held"] == limit + 1 and p["exit_advice"] == "TIME_STOP"

    def test_the_session_count_does_not_apply_to_an_intraday_position(self, harness):
        p = pos(opened_at=self.opened_at())
        harness.desk.trackers["SBIN"].bars = self.days(9)
        harness.advise(p, now=H.at(10, 0) + dt.timedelta(days=8, minutes=5))
        assert p["exit_advice"] != "TIME_STOP"


class TestAnIndexOptionIsAdvisedOnTheUnderlying:
    def test_a_buy_pe_is_a_short_with_the_stop_above(self, harness):
        p = pos(
            kind="INDEX",
            direction="short",
            series_key="BANKNIFTY",
            entry=54000.0,
            stop=54150.0,
            target=53700.0,
        )
        harness.advise(p, [54000.0, 54100.0], key="BANKNIFTY")
        assert p["exit_advice"] is None
        harness.advise(p, [54000.0, 54150.0], key="BANKNIFTY")
        assert p["exit_advice"] == "STOP"

    def test_a_buy_pe_target_is_reached_by_a_falling_underlying(self, harness):
        p = pos(
            kind="INDEX",
            direction="short",
            series_key="BANKNIFTY",
            entry=54000.0,
            stop=54150.0,
            target=53700.0,
        )
        harness.advise(p, [54000.0, 53700.0], key="BANKNIFTY")
        assert p["exit_advice"] == "TARGET"

    def test_a_buy_ce_stops_on_a_falling_underlying(self, harness):
        p = pos(
            kind="INDEX",
            direction="long",
            series_key="BANKNIFTY",
            entry=54000.0,
            stop=53850.0,
            target=54300.0,
        )
        harness.advise(p, [54000.0, 53850.0], key="BANKNIFTY")
        assert p["exit_advice"] == "STOP"
