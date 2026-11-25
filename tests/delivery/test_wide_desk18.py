"""US-099 acceptance 1 and 2: the desk 18 are the owner's live record.

1. With `wide: false` nothing changes: no wide desk, no wide key in the view, no wide table,
   the step does nothing.
2. With `wide: true` the desk 18 raise and manage exactly the same trades: on the reference
   fixtures (`tests/fixtures/pullback`, 247 trades), (a) the wide list's own round reproduces
   every one of the 247 by entry date, exit date and open-at-the-end, so it evaluates the rule
   with the same code; (b) the desk 18's product path over a window of sessions books the
   same `delivery_trades` rows, column for column, with the wide list off, on, and on with a
   cap of 0 while delivery-only stocks signal beside them, and those rows are the reference's.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from sqlalchemy import create_engine, inspect, select

import onelot.delivery.data as data
from onelot.clock import IST, SessionClock, TradingCalendar
from onelot.config import CostConfig, SessionCfg
from onelot.data.store import save_bars
from onelot.delivery.money import delivery_model, depository_paise
from onelot.delivery.rule import PullbackParams
from onelot.delivery.runner import DeliveryDesk
from onelot.delivery.universe import UniverseStock
from onelot.delivery.wide import StockDays, WideDesk, replay_rounds
from onelot.desk.config import WideCfg, load_desk_config
from onelot.store.schema import METADATA, delivery_trades

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "pullback"
COSTS = CostConfig.load(ROOT / "config" / "costs.yaml")
CAL = TradingCalendar.from_yaml(ROOT / "config" / "nse_holidays.yaml")


def fixture(key: str) -> pd.DataFrame:
    b = pd.read_csv(FIX / f"daily_{key}.csv.gz")
    idx = [
        dt.datetime.combine(dt.date.fromisoformat(d), dt.time(9, 15), tzinfo=IST)
        for d in b.pop("date")
    ]
    return b.set_index(pd.DatetimeIndex(idx))


def desk_keys() -> list[str]:
    return [i.key for i, _ in load_desk_config().delivery_slots()]


class Quiet:
    def __init__(self) -> None:
        self.sent: list = []

    def send(self, n, dedup_key=None):  # noqa: ANN001, ANN201
        self.sent.append(n)
        return {}


# ── 1. wide: false ───────────────────────────────────────────────────────────
def test_with_wide_false_the_delivery_desk_is_as_before():
    cfg = load_desk_config()
    assert cfg.wide.on  # shipped on
    off = dataclasses.replace(cfg, wide=dataclasses.replace(cfg.wide, on=False))
    eng = create_engine("sqlite://")
    d = DeliveryDesk(
        eng, off, COSTS, SessionCfg(), CAL, Quiet(), dt.datetime(2026, 1, 1, tzinfo=IST),
        background=False,
    )  # fmt: skip
    assert d.wide is None and "wide" not in d.view()
    d.wide_step(dt.datetime(2026, 11, 20, 18, 0, tzinfo=IST))
    assert not {"wide_trades", "wide_state", "wide_rounds"} & set(inspect(eng).get_table_names())


def test_the_slot_keys_are_read_strictly(tmp_path):
    def read(body: str):
        p = tmp_path / "desk.yaml"
        p.write_text(body)
        return load_desk_config(p).wide

    slot = "      - {name: pullback, timeframe: 1d, product: delivery, %s}\n"
    head = "defaults:\n  stock_strategies:\n"
    assert read(head + slot % "params: {}") == WideCfg()  # no key: off
    got = read(head + slot % "wide: true, cap_open: 4, evening_round: false, universe: u.yaml")
    assert got == WideCfg(True, 4, False, "u.yaml")
    for bad in (
        'wide: "true"',
        "cap_open: -1",
        "cap_open: 2.5",
        "evening_round: 1",
        "universe: ''",
    ):
        with pytest.raises(ValueError):
            read(head + slot % bad)


# ── 2a. the wide round's code on the 247 reference trades ────────────────────
def test_the_wide_round_makes_the_247_reference_trades():
    keys = desk_keys()
    days = {k: StockDays.of(k, fixture(k)) for k in keys}
    n = pd.read_csv(FIX / "daily_NIFTY.csv.gz")
    w = WideDesk(
        create_engine("sqlite://"), [UniverseStock(k, k, k, "0", "NSE_EQ", "desk") for k in keys],
        PullbackParams(), delivery_model(COSTS), depository_paise(COSTS), CAL,
        SessionClock(SessionCfg(), CAL), background=False,
    )  # fmt: skip
    sessions = sorted({d for sd in days.values() for d in sd.dates})
    nd = [dt.date.fromisoformat(x) for x in n["date"]]
    replay_rounds(w, days, nd, n["close"].to_numpy(float), sessions)
    got = {(t.series_key, t.signal_date.isoformat()): t for t in w.book.all()}
    ref = pd.read_csv(FIX / "expected_trades.csv")
    assert len(ref) == 247 and set(got) == set(zip(ref.stock, ref.signal_date, strict=True))
    for r in ref.itertuples():
        t = got[(r.stock, r.signal_date)]
        assert t.entry_session.isoformat() == r.entry_date
        if r.open_at_end:
            assert t.status in ("OPEN", "EXIT_PENDING")
        else:
            assert t.exit_session.isoformat() == r.exit_date
            assert abs(t.exit_paise / 100 / 0.9997 - r.exit_price) < 0.01


# ── 2b. the desk 18's own path, wide off and on ─────────────────────────────
WINDOW = ("2024-08-01", "2024-10-31")  # about 65 sessions and 18 reference trades
OTHERS = {"ADANIENT": "SBIN", "ACC": "ITC", "BEL": "LT"}  # delivery-only keys, fixture bars


def desk18_run(wide: WideCfg | None, monkeypatch) -> tuple[list[dict], int, int]:
    """The parity driver of `scripts/pullback_parity_full.py` over WINDOW, with the wide list's
    step each evening. Returns the desk 18's rows, the wide trades, the wide alerts."""
    monkeypatch.setattr(data, "clean_intraday", lambda key, frame: frame)  # cleaned already
    cfg = load_desk_config()
    cfg = dataclasses.replace(cfg, wide=wide or dataclasses.replace(cfg.wide, on=False))
    keys = desk_keys() + ["NIFTY"]
    daily = {k: fixture(k) for k in keys}
    dates = {k: np.array(pd.DatetimeIndex(daily[k].index).date) for k in keys}
    rows = {k: dict(zip(dates[k], zip(daily[k]["open"], daily[k]["close"], strict=True), strict=True)) for k in keys}  # fmt: skip
    window = [d for d in dates["NIFTY"] if WINDOW[0] <= d.isoformat() <= WINDOW[1]]
    eng = create_engine("sqlite://")
    METADATA.create_all(eng)
    for other, src in OTHERS.items():  # delivery-only stocks with stored daily bars
        f = fixture(src)
        save_bars(eng, other, "1d", f[pd.DatetimeIndex(f.index).date <= window[-1]], "fixture")
    note = Quiet()
    desk = DeliveryDesk(
        eng, cfg, COSTS, SessionCfg(), TradingCalendar({}, verified=True), note,
        dt.datetime(2000, 1, 1, tzinfo=IST), background=False,
    )  # fmt: skip
    assert (desk.wide is not None) == bool(wide and wide.on)

    def fine(k: str, day: dt.date) -> pd.DataFrame | None:
        if day not in rows[k]:
            return None
        o, c = rows[k][day]
        last = dt.time(15, 29) if k == "NIFTY" else dt.time(15, 14)
        first = dt.datetime.combine(day, dt.time(9, 15), tzinfo=IST)
        idx = pd.DatetimeIndex([first, dt.datetime.combine(day, last, tzinfo=IST)])
        return pd.DataFrame(
            {"open": [o, o], "high": max(o, c), "low": min(o, c), "close": [o, c], "volume": 1.0},
            index=idx,
        )

    for i, day in enumerate(window):
        prev = window[i - 1] if i else None
        for k in keys:
            desk._stored[k] = (daily[k].iloc[: int(np.searchsorted(dates[k], day))], {})
        desk._history_day = day

        def bars(key: str, day: dt.date = day, prev: dt.date | None = prev) -> pd.DataFrame:
            parts = [
                p for p in (fine(key, d) for d in (prev, day) if d is not None) if p is not None
            ]
            return pd.concat(parts) if parts else pd.DataFrame()

        now = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(0, 30), tzinfo=IST)
        desk.on_poll(now, bars, lambda key: None)
        desk.wide_step(now)  # what Desk._delivery_history does each poll
    with eng.connect() as conn:
        got = [
            dict(r)
            for r in conn.execute(select(delivery_trades).order_by(delivery_trades.c.id)).mappings()
        ]
    n_wide = len(desk.wide.book.all()) if desk.wide is not None else 0
    told = sum(1 for n in note.sent if (n.payload or {}).get("wide"))
    return got, n_wide, told


