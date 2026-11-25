"""US-105 on the page: the whole app.js runs in node against the real desk's alert rows.

A void alert stays in the archive with its recorded exit and net and a tag on the row; the
scoreboards print one line saying how many trades they left out; with nothing left out the
page is exactly what it was.
"""

# ruff: noqa: F811

from __future__ import annotations

import copy
import re

from test_of_qa_ui_runtime import (  # noqa: F401  (fixtures and the harness)
    _offline,
    app_cfg,
    desk_db,
    real_archive,
    rig,
    run_ui,
    signals_payload,
    step,
)

REASON = "contract not tradeable: no open interest when the alert was raised"
TAG = "not counted: the contract had no open interest"
LINE = "Left out of the score: 3 trades on contracts that were not tradeable (no open interest)"
LEFT = {
    "trades": 3,
    "net": "-183.86",
    "reason": "contracts that were not tradeable (no open interest)",
}

PAPER_ROW = {
    "strategy": "ema_cross", "timeframe": "5m", "kind": "INDEX", "primary": True, "paused": False,
    "trades": 4, "wins": 2, "losses": 2, "no_fill": 0, "open": 0, "win_rate": 50.0,
    "net": "12.00", "costs": "1.00", "avg_net": "3.00", "profit_factor": 1.2, "avg_r": 0.1,
    "last_exit": None,
}  # fmt: skip


def scenario(rows, extra=None, board=None, steps=()):
    payload = {**signals_payload(rows), "paper_scoreboard": board or [PAPER_ROW], **(extra or {})}
    return {
        "get": {"/api/desk/signals": payload, "/api/desk/journal": {"trades": []}},
        "steps": list(steps),
    }


READ = [
    {"op": "read", "id": "home-archive"},
    {"op": "read", "id": "home-scoreboard"},
]


def tagged(html: str) -> list[str]:
    """The data-aid of every archive row that carries the tag in its head."""
    rows = re.split(r'(?=<div class="alert-row)', html)
    return [m.group(1) for r in rows if TAG in r and (m := re.search(r'data-aid="([^"]+)"', r))]


class TestTheArchiveRow:
    def test_the_void_row_is_tagged_and_keeps_its_exit_and_net(self, rig, tmp_path):
        rows = real_archive(rig)
        sound = copy.deepcopy(rows)
        void_id = next(r["id"] for r in rows if r.get("exit"))
        for r in rows:
            if r["id"] == void_id:
                r["void_reason"] = REASON
        res = run_ui(tmp_path, scenario(rows, steps=READ))
        assert res["errors"] == []
        html = step(res, 0)["html"]
        assert tagged(html) == [void_id]
        row = next(p for p in re.split(r'(?=<div class="alert-row)', html) if void_id in p)
        exit_ = next(r["exit"] for r in rows if r["id"] == void_id)
        assert f"EXIT {exit_['reason'].replace('_', ' ')}" in row  # the recorded exit, as before
        # the sound page equals the page without any tag, apart from the tag itself
        plain = run_ui(tmp_path, scenario(sound, steps=READ))
        assert TAG not in step(plain, 0)["html"]
        stripped = re.sub(r' <span class="void-tag"[^>]*>[^<]*</span>', "", html)
        assert stripped == step(plain, 0)["html"]


class TestTheLeftOutLine:
    def test_the_paper_scoreboard_prints_the_line_once(self, rig, tmp_path):
        res = run_ui(tmp_path, scenario(real_archive(rig), {"paper_left_out": LEFT}, steps=READ))
        assert res["errors"] == []
        board = step(res, 1)["html"]
        assert board.count(LINE) == 1

    def test_with_nothing_left_out_the_scoreboard_is_exactly_as_before(self, rig, tmp_path):
        rows = real_archive(rig)
        with_line = step(run_ui(tmp_path, scenario(rows, {"paper_left_out": LEFT}, steps=READ)), 1)
        without = step(run_ui(tmp_path, scenario(rows, steps=READ)), 1)["html"]
        assert "Left out" not in without
        assert with_line["html"] == without + f'<div class="warnbar void-note mt">{LINE}</div>'
        zero = step(
            run_ui(tmp_path, scenario(rows, {"paper_left_out": {"trades": 0}}, steps=READ)), 1
        )
        assert zero["html"] == without

    def test_the_audit_scoreboard_prints_it_when_there_is_no_paper_book(self, rig, tmp_path):
        spec = scenario(real_archive(rig), {"scoreboard_left_out": LEFT}, steps=READ)
        spec["get"]["/api/desk/signals"]["paper_scoreboard"] = None
        res = run_ui(tmp_path, spec)
        assert res["errors"] == [] and step(res, 1)["html"].count(LINE) == 1
