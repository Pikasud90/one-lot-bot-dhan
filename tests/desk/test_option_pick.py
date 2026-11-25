"""US-104: an option alert never names a dead contract.

On 25 Nov 2026 three of five index option alerts named a contract nobody trades (no open
interest, no two-sided quote): its stale last price made the broker's delta land near 0.65 by
accident. The pick is now made among TRADEABLE rows only (`onelot.desk.option_pick`): a last
price, a bid and an ask above 0, ask at or above bid, open interest above 0 (skipped when the
chain reports it for no row of the side), and a spread of at most `option_max_spread_pct` %.
Every "now" is explicit; nothing reads the wall clock.
"""

from __future__ import annotations

import datetime as dt
import itertools
import math
import random
from types import SimpleNamespace

import pytest
from desk_harness import new_desk
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import create_engine

from onelot.broker.base import ChainRow, ChainSnapshot
from onelot.clock import IST
from onelot.config import AppConfig, BacktestCfg
from onelot.data.universe import load_specs
from onelot.desk.economics import (
    NO_TRADEABLE_NOTE,
    chain_economics,
    index_option_economics_from_chain,
)
from onelot.desk.fillcost import FillCostRecorder
from onelot.desk.option_pick import pick_contract, tradeable
from onelot.desk.tracker import Verdict
from onelot.store import init_database

NOW = dt.datetime(2026, 11, 25, 13, 14, tzinfo=IST)  # a Wednesday afternoon, as recorded
BT = BacktestCfg()
SPECS = load_specs()[1]


def chain(rows, spot=25653.2, expiry="2026-12-29"):
    """A tiny chain: only `.rows`, `.expiry` and `.underlying_price` are read."""
    return SimpleNamespace(rows=list(rows), expiry=expiry, underlying_price=spot)


def live_pe(strike, delta, sid, ltp, oi=50_000.0, half=1.0):
    return ChainRow(
        strike, "PE", sid, ltp=ltp, bid=ltp - half, ask=ltp + half, oi=oi, iv=0.13, delta=delta
    )


def nifty_live_puts() -> list[ChainRow]:
    return [
        live_pe(25700.0, -0.52, "n25700", 205.0),
        live_pe(25800.0, -0.61, "n25800", 262.0),  # the live put nearest 0.65
        live_pe(25900.0, -0.70, "n25900", 330.0),
    ]


def dead_29200(bid=None, ask=None) -> ChainRow:
    """The NIFTY put of 25 Nov 13:14 exactly as recorded: no quote, no open interest."""
    return ChainRow(
        29200.0, "PE", "n29200", ltp=4962.0, bid=bid, ask=ask, oi=0.0, iv=0.851, delta=-0.646
    )


def nifty_put(rows, entry=25653.2, stop=25700.0):
    return index_option_economics_from_chain(
        SPECS["NIFTY_OPT"], "short", entry, stop, 2.0, chain(rows), NOW, BT
    )


# ── T1 the NIFTY case of 25 Nov ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "bid, ask", [(None, None), (0.0, 0.0), (4962.0, 4962.0)], ids=["none", "zero", "one-price"]
)
def test_t1_the_dead_nifty_put_is_never_picked_the_live_one_nearest_065_is(bid, ask):
    eco = nifty_put([*nifty_live_puts(), dead_29200(bid, ask)])
    assert eco is not None and eco.synthetic is False
    assert eco.option["strike"] == 25800.0 and eco.option["security_id"] == "n25800"
    assert eco.option["bid"] == 261.0 and eco.option["ask"] == 263.0  # the row's own
    assert eco.option["spread_pct"] == pytest.approx(2.0 / 263.0 * 100.0, abs=0.01)
    assert eco.entry_price == 263.0  # bought at the ask
    assert eco.option["oi"] == 50_000.0


