"""US-078: options are traded on four indices; MIDCPNIFTY is a training instrument.

`trade: false` on an index keeps it tracked and shown but a verdict never becomes an alert.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from test_desk import FakeNotifier, FakeSource, OpenClock

from onelot.clock import now_ist
from onelot.desk import Desk, load_desk_config
from onelot.desk.config import DeskConfig, DeskInstrumentCfg, StrategySlotCfg
from onelot.desk.tracker import Verdict
from onelot.paper.view import slot_scoreboard_view
from onelot.risk.governor import RiskGovernor
from onelot.store import init_database
from onelot.store.repo import Repo

APP_JS = Path(__file__).resolve().parents[2] / "src" / "onelot" / "ui" / "app.js"


@pytest.fixture
def db(tmp_path):
    from onelot.data.db import get_database, use_database

    d = init_database(f"sqlite:///{tmp_path / 'desk.db'}")
    use_database(d)
    yield d
    d.dispose()
    get_database.__globals__["_db"] = None


def _desk(cfg, db, *, training: bool):
    slot = (StrategySlotCfg("ema_cross", "10m"),)
    keys = ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "MIDCPNIFTY")
    indices = tuple(
        DeskInstrumentCfg(k, f"{k}_OPT", "INDEX", slot, trade=not (training and k == "MIDCPNIFTY"))
        for k in keys
    )
    desk_cfg = DeskConfig(
        poll_seconds=60, history_days=10, source="yahoo", signal_valid_minutes=10,
        context=(), indices=indices, stocks=(),
    )  # fmt: skip
    repo = Repo(db)
    gov = RiskGovernor(
        cfg.risk, cfg.account.capital_inr, cfg.mode, now_ist().date().isoformat(), repo
    )
    notifier = FakeNotifier()
    desk = Desk(db.engine, cfg, notifier, gov, OpenClock(), repo=repo, desk_cfg=desk_cfg)
    desk.source = FakeSource()
    desk.fine_minutes = 2
    desk.poll()
    return desk, notifier


def _raise_all(desk: Desk) -> None:
    for t in desk.trackers.values():
        close = float(t.bars["close"].iloc[-1])
        v = Verdict(
            "long", "test", t.bars.index[-1].to_pydatetime(), close * 0.99, close, 60.0, [], True
        )
        desk._raise_signal(t, t.slots[0], v, now_ist())


def _rows(db) -> list[str]:
    from sqlalchemy import text

    with db.engine.connect() as c:
        return sorted(r[0] for r in c.execute(text("select instrument_key from desk_signals")))


def test_a_training_index_raises_no_alert_and_the_other_four_do(cfg, db):
    desk, notifier = _desk(cfg, db, training=True)
    _raise_all(desk)
    got = _rows(db)
    assert got == sorted(f"{k}_OPT" for k in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"))
    assert "MIDCPNIFTY_OPT" not in got
    assert all("MIDCPNIFTY" not in n.title + n.body for n in notifier.sent)
    assert "MIDCPNIFTY_OPT" not in {s["instrument_key"] for s in desk.signals.values()}


def test_with_the_flag_off_all_five_alert_as_before(cfg, db):
    desk, _ = _desk(cfg, db, training=False)
    _raise_all(desk)
    assert len(_rows(db)) == 5


def test_the_flag_does_not_pause_a_slot_on_the_scoreboard():
    shipped = load_desk_config()
    assert not any(s.enabled is False for i in shipped.instruments for s in i.strategies)
    assert shipped.paused_slots() == frozenset()
    view = slot_scoreboard_view({}, shipped.primary_slots(), {}, shipped.paused_slots())
    assert [r["paused"] for r in view] and not any(r["paused"] for r in view)


def test_the_snapshot_rows_carry_trade(cfg, db):
    desk, _ = _desk(cfg, db, training=True)
    rows = {r["key"]: r["trade"] for r in desk.snapshot()["indices"]}
    assert rows["MIDCPNIFTY"] is False
    assert all(v is True for k, v in rows.items() if k != "MIDCPNIFTY")


def test_the_flag_defaults_to_true_and_an_unknown_value_is_refused(tmp_path):
    ok = tmp_path / "a.yaml"
    ok.write_text("indices:\n  - {key: NIFTY}\n  - {key: MIDCPNIFTY, trade: false}\n")
    got = {i.key: i.trade for i in load_desk_config(ok).indices}
    assert got == {"NIFTY": True, "MIDCPNIFTY": False}
    bad = tmp_path / "b.yaml"
    bad.write_text('indices:\n  - {key: NIFTY, trade: "no"}\n')
    with pytest.raises(ValueError, match="trade must be true or false"):
        load_desk_config(bad)


def test_the_shipped_config_trains_on_midcpnifty_only():
    got = {i.key: i.trade for i in load_desk_config().indices}
    assert got == {"BANKNIFTY": True, "NIFTY": True, "FINNIFTY": True,
                   "MIDCPNIFTY": False, "SENSEX": True}  # fmt: skip


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_card_label_appears_only_for_a_training_instrument():
    source = APP_JS.read_text(encoding="utf-8")
    lines = [re.search(rf"^const {n} = .*;$", source, re.M).group(0) for n in ("esc", "trainTag")]
    probe = "console.log(JSON.stringify([{trade:false},{trade:true},{},{key:'X'}].map(trainTag)))"
    done = subprocess.run(
        ["node", "-e", "\n".join([*lines, probe])], capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert "Training only: no alerts" in out[0] and out[1:] == ["", "", ""]
