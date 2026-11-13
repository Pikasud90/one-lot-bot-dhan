"""US-068 on the page: the index card's micro-trend strip and table (app.js helpers in node).

trendVerdict(trend)   "aligned up" / "aligned down" / "not aligned" on the micro ladder
trendReversal(trend)  " · reversed k of 7, exit at 3" while a micro-trend trade is open
trendPips(trend)      the strip: 3m first, marked as the trigger
accRow(row)           a stock's card has no micro-trend strip or table
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
HELPERS = ["esc", "trendRows", "trendCls", "trendWord", "trendMark", "trendVerdict", "trendTitle",
           "trendReversal", "trendPips"]  # fmt: skip

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def js(expression: str, payload: Any = None) -> Any:
    source = APP_JS.read_text(encoding="utf-8")
    lines = []
    for name in HELPERS:
        found = re.search(rf"^const {name} = .*;$", source, re.M)
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


TFS = ["3m", "5m", "10m", "15m", "30m", "60m", "4h"]


def trend(dirs: list[int], reversal: dict | None = None) -> dict:
    rows = [{"timeframe": tf, "direction": d} for tf, d in zip(TFS, dirs, strict=True)]
    rows[0]["trigger"] = True
    out: dict = {"rows": rows, "period": 14, "mult": 1.7}
    if reversal:
        out["reversal"] = reversal
    return out


def test_the_micro_ladder_says_aligned_up_down_or_not():
    assert js("trendVerdict(P)", trend([1] * 7)) == ["up", "aligned up"]
    assert js("trendVerdict(P)", trend([-1] * 7)) == ["down", "aligned down"]
    assert js("trendVerdict(P)", trend([1] * 6 + [-1])) == ["mixed", "not aligned"]
    assert js("trendVerdict(P)", trend([0] * 7)) == ["none", "no trend reading"]


def test_the_strip_marks_the_trigger_and_shows_the_reversal_count():
    html = js("trendPips(P)", trend([1, 1, -1, 1, 1, 1, 1], {"reversed": 1, "of": 7, "exit_at": 3}))
    assert "3m trigger" in html and 'title="the trigger"' in html
    assert "4h" in html and "not aligned · reversed 1 of 7, exit at 3" in html
    assert "reversed" not in js("trendPips(P)", trend([1] * 7))


def test_the_old_ladder_keeps_its_wording():
    rows = [{"timeframe": tf, "direction": 1} for tf in ("1m", "5m", "15m", "30m", "60m", "1d")]
    assert js("trendVerdict(P)", {"rows": rows}) == ["up", "all 6 up"]


def test_a_stock_card_has_no_micro_trend_strip_or_table():
    source = APP_JS.read_text(encoding="utf-8")
    assert '${r.kind === "STOCK" ? "" : trendPips(r.trend)}' in source
    assert source.count('${r.kind === "STOCK" ? "" : trendBlock(r.trend, L.price)}') == 2