def test_t1_the_old_rule_would_have_picked_the_dead_row():
    """Why the case is real: the dead row's |delta| 0.646 is the nearest 0.65 of the chain."""
    rows = [*nifty_live_puts(), dead_29200()]
    nearest = min(rows, key=lambda r: abs(abs(r.delta) - 0.65))
    assert nearest.strike == 29200.0


# ── T2 the MIDCPNIFTY case: a two-sided quote without open interest ──────────────────────
def test_t2_a_quoted_row_without_open_interest_is_not_picked_while_one_with_it_exists():
    dead = ChainRow(
        15800.0, "PE", "m15800", ltp=1500.0, bid=1499.0, ask=1501.0, oi=0.0, iv=0.625, delta=-0.643
    )
    live = ChainRow(
        14300.0, "PE", "m14300", ltp=180.0, bid=179.0, ask=181.0, oi=12_000.0, delta=-0.55
    )
    eco = index_option_economics_from_chain(
        SPECS["MIDCPNIFTY_OPT"], "short", 14262.0, 14300.0, 2.0, chain([dead, live], 14262.0),
        NOW, BT,
    )  # fmt: skip
    assert eco is not None and eco.option["security_id"] == "m14300"
    assert not tradeable(dead, 10.0) and tradeable(live, 10.0)


def test_t2_the_recorded_midcap_put_fails_on_its_spread_too():
    recorded = ChainRow(
        15800.0, "PE", "m15800", ltp=1500.0, bid=1313.45, ask=1678.50, oi=7.0, delta=-0.643
    )
    assert (1678.50 - 1313.45) / 1678.50 * 100.0 == pytest.approx(21.7, abs=0.05)
    assert not tradeable(recorded, 10.0)


# ── T3 a source without open interest ────────────────────────────────────────────────────
def test_t3_when_no_row_reports_open_interest_the_quotes_alone_decide():
    rows = [
        ChainRow(25800.0, "PE", "a", ltp=262.0, bid=261.0, ask=263.0, delta=-0.61),
        ChainRow(25900.0, "PE", "b", ltp=330.0, bid=None, ask=331.0, delta=-0.65),  # one side
    ]
    eco = nifty_put(rows)
    assert eco is not None and eco.option["security_id"] == "a"
    assert eco.option["oi"] is None


def test_t3_an_oi_of_zero_is_none_not_unknown():
    rows = [
        ChainRow(25800.0, "PE", "a", ltp=262.0, bid=261.0, ask=263.0, delta=-0.61, oi=0.0),
        ChainRow(25900.0, "PE", "b", ltp=330.0, bid=329.0, ask=331.0, delta=-0.65, oi=0.0),
    ]
    assert nifty_put(rows) is None


def test_t3_one_row_reporting_open_interest_means_the_side_reports_it():
    rows = [
        ChainRow(25800.0, "PE", "a", ltp=262.0, bid=261.0, ask=263.0, delta=-0.61, oi=900.0),
        ChainRow(25900.0, "PE", "b", ltp=330.0, bid=329.0, ask=331.0, delta=-0.65),  # oi None
    ]
    assert nifty_put(rows).option["security_id"] == "a"


# ── T4 a missing or zero side ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "bid, ask",
    [(None, 263.0), (261.0, None), (0.0, 263.0), (261.0, 0.0), (264.0, 263.0)],
    ids=["no-bid", "no-ask", "zero-bid", "zero-ask", "crossed"],
)
def test_t4_a_row_with_open_interest_but_no_two_sided_quote_is_never_picked(bid, ask):
    bad = ChainRow(25850.0, "PE", "bad", ltp=262.0, bid=bid, ask=ask, oi=80_000.0, delta=-0.65)
    good = live_pe(25700.0, -0.52, "good", 205.0)
    assert not tradeable(bad, 10.0)
    assert nifty_put([bad, good]).option["security_id"] == "good"


