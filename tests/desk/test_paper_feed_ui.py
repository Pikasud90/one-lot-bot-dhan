"""US-030 UI: when the Paper panel fetches GET /api/paper (developer tests).

    wantPaper(force)  asks for a fetch: on a desk snapshot version change (force false) or on
                      opening the Live Desk tab (force true)
    loadPaper()       one fetch; on success the panel and its day line, on failure a note

The rules: never two requests in flight, fetch starts at least 5 s apart, and once the
version stops moving (and the server answers) the last fetch saw the latest version. On a
failure the panel never stays on "Loading", and the day line turns into a warning.

No browser: `paperFeed`, `liveTabActive`, `wantPaper` and `loadPaper` are lifted out of
app.js and run in node over a fake clock, fake timers and a fake fetch. Skipped without node.
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
ONE_LINERS = ["esc", "paperFeed", "liveTabActive"]
FUNCTIONS = ["wantPaper", "loadPaper"]

# A fake browser: clock, timers, fetch, two DOM nodes and a Live Desk tab that can be left.
HARNESS = r"""
const T0 = 1798000000000;  // a real epoch: paperFeed.at starts at 0
let clock = T0, nextId = 1, lastDeskVersion = -1, active = true;
const timers = [], pending = [], fetches = [];
let inFlight = 0, maxInFlight = 0;
globalThis.setTimeout = (fn, ms) => { const id = nextId++; timers.push({ at: clock + Math.max(0, ms), fn, id }); return id; };
Date.now = () => clock;
globalThis.fetch = (url) => { fetches.push({ url, at: clock - T0, version: lastDeskVersion }); inFlight++; maxInFlight = Math.max(maxInFlight, inFlight);
  return new Promise((resolve) => pending.push(resolve)); };
