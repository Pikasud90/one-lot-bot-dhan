"""US-105 through the REAL Desk and the HTTP API: a trade on a contract that was not tradeable
is tagged on its alert row, left out of the scoreboards, and counted in a left-out figure.

The rig is the OF-5 one. The paper trade is a stock trade the rig opens and stops out; the
void mark is written the way the book writes it, and the alert's facts are set to the
recorded ones, because the rig raises no index option alert.
"""

# ruff: noqa: F811

from __future__ import annotations

import datetime as dt
import json

from sqlalchemy import select
from test_of5_alert_lifecycle import (  # noqa: F401  (fixtures)
    _offline,
    api,
    app_cfg,
    desk_db,
    rig,
    through_the_stop,
)

from onelot.desk.audit import scoreboard_left_out, strategy_scoreboard
from onelot.store.schema import paper_trades, paper_void
from onelot.void import LEFT_OUT_NOTE, VOID_REASON


def void_facts(alert: dict) -> dict:
    """The alert as the desk stored it for the MIDCPNIFTY put of 23 Nov (open interest 0)."""
    alert = json.loads(json.dumps(alert))
    alert["kind"] = "INDEX"
    alert["economics"] = {
        **alert.get("economics", {}),
        "synthetic": False,
        "option": {"bid": 1313.45, "ask": 1678.5, "oi": 0.0, "iv": 0.625, "delta": 0.643},
    }
    return alert


def closed_alert(rig_, strategy: str, oid: str, pnl: float, void: bool) -> dict:
    s = {
        "id": oid,
        "strategy": strategy,
        "kind": "STOCK",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": "EXPIRED",
        "outcome": {"outcome": "STOP" if pnl < 0 else "TARGET", "r_now": pnl / 100, "pnl_est": pnl},
    }
    return void_facts(s) if void else s


class TestAuditScoreboard:
    def test_void_alerts_are_left_out_and_counted(self):
        sigs = [
            closed_alert(None, "a", "1", 100.0, void=False),
            closed_alert(None, "a", "2", -50.0, void=False),
            closed_alert(None, "a", "3", -400.0, void=True),
            closed_alert(None, "b", "4", 30.0, void=True),
        ]
        board = strategy_scoreboard(sigs)
        assert board["a"]["n"] == 2 and board["a"]["pnl_sum"] == 50.0 and "b" not in board
        assert scoreboard_left_out(sigs) == {"trades": 2, "net": -370.0, "reason": LEFT_OUT_NOTE}

    def test_with_nothing_void_the_figures_are_as_before(self):
        sigs = [closed_alert(None, "a", "1", 100.0, void=False)]
        assert scoreboard_left_out(sigs) is None
        assert strategy_scoreboard(sigs) == strategy_scoreboard(sigs, counted_only=False)

    def test_an_open_outcome_of_a_void_alert_is_not_counted_as_left_out(self):
        s = closed_alert(None, "a", "1", 100.0, void=True)
        s["outcome"] = {"outcome": "OPEN", "r_now": 0.2}
        assert scoreboard_left_out([s]) is None

    def test_the_unfiltered_record_is_kept_for_the_confidence_score(self):
        sigs = [closed_alert(None, "a", str(i), 10.0, void=True) for i in range(6)]
        assert strategy_scoreboard(sigs) == {}
        assert strategy_scoreboard(sigs, counted_only=False)["a"]["n"] == 6


class TestTheDeskAndTheApi:
    def test_the_alert_row_is_tagged_and_the_stored_alert_is_untouched(self, rig):
        alert = rig.alert("SBIN")
        through_the_stop(rig, alert)
        rig.poll()
        kept = next(s for s in rig.desk.archive if s["id"] == alert["id"])
        kept.update(void_facts(kept))
        before = json.dumps(kept, sort_keys=True, default=str)
        body = api(rig.desk).get("/api/desk/signals").json()
        row = next(s for s in body["signals"] if s["id"] == alert["id"])
        assert row["void_reason"] == VOID_REASON
        assert row["exit"]["reason"] == "STOP"  # the recorded exit is still on the row
        assert json.dumps(kept, sort_keys=True, default=str) == before  # nothing stored changed
        assert "void_reason" not in kept

    def test_a_sound_alert_row_has_no_tag_and_no_left_out_key_appears(self, rig):
        alert = rig.alert("SBIN")
        through_the_stop(rig, alert)
        rig.poll()
        body = api(rig.desk).get("/api/desk/signals").json()
        row = next(s for s in body["signals"] if s["id"] == alert["id"])
        assert "void_reason" not in row
        assert "paper_left_out" not in body and "scoreboard_left_out" not in body

    def test_the_marked_trade_is_left_out_of_the_scoreboard_and_counted_apart(self, rig):
        alert = rig.alert("SBIN")
        through_the_stop(rig, alert)
        rig.poll()
        before = api(rig.desk).get("/api/desk/signals").json()
        mine = lambda b: next(  # noqa: E731
            r for r in b["paper_scoreboard"] if r["strategy"] == alert["strategy"] and r["trades"]
        )
        counted = mine(before)
        assert "left_out" not in counted
        with rig.engine.begin() as conn:
            trade = conn.execute(select(paper_trades.c.id, paper_trades.c.net_paise)).one()
            conn.execute(
                paper_void.insert().values(
                    trade_id=trade.id,
                    alert_id=alert["id"],
                    reason=VOID_REASON,
                    marked_at=dt.datetime.now(dt.timezone.utc),
                )
            )
        after = api(rig.desk).get("/api/desk/signals").json()
        rows = [r for r in after["paper_scoreboard"] if r["strategy"] == alert["strategy"]]
        assert all(r["trades"] == 0 for r in rows)
        assert after["paper_left_out"]["trades"] == 1
        assert after["paper_left_out"]["net"] == counted["net"]
        assert after["paper_left_out"]["reason"] == LEFT_OUT_NOTE

    def test_the_paper_api_still_lists_the_trade_with_its_net_and_a_tag(self, rig):
        alert = rig.alert("SBIN")
        through_the_stop(rig, alert)
        rig.poll()
        with rig.engine.begin() as conn:
            row = conn.execute(select(paper_trades.c.id, paper_trades.c.plan)).one()
            plan = {**row.plan, "alert": void_facts(row.plan["alert"])}
            conn.execute(paper_trades.update().values(plan=plan))
        body = api(rig.desk).get("/api/paper").json()
        (t,) = [t for t in body["trades"] if t["alert_id"] == alert["id"]]
        assert t["status"] == "CLOSED" and t["net"] is not None  # listed, as recorded
        assert t["void_reason"] == VOID_REASON