# ── T5 the spread cap ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "bid, ask, ok",
    [(90.0, 100.0, True), (89.99, 100.0, False), (78.3, 100.0, False), (99.0, 100.0, True)],
    ids=["at-cap", "just-above", "21.7pct", "narrow"],
)
def test_t5_the_spread_cap_is_inclusive(bid, ask, ok):
    row = ChainRow(25800.0, "PE", "x", ltp=95.0, bid=bid, ask=ask, oi=10.0, delta=-0.6)
    assert tradeable(row, 10.0) is ok


def test_t5_the_cap_is_a_setting_with_bounds():
    assert BacktestCfg().option_max_spread_pct == 10.0
    assert BacktestCfg(option_max_spread_pct=1.0).option_max_spread_pct == 1.0
    assert BacktestCfg(option_max_spread_pct=50.0).option_max_spread_pct == 50.0
    for bad in (0.99, 50.01, 0.0, -5.0):
        with pytest.raises(ValueError, match="option_max_spread_pct"):
            BacktestCfg(option_max_spread_pct=bad)


def test_t5_the_chain_pick_reads_the_cap_from_the_setting():
    row = ChainRow(25800.0, "PE", "x", ltp=95.0, bid=85.0, ask=100.0, oi=10.0, delta=-0.6)
    rows = [row]
    assert nifty_put(rows) is None  # 15% > 10%
    wide = BacktestCfg(option_max_spread_pct=20.0)
    eco = index_option_economics_from_chain(
        SPECS["NIFTY_OPT"], "short", 25653.2, 25700.0, 2.0, chain(rows), NOW, wide
    )
    assert eco is not None and eco.option["security_id"] == "x"


# ── T6 nothing tradeable ─────────────────────────────────────────────────────────────────
def test_t6_nothing_tradeable_is_none_and_says_so():
    rows = [dead_29200(), dead_29200(4962.0, 4962.0)]
    assert nifty_put(rows) is None
    got = chain_economics(SPECS["NIFTY_OPT"], "short", 25653.2, 25700.0, 2.0, chain(rows), NOW, BT)
    assert got.economics is None and got.untradeable is True


@pytest.mark.parametrize("rows", [[], [ChainRow(25800.0, "CE", "c", ltp=1.0, bid=1.0, ask=1.0)]])
def test_t6_a_chain_without_rows_of_the_side_is_not_untradeable(rows):
    got = chain_economics(SPECS["NIFTY_OPT"], "short", 25653.2, 25700.0, 2.0, chain(rows), NOW, BT)
    assert got.economics is None and got.untradeable is False


@pytest.fixture
def app_cfg() -> AppConfig:
    return AppConfig.load()


@pytest.fixture
def db(tmp_path):
    from onelot.data.db import get_database, use_database

    d = init_database(f"sqlite:///{tmp_path / 'pick.db'}")
    use_database(d)
    yield d
    d.dispose()
    get_database.__globals__["_db"] = None


def desk_economics(app_cfg, db, monkeypatch, live_chain):
    d, _, _ = new_desk(app_cfg, db)
    t = d.trackers["BANKNIFTY"]
    monkeypatch.setattr(d, "_live_chain", lambda tracker, now: live_chain)
    v = Verdict("long", "x", NOW, 45110.0, 45210.0, 60.0, [], True)
    return d._economics(t, t.slots[0], v, NOW)


def bank_calls_dead() -> list[ChainRow]:
    return [
        ChainRow(45000.0, "CE", "d1", ltp=420.0, oi=0.0, delta=0.64),
        ChainRow(45100.0, "CE", "d2", ltp=345.0, bid=300.0, ask=400.0, oi=0.0, delta=0.55),
    ]


def test_t6_the_desk_falls_back_to_the_model_and_says_why_first(app_cfg, db, monkeypatch):
    snap = ChainSnapshot(NOW, "BANKNIFTY", "2026-12-29", 45210.0, bank_calls_dead())
    eco = desk_economics(app_cfg, db, monkeypatch, snap)
    assert eco.synthetic is True
    assert eco.notes[0] == NO_TRADEABLE_NOTE
    assert NO_TRADEABLE_NOTE == (
        "no tradeable contract in the live chain (no open interest or no two-sided quote): "
        "the premium is the model's estimate"
    )


