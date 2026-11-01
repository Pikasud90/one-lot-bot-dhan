"""US-028: the Settings "Desk bars" row uses the same delayNote() rule as the Desk header.

The row is one line of src/onelot/ui/app.js; it is lifted out with its one-line helpers
(esc, kv, delayNote) and rendered with node. Skipped on a machine without node.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "src" / "onelot" / "ui" / "app.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def desk_bars_row(desk_source: dict | None) -> str:
    js = APP_JS.read_text(encoding="utf-8")
    helpers = []
    for name in ("esc", "kv", "delayNote"):
        m = re.search(rf"^const {name} = .*;$", js, re.M)
        assert m, f"`const {name} = ...;` not found as a single line in {APP_JS}"
        helpers.append(m.group(0))
    row = re.search(r'^\s*kv\("Desk bars", .*\);$', js, re.M)
    assert row, 'the Settings `kv("Desk bars", ...)` line was not found in app.js'
    expr = row.group(0).strip().removesuffix(";")
    script = "\n".join(
        [*helpers, "const s = JSON.parse(process.argv[1]);", f"console.log({expr});"]
    )
    done = subprocess.run(
        ["node", "-e", script, json.dumps({"desk_source": desk_source})],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


class TestSettingsDeskBarsRow:
    def test_names_the_yahoo_served_instruments_escaped_once(self):
        out = desk_bars_row(
            {"name": "dhan+yahoo", "resolution": "1m", "delayed": True, "delayed_keys": ["M&M"]}
        )
        assert "dhan+yahoo 1m · delayed ~15 min: M&amp;M" in out
        assert "&amp;amp;" not in out and "(delayed)" not in out

    def test_live_when_no_instrument_is_delayed(self):
        out = desk_bars_row(
            {"name": "dhan+yahoo", "resolution": "1m", "delayed": False, "delayed_keys": []}
        )
        assert "dhan+yahoo 1m · live" in out and "delayed" not in out

    def test_older_snapshot_without_delayed_keys_still_says_delayed(self):
        out = desk_bars_row({"name": "yahoo", "resolution": "2m", "delayed": True})
        assert "yahoo 2m · delayed ~15 min<" in out