_OFF: list = []  # the run with the wide list off, made once for both cases


@pytest.mark.parametrize("cap", [10, 0])
def test_the_desk_18_book_the_same_trades_with_the_wide_list_on(monkeypatch, cap):
    if not _OFF:
        _OFF.append(desk18_run(None, monkeypatch))
    off, n0, _ = _OFF[0]
    on, n1, told = desk18_run(
        WideCfg(True, cap, True, "config/delivery_universe.yaml"), monkeypatch
    )
    assert n0 == 0 and n1 > 0  # the wide list did trade beside them
    assert told == (0 if cap == 0 else told) and (cap == 0) == (told == 0)
    assert on == off and len(on) > 10
    ref = pd.read_csv(FIX / "expected_trades.csv")
    first, last = WINDOW
    got = {(r["series_key"], r["signal_date"]): r for r in on}
    # the reference's trades whose whole life is inside the window, the first session aside
    inside = ref[(ref.signal_date > first) & (ref.exit_date <= last) & (ref.open_at_end == 0)]
    for r in inside.itertuples():
        row = got[(r.stock, r.signal_date)]
        assert row["entry_session"] == r.entry_date and row["status"] == "CLOSED"
        assert row["exit_ts"].astimezone(IST).date().isoformat() == r.exit_date