def test_t6_no_chain_at_all_gives_no_such_note(app_cfg, db, monkeypatch):
    eco = desk_economics(app_cfg, db, monkeypatch, None)
    assert eco.synthetic is True and NO_TRADEABLE_NOTE not in eco.notes


def test_t6_a_tradeable_chain_is_used_without_the_note(app_cfg, db, monkeypatch):
    rows = [
        *bank_calls_dead(),
        ChainRow(45200.0, "CE", "live", ltp=300.0, bid=299.0, ask=301.0, oi=900.0, delta=0.6),
    ]
    snap = ChainSnapshot(NOW, "BANKNIFTY", "2026-12-29", 45210.0, rows)
    eco = desk_economics(app_cfg, db, monkeypatch, snap)
    assert eco.synthetic is False and eco.option["security_id"] == "live"
    assert NO_TRADEABLE_NOTE not in eco.notes


# ── T7 the delta note ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "delta, note",
    [(-0.61, False), (-0.55, False), (-0.75, False), (-0.52, True), (-0.80, True)],
)
def test_t7_the_delta_note_appears_only_beyond_010(delta, note):
    eco = nifty_put([live_pe(25800.0, delta, "a", 262.0)])
    want = f"nearest tradeable contract: delta {abs(delta):.2f} (wanted about 0.65)"
    assert (want in eco.notes) is note
    assert eco.notes[0].startswith("live chain quote: bid 261.00 / ask 263.00")
    if note:
        assert eco.notes[1] == want


# ── T8 the fill-cost watcher ─────────────────────────────────────────────────────────────
class ChainOnly:
    """expiry_list_raw / option_chain only. Nothing here can trade."""

    def __init__(self, rows):
        self.rows = rows

    def expiry_list_raw(self, security_id, segment="IDX_I"):
        return ["2026-12-29"]

    def option_chain(self, underlying, expiry, security_id=25, segment="IDX_I"):
        return ChainSnapshot(NOW, underlying, expiry, 45210.0, list(self.rows))


def recorder() -> FillCostRecorder:
    return FillCostRecorder(
        create_engine("sqlite://"),
        lambda: None,
        lambda: True,
        lambda now: True,
        lambda: [],
        lambda: [("BANKNIFTY", "25", "NSE", "BANKNIFTY_OPT")],
    )


def test_t8_the_watcher_skips_a_dead_row_nearest_the_target_and_watches_the_live_one():
    rows = [
        ChainRow(44000.0, "CE", "dead", ltp=1300.0, oi=0.0, delta=0.65),
        ChainRow(45000.0, "CE", "live", ltp=420.0, bid=419.0, ask=421.0, oi=900.0, delta=0.60),
        ChainRow(45500.0, "PE", "put", ltp=380.0, bid=379.0, ask=381.0, oi=500.0, delta=-0.66),
    ]
    rec = recorder()
    rec._refresh_options(ChainOnly(rows), NOW)
    assert set(rec._options) == {"live", "put"}


def test_t8_with_nothing_tradeable_the_watcher_watches_nothing_new():
    rows = [
        ChainRow(44000.0, "CE", "dead", ltp=1300.0, oi=0.0, delta=0.65),
        ChainRow(46000.0, "PE", "deadp", ltp=900.0, bid=None, ask=901.0, oi=40.0, delta=-0.6),
    ]
    rec = recorder()
    rec._refresh_options(ChainOnly(rows), NOW)
    assert rec._options == {}


# ── T9 a healthy chain gives exactly the old pick ────────────────────────────────────────
def old_pick(rows, opt, entry, target=0.65):
    """The rule before US-104, as the oracle."""
    rows = [r for r in rows if r.opt_type == opt and r.ltp and r.ltp > 0]
    if not rows:
        return None
    with_delta = [r for r in rows if r.delta is not None]
    if with_delta:
        return min(with_delta, key=lambda r: abs(abs(r.delta) - target))
    itm = [r for r in rows if (r.strike <= entry if opt == "CE" else r.strike >= entry)]
    return min(itm or rows, key=lambda r: abs(r.strike - entry))


