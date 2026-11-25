"""US-097 point 1: Home's one plain line about the desk's uptime, a row of the Today strip.

The helpers are lifted out of app.js and run in node, as the Today strip's own tests do. The
line appears only when a session of the last ten was missed or covered under 90%.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "src" / "onelot" / "ui" / "app.js"
HELPERS = ["esc", "hhmm", "hms", "delayNote", "trendRows", "trendVerdict", "tokenHoursLeft",
           "todayLines", "todayStrip", "UPTIME_MONTHS", "uptimeDates", "uptimeLine"]  # fmt: skip

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def js(expression: str, payload: Any = None) -> Any:
    source = APP_JS.read_text(encoding="utf-8")
    lines = []
    for name in HELPERS:
        found = re.search(rf"^const {name} = .*?;$", source, re.M | re.S)
        assert found, name
        lines.append(found.group(0))
    lines += [
        "const P = JSON.parse(process.argv[1]);",
        f"console.log(JSON.stringify({expression}));",
    ]
    done = subprocess.run(
        ["node", "-e", "\n".join(lines), json.dumps(payload)],
        capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def day(ymd: str, share: float, known: bool = True, finished: bool = True) -> dict:
    return {"day": ymd, "share": share, "known": known, "finished": finished}


FULL = [day(f"2026-11-{d:02d}", 1.0) for d in (3, 4, 5, 6, 9, 11, 12, 13, 16, 17, 18, 19, 20, 23)]


def with_days(change: dict[str, float]) -> dict:
    return {"days": [dict(r, share=change.get(r["day"], r["share"])) for r in FULL]}


@needs_node
def test_no_line_when_every_one_of_the_last_ten_sessions_was_covered() -> None:
    assert js("uptimeLine(P)", {"days": FULL}) == ""
    assert js("uptimeLine(P)", with_days({"2026-11-23": 0.9})) == ""
    assert js("uptimeLine(P)", None) == "" and js("uptimeLine(P)", {"days": []}) == ""


@needs_node
def test_the_week_the_desk_was_down() -> None:
    body = with_days({f"2026-11-{d}": 0.0 for d in (16, 17, 18, 19, 20)})
    assert js("uptimeLine(P)", body) == (
        "The desk did not run on 5 of the last 10 sessions: 16, 17, 18, 19, 20 Nov."
    )


@needs_node
def test_a_missed_day_older_than_ten_sessions_is_not_named() -> None:
    assert js("uptimeLine(P)", with_days({"2026-11-03": 0.0, "2026-11-04": 0.0})) == ""


@needs_node
def test_a_short_session_and_both_kinds_in_one_line() -> None:
    assert js("uptimeLine(P)", with_days({"2026-11-23": 0.71})) == (
        "The desk covered less than 90% of 1 of the last 10 sessions: 23 Nov."
    )
    both = with_days({"2026-11-19": 0.0, "2026-11-20": 0.0, "2026-11-23": 0.71})
    assert js("uptimeLine(P)", both) == (
        "The desk did not run on 2 of the last 10 sessions: 19, 20 Nov; it covered less than 90% "
        "of 23 Nov."
    )


@needs_node
def test_today_in_progress_and_days_before_the_record_began_are_left_out() -> None:
    body = {
        "days": [
            day("2026-10-29", 0.0, known=False),
            day("2026-10-30", 0.0),
            day("2026-11-02", 1.0),
            day("2026-11-03", 0.2, finished=False),
        ]
    }
    assert js("uptimeLine(P)", body) == "The desk did not run on 1 of the last 2 sessions: 30 Oct."


@needs_node
def test_dates_across_two_months() -> None:
    assert js("uptimeDates(P)", ["2026-10-28", "2026-10-30", "2026-11-03"]) == "28, 30 Oct, 3 Nov"


@needs_node
def test_the_line_is_a_row_of_the_today_strip_and_absent_without_it() -> None:
    snap = {"session": {"trading_day": True, "open": True}, "source": {"name": "dhan"}}
    line = "The desk did not run on 1 of the last 10 sessions: 20 Nov."
    rows = js(f"todayLines(P, null, new Date(), {json.dumps(line)})", snap)
    assert ["Desk uptime", line, "warn"] in rows
    assert rows[-1][0] == "Next"
    plain = js("todayLines(P, null, new Date(), '')", snap)
    assert all(r[0] != "Desk uptime" for r in plain)
    down = js(f"todayLines(null, null, new Date(), {json.dumps(line)})")
    assert down[1] == ["Desk uptime", line, "warn"]