# ── every new step is guarded ───────────────────────────────────────────────
def test_a_failing_wide_list_never_stops_the_poll_or_the_desk_18(cfg, tmp_path):
    import sys
    from types import SimpleNamespace

    sys.path.insert(0, str(ROOT / "tests" / "desk"))
    from desk_harness import new_desk

    from onelot.store import init_database

    desk, _, _ = new_desk(cfg, init_database(f"sqlite:///{tmp_path / 'd.db'}"))
    calls: list[str] = []

    def boom(now):  # noqa: ANN001
        raise RuntimeError("wide list broken")

    desk.delivery = SimpleNamespace(
        load_history=lambda now: calls.append("history"),
        maybe_top_up=lambda now: calls.append("top-up"),
        wide_step=boom,
    )
    before = desk.hook_errors
    desk._delivery_history(dt.datetime(2026, 11, 20, 18, 0, tzinfo=IST))
    assert calls == ["history", "top-up"] and desk.hook_errors == before + 1

    # inside the delivery desk: a round that cannot start and alerts that cannot be sent
    on = DeliveryDesk(
        create_engine("sqlite://"), load_desk_config(), COSTS, SessionCfg(), CAL, Quiet(),
        dt.datetime(2026, 1, 1, tzinfo=IST), background=False,
    )  # fmt: skip
    told: list[str] = []
    on.wide.maybe_round = boom
    on.wide.tell = lambda now: told.append("tell")
    on.wide_step(dt.datetime(2026, 11, 20, 18, 0, tzinfo=IST))
    assert told == ["tell"]
    on.wide.summary = lambda: boom(None)
    assert on.view()["wide"]["note"] == "the wide list could not be read"