def healthy_chain(rng: random.Random, n: int, greeks: str) -> list[ChainRow]:
    rows = []
    for i in range(n):
        opt = rng.choice(["CE", "PE"])
        ltp = round(rng.uniform(5.0, 2000.0), 2)
        half = round(ltp * rng.uniform(0.0, 0.04), 2)
        delta = None
        if greeks == "all" or (greeks == "some" and rng.random() < 0.5):
            delta = round(rng.uniform(0.05, 0.95), 3) * (1 if opt == "CE" else -1)
        rows.append(
            ChainRow(
                float(45000 + 100 * rng.randint(-20, 20)), opt, f"s{i}", ltp=ltp,
                bid=ltp - half if ltp - half > 0 else ltp, ask=ltp + half,
                oi=float(rng.randint(1, 10**6)), delta=delta,
            )
        )  # fmt: skip
    return rows


def test_t9_a_healthy_chain_gives_exactly_the_old_pick():
    rng = random.Random(104)
    checked = 0
    for n, greeks, direction in itertools.product(
        (1, 2, 5, 15), ("all", "some", "none"), ("long", "short")
    ):
        for _ in range(25):
            rows = healthy_chain(rng, n, greeks)
            assert all(tradeable(r, 10.0) for r in rows)
            opt = "CE" if direction == "long" else "PE"
            entry = 45000.0 + rng.uniform(-500.0, 500.0)
            want = old_pick(rows, opt, entry)
            got = pick_contract(rows, opt, entry, 0.65, 10.0)
            assert got is want
            eco = index_option_economics_from_chain(
                SPECS["BANKNIFTY_OPT"], direction, entry, entry - 50.0, 2.0, chain(rows), NOW, BT
            )
            if want is None:
                assert eco is None
            else:
                assert eco.option["security_id"] == want.security_id
                checked += 1
    assert checked > 300


# ── T10 whatever the rows hold, no exception and a pick that passes the test ─────────────
WILD = st.one_of(
    st.none(),
    st.just(0.0),
    st.just(math.nan),
    st.just(math.inf),
    st.just(-math.inf),
    st.floats(min_value=-1e6, max_value=-1e-6),
    st.floats(min_value=1e-6, max_value=1e9),
    st.just(1e300),
)
ROW = st.builds(
    ChainRow,
    strike=st.one_of(st.just(math.nan), st.just(-100.0), st.floats(1.0, 1e6)),
    opt_type=st.sampled_from(["CE", "PE"]),
    security_id=st.one_of(st.none(), st.text("abc", min_size=1, max_size=3)),
    ltp=WILD,
    bid=WILD,
    ask=WILD,
    oi=WILD,
    iv=WILD,
    delta=st.one_of(WILD, st.floats(-1.0, 1.0)),
)


@settings(max_examples=400, deadline=None)
@given(rows=st.lists(ROW, max_size=8), direction=st.sampled_from(["long", "short"]))
def test_t10_wild_rows_never_raise_and_a_pick_is_always_tradeable(rows, direction):
    opt = "CE" if direction == "long" else "PE"
    side = [r for r in rows if r.opt_type == opt]
    need_oi = any(r.oi is not None for r in side)
    got = pick_contract(rows, opt, 45000.0, 0.65, 10.0)
    if got is not None:
        assert got in side and tradeable(got, 10.0, need_oi=need_oi)
    res = chain_economics(
        SPECS["BANKNIFTY_OPT"], direction, 45000.0, 44950.0, 2.0, chain(rows, 45000.0), NOW, BT
    )
    if res.economics is not None:
        o = res.economics.option
        assert got is not None and o["strike"] == float(got.strike)
        assert o["bid"] == got.bid and o["ask"] == got.ask