const els = { "desk-paper": { innerHTML: "<div class=\"muted\">Loading paper trades…</div>" }, "desk-paper-meta": { textContent: "", className: "muted" } };
const $ = (id) => els[id];
globalThis.document = { querySelector: () => ({ classList: { contains: () => active } }) };
const paperPanel = (p) => `PANEL ${JSON.stringify(p)}`;
const paperMeta = () => "META";
const OK = { enabled: true };  // the smallest reply the panel accepts
const flush = () => new Promise((r) => setImmediate(r));
async function advance(ms) {
  const end = clock + ms;
  for (;;) {
    timers.sort((a, b) => a.at - b.at || a.id - b.id);
    if (!timers.length || timers[0].at > end) break;
    const t = timers.shift(); clock = t.at; t.fn(); await flush();
  }
  clock = end; await flush();
}
async function reply(status, body) {
  const resolve = pending.shift(); if (!resolve) throw new Error("no fetch to answer");
  inFlight--; resolve({ ok: status < 400, status, json: async () => body }); await flush(); await flush();
}
function bump() { lastDeskVersion++; if (active) wantPaper(false); }
const state = () => ({ fetches, maxInFlight, version: lastDeskVersion, panel: els["desk-paper"].innerHTML, meta: els["desk-paper-meta"].textContent, metaClass: els["desk-paper-meta"].className, timers: timers.length });
"""

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed; app.js helpers not exercised"
)


def lifted() -> list[str]:
    source = APP_JS.read_text(encoding="utf-8")
    parts = []
    for name in ONE_LINERS:
        found = re.search(rf"^const {name} = .*;$", source, re.M)
        assert found, f"`const {name} = ...;` not found as a single line in {APP_JS}"
        parts.append(found.group(0))
    for name in FUNCTIONS:
        found = re.search(rf"^(?:async )?function {name}\(.*?^\}}$", source, re.M | re.S)
        assert found, f"`function {name}(...)` not found in {APP_JS}"
        parts.append(found.group(0))
    return parts


def run(scenario: str) -> Any:
    """Run `scenario` (async JS using the harness) and return `state()` as JSON."""
    script = "\n".join(
        [
            HARNESS,
            *lifted(),
            f"(async () => {{ {scenario}\n console.log(JSON.stringify(state())); }})()",
        ]
    )
    done = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30, check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def starts(state: dict[str, Any]) -> list[int]:
    return [f["at"] for f in state["fetches"]]


class TestWhenItFetches:
    def test_opening_the_tab_fetches_at_once_and_renders(self):
        got = run("wantPaper(true); await advance(0); await reply(200, {enabled: true});")
        assert starts(got) == [0] and got["fetches"][0]["url"] == "/api/paper"
        assert got["panel"].startswith("PANEL") and got["meta"] == "META"

    def test_a_burst_of_snapshots_is_one_fetch_5_s_after_the_last(self):
        got = run(
            "wantPaper(true); await advance(0); await reply(200, OK);"
            "for (let i = 0; i < 4; i++) { await advance(500); bump(); }"  # 0.5 .. 2 s
            "await advance(10000); await reply(200, OK);"
        )
        assert starts(got) == [0, 5000]
        assert got["fetches"][1]["version"] == 3  # it saw the latest snapshot

    def test_a_snapshot_during_a_fetch_is_picked_up_by_a_trailing_fetch(self):
        got = run(
            "wantPaper(true); await advance(0); await advance(100); bump();"
            "await advance(100); await reply(200, OK); await advance(10000);"
            "if (pending.length) await reply(200, OK);"
        )
        assert starts(got) == [0, 5000]
        assert got["fetches"][1]["version"] == 0

    def test_no_new_snapshot_no_new_fetch(self):
        got = run("wantPaper(true); await advance(0); await reply(200, OK); await advance(60000);")
        assert starts(got) == [0] and got["timers"] == 0


class TestOnFailure:
    def test_a_first_failure_replaces_the_loading_line_with_the_reason(self):
        got = run(
            "wantPaper(true); await advance(0);"
            'await reply(503, {detail: "paper book unreadable: <locked>"});'
        )
        assert "Loading" not in got["panel"]
        assert "paper book unreadable: &lt;locked&gt;" in got["panel"]  # escaped once
        assert got["meta"] == "paper trades unavailable: paper book unreadable: <locked>"
        assert got["metaClass"] == "warn"

    def test_a_later_failure_keeps_the_table_and_warns_on_the_day_line(self):
        got = run(
            "wantPaper(true); await advance(0); await reply(200, OK); bump();"
            'await advance(5000); await reply(503, {detail: "locked"});'
        )
        assert got["panel"].startswith("PANEL") and got["metaClass"] == "warn"

    def test_success_after_a_failure_clears_the_warning(self):
        got = run(
            'wantPaper(true); await advance(0); await reply(503, {detail: "locked"});'
            "await advance(5000); await reply(200, OK);"
        )
        assert starts(got) == [0, 5000]  # retried at the 5 s floor while the tab is open
        assert got["panel"].startswith("PANEL") and (got["meta"], got["metaClass"]) == (
            "META",
            "muted",
        )

    def test_no_retry_once_the_tab_is_left(self):
        got = run(
            "wantPaper(true); await advance(0); active = false; await reply(500, {});"
            "await advance(60000);"
        )
        assert starts(got) == [0] and got["timers"] == 0

    @pytest.mark.parametrize(
        "body",
        ["{}", "[]", '"ok"', "null", "{enabled: 1}", '{detail: "x"}'],
        ids=["empty", "array", "string", "null", "enabled-not-bool", "no-enabled"],
    )
    def test_a_200_without_an_enabled_flag_is_a_failure_not_paper_off(self, body):
        # A proxy page or a half-written reply must never read as "Paper trading is off".
        got = run(f"wantPaper(true); await advance(0); await reply(200, {body});")
        assert "PANEL" not in got["panel"] and "Loading" not in got["panel"]
        assert got["meta"] == "paper trades unavailable: unexpected reply from /api/paper"
        assert got["metaClass"] == "warn"

    def test_a_200_that_is_not_json_is_a_failure(self):
        got = run(
            "wantPaper(true); await advance(0);"
            "const r = pending.shift(); inFlight--;"
            "r({ ok: true, status: 200, json: async () => { throw new SyntaxError('bad'); } });"
            "await flush(); await flush();"
        )
        assert got["meta"] == "paper trades unavailable: unexpected reply from /api/paper"

    def test_paper_off_is_a_real_answer(self):
        got = run("wantPaper(true); await advance(0); await reply(200, {enabled: false});")
        assert got["panel"].startswith("PANEL") and got["metaClass"] == "muted"

    def test_a_network_error_is_a_failure_too(self):
        got = run(
            "wantPaper(true); await advance(0);"
            "const r = pending.shift(); inFlight--; r(Promise.reject(new Error('offline')));"
            "await flush(); await flush();"
        )
        assert got["meta"] == "paper trades unavailable: offline"


class TestInvariants:
    @pytest.mark.parametrize("seed", [1, 7, 42, 1234])
    def test_random_snapshots_and_slow_answers_keep_the_rules(self, seed):
        # Over 5 simulated minutes: snapshots at random, answers after random delays, some
        # failing. Never two in flight, starts >= 5 s apart, and the last fetch saw the
        # last snapshot once everything settles.
        got = run(
            f"let s = {seed}; const rnd = () => (s = (s * 1103515245 + 12345) % 2147483648) / 2147483648;"
            "wantPaper(true);"
            "for (let step = 0; step < 600; step++) {"
            "  await advance(500);"
            "  if (rnd() < 0.3) bump();"
            "  if (pending.length && rnd() < 0.4) await reply(rnd() < 0.2 ? 503 : 200, OK);"
            "}"
            "for (let i = 0; i < 20; i++) { await advance(6000); while (pending.length) await reply(200, OK); }"
        )
        at = starts(got)
        assert got["maxInFlight"] == 1
        assert all(b - a >= 5000 for a, b in zip(at, at[1:], strict=False)), at
        assert got["fetches"][-1]["version"] == got["version"]
        assert got["timers"] == 0
