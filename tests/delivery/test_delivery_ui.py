"""US-067 D7, D11, D14, D15 and ruling 5 on the page: the pure render helpers in app.js.

    pbBlock(card)       the stock card's pullback reading, evidence line and history date
    dlvHome(view)       Home: pending delivery alerts and open delivery trades with D7's fields
    dlvArchive(list)    the archive's delivery alerts

The one-line `const name = ...;` helpers are lifted out of app.js and run in node, as the
paper panel's tests do. Skipped without node.
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
HELPERS = ["fmt", "rup", "rup2", "cls", "esc", "kv", "spct", "pbTick", "pbBlock", "dlvRow",
           "dlvAlert", "dlvHome", "dlvArchive", "pbChip", "pbUnit"]  # fmt: skip

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


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


CARD = {
    "evidence": "Backtest: x. Not proven: this is the paper test.",
    "history_to": "2026-11-13",
    "stored_to": "2026-11-06",
    "note": None,
    "session": "2026-11-13",
    "reading": {"signal": False, "below_high_pct": 5.25, "needed_pct": 8.0, "deep": False,
                "above_sma200": True, "nifty_above_sma200": False, "close": 1017.5,
                "sma200": 900.0, "why": "NIFTY not above its SMA200"},
    "trade": None,
}  # fmt: skip


def test_the_card_shows_the_distance_the_two_tests_the_evidence_and_the_history_date():
    html = js("pbBlock(P)", CARD)
    assert "5.25% of 8% needed" in html
    assert "Close above SMA200" in html and "NIFTY above its SMA200" in html
    assert "NIFTY not above its SMA200" in html
    assert "Not proven: this is the paper test." in html  # D14
    assert "daily history complete to 2026-11-13" in html and "store to 2026-11-06" in html


def test_the_card_shows_why_there_is_no_reading_and_escapes_it():
    html = js("pbBlock(P)", {**CARD, "reading": None, "note": "<b>short</b> history"})
    assert "&lt;b&gt;short&lt;/b&gt; history" in html and "Session" not in html
    assert js("pbBlock(P)", None) == ""


TRADE = {"series_key": "SBIN", "label": "State Bank of India", "qty": 10, "status": "OPEN",
         "signal_date": "2026-11-13", "entry": 1000.3, "last": 1010.0, "unrealised": 97.0,
         "unrealised_pct": 0.97, "sessions": 3, "max_sessions": 30, "sma_exit": 1050.12,
         "stop": 850.26, "warning": "possible corporate action on 2026-11-17: check the figures"}  # fmt: skip


def test_home_lists_open_trades_with_the_d7_fields_and_the_warning():
    html = js(
        "dlvHome(P)",
        {"pending": [{**TRADE, "status": "PENDING_ENTRY", "alert": "BUY 10 shares"}],
         "open": [TRADE], "history_to": "2026-11-13", "stale": ["ITC"]},
    )  # fmt: skip
    for text in ("3 of 30", "1050.12", "850.26", "1010.00", "+0.97%", "₹97.00",
                 "possible corporate action on 2026-11-17", "BUY 10 State Bank of India",
                 "daily history complete to 2026-11-13", "not fresh: ITC"):  # fmt: skip
        assert text in html, text


def test_home_without_a_delivery_desk_shows_nothing():
    assert js("dlvHome(P)", None) == ""
    assert "No open delivery trade." in js("dlvHome(P)", {"pending": [], "open": []})


def test_the_archive_lists_delivery_alerts_with_their_result():
    rows = [{**TRADE, "status": "CLOSED", "exit": 1100.0, "exit_late": True, "net": 950.5,
             "net_pct": 9.5, "exit_reason": "SMA20"},
            {**TRADE, "status": "MISSED", "note": "missed: the desk was not running"}]  # fmt: skip
    html = js("dlvArchive(P)", rows)
    assert "closed" in html and "1100.00 (late)" in html and "₹950.50" in html
    assert "missed: the desk was not running" in html
    assert js("dlvArchive(P)", []) == ""


# ── round 3: the collapsed stock row and the unit wording ──────────────────────────────────


def test_the_row_chip_shows_the_distance_against_what_is_needed():
    html = js("pbChip(P)", CARD)
    assert "pullback 1d · 5.3% of 8%" in html and 'class="schip "' in html


def test_the_row_chip_is_highlighted_when_the_signal_is_on():
    card = {**CARD, "reading": {**CARD["reading"], "signal": True}}
    assert 'class="schip long"' in js("pbChip(P)", card)
    assert "BUY at the next open" in js("pbChip(P)", card)
    pending = {**CARD, "trade": {"status": "PENDING_ENTRY"}}
    assert "BUY at the next open" in js("pbChip(P)", pending)


def test_the_row_chip_shows_an_open_trade_and_a_pending_exit():
    open_ = {**CARD, "trade": {"status": "OPEN", "sessions": 3, "max_sessions": 30}}
    assert "pullback 1d · open 3 of 30" in js("pbChip(P)", open_)
    leaving = {**CARD, "trade": {"status": "EXIT_PENDING", "exit_reason": "STOP"}}
    assert "exit at the next open" in js("pbChip(P)", leaving)


def test_the_row_chip_says_no_reading_with_the_note_as_its_title():
    html = js("pbChip(P)", {**CARD, "reading": None, "note": "only 150 daily bars, 201 needed"})
    assert "pullback 1d · no reading" in html
    assert 'title="only 150 daily bars, 201 needed"' in html
    assert js("pbChip(P)", None) == ""


def test_the_unit_line_names_the_delivery_amount_from_the_settings():
    assert js("pbUnit(P)", {**CARD, "unit_inr": 10000}) == "delivery: shares nearest ₹10,000"
    assert js("pbUnit(P)", {**CARD, "unit_inr": 25000}) == "delivery: shares nearest ₹25,000"


def test_paused_slots_show_dimmed_and_the_pullback_chip_comes_first():
    source = APP_JS.read_text(encoding="utf-8")
    line = next(
        x
        for x in source.splitlines()
        if x.strip().startswith("const chips = (r.pullback ? pbChip(")
    )
    assert "s.enabled === false" in line and 'class="schip paused" title="paused"' in line
    css = (APP_JS.parent / "styles.css").read_text(encoding="utf-8")
    assert ".schip.paused{opacity:.5}" in css
