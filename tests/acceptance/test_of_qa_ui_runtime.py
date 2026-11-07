"""Tester (QA): the Home / Live Desk JavaScript of OF-1 and OF-5 runs against the REAL desk's payloads.

src/onelot/ui/app.js is loaded whole into node (a tiny DOM stand-in, `fetch` answered from the
scenario) and driven the way the owner drives it: open Home, press "Load 10 more", open "my
prices" on a journal row, type a price into a position and press Save. The payloads are the
ones the real Desk produces (a Rig with a real PaperBook), never hand-written shapes.

Skipped (with a reason) on a machine without node.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import us001_helpers as H
from test_us002_b_desk_end_to_end import Rig

from onelot.config import AppConfig

ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "src" / "onelot" / "ui" / "app.js"
INDEX_HTML = ROOT / "src" / "onelot" / "ui" / "index.html"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

HARNESS = r"""// Runs src/onelot/ui/app.js in node against a tiny DOM stand-in and drives it with a scenario.
//   node ui_harness.js <app.js> <index.html> <scenario.json>
// The scenario names the GET payloads (by URL prefix) and the steps to take; the result is JSON
// on stdout: whatever the steps asked to read, plus every uncaught error and every fetch made.
const fs = require("fs");
const [, , appPath, htmlPath, scenarioPath] = process.argv;
const scenario = JSON.parse(fs.readFileSync(scenarioPath, "utf8"));
const html = fs.readFileSync(htmlPath, "utf8");
const docOrder = [...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]);
const staticIds = new Set(docOrder);
const camel = (s) => s.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
const unescapeHtml = (s) => s.replace(/&quot;/g, '"').replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");

const errors = [], fetches = [], toasts = [];
const registry = new Map(); // static elements by id
let activeTab = "home";

class Elem {
  constructor(id) {
    this.id = id || "";
    this._html = "";
    this._kids = {};
    this.dataset = {};
    this.style = {};
    this.value = "";
    this.checked = false;
    this.hidden = false;
    this.options = [];
    this.textContent = "";
    this.className = "";
    this.children = [];
    this.listeners = {};
    this.onclick = null;
    this.onchange = null;
    this.oninput = null;
    const self = this;
    this.classList = {
      add() {}, remove() {}, toggle() {},
      contains: (c) => (self.activeFlag ? c === "active" && self.activeFlag() : false),
    };
  }
  set innerHTML(v) { this._html = String(v); this._kids = {}; }
  get innerHTML() { return this._html; }
  get isConnected() { return true; }
  setAttribute() {} getAttribute() { return null; } removeAttribute() {}
  appendChild(c) { this.children.push(c); if (c && c.className && /toast/.test(c.className)) toasts.push({ text: c.textContent, kind: c.className }); return c; }
  remove() {} focus() {} blur() {} scrollIntoView() {}
  insertAdjacentHTML(_p, h) { this._html += h; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  contains() { return false; }
  closest() { return null; }
  getBoundingClientRect() { return { width: 0, height: 0, top: 0, left: 0 }; }
  getContext() { return new Proxy({}, { get: () => () => {} }); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    if (this._kids[sel]) return this._kids[sel];
    let out = [];
    let m;
    if ((m = sel.match(/^\[data-([a-z-]+)\]$/))) {
      const re = new RegExp(`data-${m[1]}="([^"]*)"`, "g");
      for (const x of this._html.matchAll(re)) { const k = new Elem(""); k.dataset[camel(m[1])] = unescapeHtml(x[1]); out.push(k); }
    } else if (sel === "input[id]") {
      for (const x of this._html.matchAll(/<input\b[^>]*\bid="([^"]+)"[^>]*>/g)) out.push(inputFrom(x[0], x[1]));
    }
    this._kids[sel] = out;
    return out;
  }
}
function inputFrom(tag, id) {
  const k = new Elem(id);
  const v = tag.match(/\bvalue="([^"]*)"/);
  k.value = v ? unescapeHtml(v[1]) : "";
  return k;
}
function byId(id) {
  if (staticIds.has(id)) {
    if (!registry.has(id)) registry.set(id, new Elem(id));
    return registry.get(id);
  }
  // an element made by innerHTML: the first container (document order) that holds it, exactly
  // what document.getElementById answers when two containers repeat an id
  for (const cid of docOrder) {
    const c = registry.get(cid);
    if (!c || !c._html) continue;
    const input = c.querySelectorAll("input[id]").find((k) => k.id === id);
    if (input) return input;
    const tag = c._html.match(new RegExp(`<[a-z]+\\b[^>]*\\bid="${id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}"[^>]*>`));
    if (!tag) continue;
    if (!c._kids["#" + id]) c._kids["#" + id] = [new Elem(id)];
    return c._kids["#" + id][0];
  }
  return null;
}

const tabEls = ["home", "live", "lab", "data", "news", "settings"].map((n) => {
  const e = new Elem("");
  e.dataset.tab = n;
  e.activeFlag = () => activeTab === n;
  e.classList.toggle = (c, on) => { if (c === "active" && on) activeTab = n; };
  return e;
});

const document = {
  getElementById: byId,
  createElement: () => new Elem(""),
  querySelector(sel) {
    const m = sel.match(/data-tab="([a-z]+)"/);
    if (m) return tabEls.find((e) => e.dataset.tab === m[1]) || null;
    return null;
  },
  querySelectorAll(sel) { return sel === ".tab" ? tabEls : []; },
  addEventListener() {},
  documentElement: new Elem(""),
  body: new Elem(""),
  get activeElement() { return null; },
};
const store = new Map();
const route = (url) => {
  const keys = Object.keys(scenario.get || {}).sort((a, b) => b.length - a.length);
  const hit = keys.find((k) => url.startsWith(k));
  return hit === undefined ? {} : scenario.get[hit];
};
const fakeFetch = async (url, opt = {}) => {
  const method = (opt.method || "GET").toUpperCase();
  const body = opt.body ? JSON.parse(opt.body) : null;
  fetches.push({ url, method, body });
  const payload = method === "GET" ? route(url) : (scenario.post || {})[url] || (scenario.postDefault || { ok: true });
  return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(payload)) };
};
let stream = null;
class FakeSource { constructor() { stream = this; } close() {} }

const g = global;
g.window = g;
g.document = document;
g.localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: (k) => store.delete(k) };
g.location = { hash: scenario.hash || "" };
g.fetch = fakeFetch;
g.EventSource = FakeSource;
g.getComputedStyle = () => ({ getPropertyValue: () => "" });
g.requestAnimationFrame = () => 0;
g.setInterval = () => 0;
g.setTimeout = (fn, ms) => { if (ms === undefined || ms < 1) return setImmediate(fn); return 0; };
g.confirm = () => true;
g.addEventListener = () => {};
g.ONELOT_TOKEN = "t";
g.Charts = undefined;
process.on("uncaughtException", (e) => errors.push("uncaught: " + (e && e.stack || e)));
process.on("unhandledRejection", (e) => errors.push("unhandled: " + (e && e.stack || e)));

let skew = 0; // every stream message arrives 5 s after the one before (renderDesk draws once per 4 s)
const realNow = Date.now.bind(Date);
Date.now = () => realNow() + skew;
const flush = async () => { for (let i = 0; i < 12; i++) await new Promise((r) => setImmediate(r)); };
const read = (id) => { const e = byId(id); return e ? e.innerHTML : null; };
const count = (s, needle) => (s ? s.split(needle).length - 1 : 0);

(async () => {
  try {
    require("vm").runInThisContext(fs.readFileSync(appPath, "utf8"), { filename: "app.js" });
  } catch (e) { errors.push("load: " + (e && e.stack || e)); }
  await flush();
  const out = { steps: [] };
  for (const step of scenario.steps || []) {
    const r = { op: step.op };
    try {
      if (step.op === "read") r.html = read(step.id);
      else if (step.op === "text") { const e = byId(step.id); r.text = e ? e.textContent : null; }
      else if (step.op === "count") r.n = count(read(step.id), step.needle);
      else if (step.op === "click") { const e = byId(step.id); r.found = !!e; if (e && e.onclick) await e.onclick({ stopPropagation() {} }); r.clicked = !!(e && e.onclick); }
      else if (step.op === "clickData") {
        const c = byId(step.container);
        const kids = c.querySelectorAll(`[data-${step.attr}]`);
        const k = kids.find((x) => x.dataset[camel(step.attr)] === step.value) || kids[step.index || 0];
        r.found = !!k; r.clicked = !!(k && k.onclick);
        if (k && k.onclick) await k.onclick({ stopPropagation() {} });
      }
      else if (step.op === "set") { const e = byId(step.id); r.found = !!e; if (e) { e.value = step.value; e.dataset.dirty = "1"; } }
      else if (step.op === "setIn") {
        const k = byId(step.container).querySelectorAll("input[id]").find((x) => x.id === step.id);
        r.found = !!k; if (k) { k.value = step.value; k.dataset.dirty = "1"; }
      }
      else if (step.op === "getIn") {
        const k = byId(step.container).querySelectorAll("input[id]").find((x) => x.id === step.id);
        r.found = !!k; r.value = k ? k.value : null;
      }
      else if (step.op === "tab") { activeTab = step.name; }
      else if (step.op === "change") { const e = byId(step.id); if (e) { if (step.value !== undefined) e.value = step.value; if (e.onchange) await e.onchange(); } }
      else if (step.op === "input") { const e = byId(step.id); if (e) { e.value = step.value; if (e.oninput) await e.oninput(); } }
      else if (step.op === "stream") { skew += 5000; stream.onmessage({ data: JSON.stringify(step.payload) }); }
      else if (step.op === "ids") {
        const html = step.containers.map((c) => [c, read(c) || ""]);
        const seen = {};
        for (const [c, h] of html) for (const m of h.matchAll(/\bid="([^"]+)"/g)) (seen[m[1]] = seen[m[1]] || new Set()).add(c);
        r.duplicated = Object.keys(seen).filter((k) => seen[k].size > 1);
      }
      await flush();
    } catch (e) { r.error = String(e && e.stack || e); errors.push("step " + step.op + ": " + r.error); }
    out.steps.push(r);
  }
  out.errors = errors; out.fetches = fetches; out.toasts = toasts;
  process.stdout.write(JSON.stringify(out));
  process.exit(0);
})();
"""

# the legacy Bank Nifty engine's state, as /api/state carries it next to the desk snapshot
LEGACY_STATE = {
    "status": {
        "running": True,
        "mode": "PAPER",
        "broker": "dhan",
        "now": "2026-11-05T11:00:00+05:30",
    },
    "health": {
        "state": "OK",
        "reasons": [],
        "may_signal": True,
        "may_manage_positions": True,
        "details": {},
    },
    "regime": {"regime": "CHOP", "confidence": 0.5, "probabilities": {}, "method": "rule_based"},
    "ivp": None,
    "risk": {"state": "ACTIVE", "realized_pnl": 0.0, "trades_taken": 0, "open_positions": 0},
    "active_signal": None,
    "signal_seconds_remaining": 0,
    "positions": [],
    "prices": {"index": 59000.0, "futures": 58950.0},
    "chain": {"polls": 1, "failures": 0, "fresh": True},
    "fallback": {"source": "yfinance", "available": True},
    "rate_limiter": {"global_available": 12.0, "endpoints": {}},
    "notifications": {"channels": []},
    "journal": {"trades": 0, "note": "no closed trades yet"},
}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    H.block_network(monkeypatch)


@pytest.fixture
def app_cfg():
    return AppConfig.load()


@pytest.fixture
def desk_db(tmp_path):
    d = H.new_db(tmp_path, "desk.db", make_global=True)
    yield d
    H.dispose_db(d, was_global=True)


@pytest.fixture
def rig(app_cfg, desk_db):
    return Rig(app_cfg, desk_db)


def run_ui(tmp_path: Path, scenario: dict) -> dict:
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    spec = tmp_path / "scenario.json"
    spec.write_text(json.dumps(scenario, default=str), encoding="utf-8")
    done = subprocess.run(
        ["node", str(harness), str(APP_JS), str(INDEX_HTML), str(spec)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def step(res: dict, i: int) -> dict:
    return res["steps"][i]


def real_archive(rig) -> list[dict]:
    """Archived alerts as the desk serves them: a stock stop-out and an option session-end,
    both with their exit (OF-5), a no-fill, and one the owner took and closed."""

    stock = H.raise_alert(rig.desk, "SBIN", now=H.mid_session(rig.desk, "SBIN"))
    assert rig.desk.acknowledge(stock["id"], "SKIP")["accepted"]
    index = H.raise_alert(rig.desk, "BANKNIFTY", now=H.mid_session(rig.desk, "BANKNIFTY"))
    taken = rig.desk.acknowledge(index["id"], "TAKE")
    assert taken["accepted"]
    rig.script("SBIN")
    rig.script("BANKNIFTY")
    rig.poll()
    rows = rig.desk.signals_view(days=30)["signals"]
    assert any(r.get("exit") for r in rows), "harness: the rig produced no exited alert"
    assert stock["id"] in {r["id"] for r in rows}
    assert all(r["status"] != "PENDING" for r in rows), "harness: an archive row is still pending"
    return rows


def many(rows: list[dict], n: int, *, key: str = "id") -> list[dict]:
    """`n` rows cycled from the real ones, each with its own id and a falling raise time."""
    out = []
    base = dt.datetime(2026, 11, 5, 14, 0, tzinfo=H.IST)
    for i in range(n):
        r = copy.deepcopy(rows[i % len(rows)])
        r[key] = f"{r[key]}-{i:02d}"
        if "created_at" in r:
            r["created_at"] = (base - dt.timedelta(minutes=17 * i)).isoformat()
        out.append(r)
    return out


def signals_payload(rows: list[dict]) -> dict:
    return {
        "signals": rows,
        "scoreboard": {},
        "days": 7,
        "pending": [],
        "primary_strategies": ["ema_cross", "supertrend_vol"],
        "paper_scoreboard": [],
    }


# ══ OF-1: the archive shows 10 and loads 10 more ═════════════════════════════════════════
class TestArchivePaging:
    def scenario(self, rows, steps):
        return {
            "get": {
                "/api/desk/signals": signals_payload(rows),
                "/api/desk/journal": {"trades": []},
            },
            "steps": steps,
        }

    def test_ten_at_a_time_then_all_then_back_to_the_newest_ten(self, rig, tmp_path):
        rows = many(real_archive(rig), 25)
        res = run_ui(
            tmp_path,
            self.scenario(
                rows,
                [
                    {"op": "count", "id": "home-archive", "needle": "data-aid="},  # 0
                    {"op": "read", "id": "home-archive-more"},  # 1
                    {"op": "click", "id": "archive-more"},  # 2
                    {"op": "count", "id": "home-archive", "needle": "data-aid="},  # 3
                    {"op": "read", "id": "home-archive-more"},  # 4
                    {"op": "click", "id": "archive-more"},  # 5
                    {"op": "count", "id": "home-archive", "needle": "data-aid="},  # 6
                    {"op": "read", "id": "home-archive-more"},  # 7
                    {"op": "click", "id": "archive-less"},  # 8
                    {"op": "count", "id": "home-archive", "needle": "data-aid="},  # 9
                ],
            ),
        )
        assert res["errors"] == []
        assert step(res, 0)["n"] == 10
        assert "Load 10 more" in step(res, 1)["html"] and "10 of 25 shown" in step(res, 1)["html"]
        assert step(res, 3)["n"] == 20
        assert "Load 5 more" in step(res, 4)["html"] and "20 of 25 shown" in step(res, 4)["html"]
        assert step(res, 6)["n"] == 25
        assert "all 25 shown" in step(res, 7)["html"]
        assert step(res, 9)["n"] == 10

    @pytest.mark.parametrize("n, button", [(0, False), (7, False), (10, False), (11, True)])
    def test_the_button_appears_only_when_there_is_more_to_load(self, rig, tmp_path, n, button):
        rows = many(real_archive(rig), n) if n else []
        res = run_ui(
            tmp_path,
            self.scenario(
                rows,
                [
                    {"op": "count", "id": "home-archive", "needle": "data-aid="},
                    {"op": "read", "id": "home-archive-more"},
                ],
            ),
        )
        assert res["errors"] == []
        assert step(res, 0)["n"] == min(n, 10)
        assert ("archive-more" in step(res, 1)["html"]) is button

    def test_changing_the_window_starts_again_at_the_newest_ten(self, rig, tmp_path):
        rows = many(real_archive(rig), 25)
        res = run_ui(
            tmp_path,
            self.scenario(
                rows,
                [
                    {"op": "click", "id": "archive-more"},
                    {"op": "click", "id": "archive-more"},
                    {"op": "count", "id": "home-archive", "needle": "data-aid="},  # 2
                    {"op": "change", "id": "alerts-days", "value": "30"},
                    {"op": "count", "id": "home-archive", "needle": "data-aid="},  # 4
                ],
            ),
        )
        assert res["errors"] == []
        assert step(res, 2)["n"] == 25
        assert step(res, 4)["n"] == 10


class TestArchiveSurvivesTheLiveRefresh:
    def test_the_pages_the_owner_opened_stay_open_when_the_alerts_reload_themselves(
        self, rig, tmp_path
    ):
        """The Home alerts reload every 15 s while the owner reads (a desk snapshot arrives): an
        archive grown to 20 rows must not snap back to 10 under their hands."""
        rows = many(real_archive(rig), 25)
        quiet_desk = {
            "updated_at": "2026-11-05T11:00:00+05:30",
            "booting": False,
            "version": 1,
            "source": {"name": "offline", "resolution": "2m", "poll_seconds": 60},
            "session": {"open": False, "trading_day": True, "entries_allowed": False},
            "context": [],
            "positions": [],
            "signals": [],
            "indices": [],
            "stocks": [],
            "risk": {},
            "paper": None,
            "scoreboard": {},
            "counts": {"instruments": 0, "strategies": 0, "pending": 0, "hold": 0},
        }
        messages = [
            {
                "state": {**LEGACY_STATE, "desk": {**quiet_desk, "version": 1 + i}},
                "notifications": [],
            }
            for i in range(5)
        ]
        res = run_ui(
            tmp_path,
            {
                "get": {
                    "/api/desk/signals": signals_payload(rows),
                    "/api/desk/journal": {"trades": []},
                },
                "steps": [
                    {"op": "tab", "name": "home"},
                    {"op": "click", "id": "archive-more"},
                    {"op": "count", "id": "home-archive", "needle": "data-aid="},  # 2
                    *[{"op": "stream", "payload": m} for m in messages],  # 25 s: a reload
                    {"op": "count", "id": "home-archive", "needle": "data-aid="},  # 8
                ],
            },
        )
        assert res["errors"] == []
        reloads = [f for f in res["fetches"] if f["url"].startswith("/api/desk/signals")]
        assert len(reloads) >= 2, "the harness never made the alerts reload"
        assert step(res, 2)["n"] == 20 and step(res, 8)["n"] == 20


# ══ OF-5: "my prices" in the journal and the position tables ═════════════════════════════
def real_journal(rig) -> list[dict]:
    """Closed desk trades as the desk serves them: one closed at its exit alert's price, one
    the owner closed early (the alert's exit not in yet)."""
    stock = H.raise_alert(rig.desk, "SBIN", now=H.mid_session(rig.desk, "SBIN"))
    p1 = rig.desk.acknowledge(stock["id"], "TAKE")["position_id"]
    rig.desk.signals.clear()
    index = H.raise_alert(rig.desk, "BANKNIFTY", now=H.mid_session(rig.desk, "BANKNIFTY"))
    p2 = rig.desk.acknowledge(index["id"], "TAKE", fill_price=index["economics"]["entry_price"] + 1)
    assert rig.desk.close_position(p2["position_id"], index["economics"]["entry_price"] + 3.0)
    rig.script("SBIN")
    rig.poll()
    assert p1 not in rig.desk.positions
    rows = rig.desk.journal()
    assert len(rows) == 2
    return rows


class TestJournalMyPrices:
    def test_ten_rows_load_ten_more_and_my_prices_posts_what_was_typed(self, rig, tmp_path):
        rows = many(real_journal(rig), 12)
        first = rows[0]["id"]
        get = {
            "/api/desk/journal": {"trades": rows},
            "/api/desk/signals": signals_payload([]),
        }
        post = {f"/api/desk/position/{first}/report": {"ok": True, "diff_pnl": -3.2}}
        res = run_ui(
            tmp_path,
            {
                "get": get,
                "post": post,
                "steps": [
                    {"op": "count", "id": "home-journal", "needle": "data-jedit="},  # 0
                    {"op": "click", "id": "journal-more"},
                    {"op": "count", "id": "home-journal", "needle": "data-jedit="},  # 2
                    {
                        "op": "clickData",
                        "container": "home-journal",
                        "attr": "jedit",
                        "value": first,
                    },
                    {"op": "count", "id": "home-journal", "needle": f'id="je_{first}"'},  # 4
                    {"op": "set", "id": f"je_{first}", "value": "123.45"},
                    {
                        "op": "clickData",
                        "container": "home-journal",
                        "attr": "jsave",
                        "value": first,
                    },
                ],
            },
        )
        assert res["errors"] == []
        assert step(res, 0)["n"] == 10
        assert step(res, 2)["n"] == 12
        assert step(res, 4)["n"] == 1, "the 'my prices' form opened under its row"
        posts = [f for f in res["fetches"] if f["method"] == "POST"]
        assert posts == [
            {
                "url": f"/api/desk/position/{first}/report",
                "method": "POST",
                "body": {"entry_price": 123.45, "exit_price": rows[0]["user_exit_price"]},
            }
        ]
        assert any("Saved" in t["text"] for t in res["toasts"])

    def test_a_saved_form_with_nothing_typed_posts_nothing(self, rig, tmp_path):
        rows = real_journal(rig)
        first = rows[0]["id"]
        res = run_ui(
            tmp_path,
            {
                "get": {
                    "/api/desk/journal": {"trades": rows},
                    "/api/desk/signals": signals_payload([]),
                },
                "steps": [
                    {
                        "op": "clickData",
                        "container": "home-journal",
                        "attr": "jedit",
                        "value": first,
                    },
                    {"op": "set", "id": f"je_{first}", "value": ""},
                    {"op": "set", "id": f"jx_{first}", "value": ""},
                    {
                        "op": "clickData",
                        "container": "home-journal",
                        "attr": "jsave",
                        "value": first,
                    },
                ],
            },
        )
        assert res["errors"] == []
        assert [f for f in res["fetches"] if f["method"] == "POST"] == []
        assert any("Type your entry" in t["text"] for t in res["toasts"])

    def test_a_row_closed_before_of5_renders_without_an_error(self, rig, tmp_path):
        rows = real_journal(rig)
        old = copy.deepcopy(rows[0])
        for k in (
            "alert_created_at",
            "alert_exit_price",
            "user_entry_price",
            "user_net_pnl",
            "diff_pnl",
            "exit_source",
            "label",
            "alert_net_pnl",
        ):
            old[k] = None
        res = run_ui(
            tmp_path,
            {
                "get": {
                    "/api/desk/journal": {"trades": [old]},
                    "/api/desk/signals": signals_payload([]),
                },
                "steps": [{"op": "read", "id": "home-journal"}],
            },
        )
        assert res["errors"] == []
        html = step(res, 0)["html"]
        assert "undefined" not in html and "NaN" not in html and "null" not in html


class TestPositionTables:
    @staticmethod
    def streams(rig):
        alert = H.raise_alert(rig.desk, "SBIN", now=H.mid_session(rig.desk, "SBIN"))
        pid = rig.desk.acknowledge(
            alert["id"], "TAKE", fill_price=alert["economics"]["entry_price"] + 1.0
        )["position_id"]
        now = dt.datetime.now(tz=H.IST)
        rig.desk._rebuild_snapshot(now, H.OpenClock().current_window(now))
        snap = json.loads(json.dumps(rig.desk.snapshot(), default=str))
        again = {**snap, "version": snap["version"] + 1}
        mk = lambda desk: {"state": {**LEGACY_STATE, "desk": desk}, "notifications": []}  # noqa: E731
        return pid, alert, mk(snap), mk(again)

    def test_the_monitoring_table_shows_the_alert_time_and_both_entries(self, rig, tmp_path):
        pid, alert, first, _ = self.streams(rig)
        res = run_ui(
            tmp_path,
            {
                "get": {
                    "/api/desk/signals": signals_payload([]),
                    "/api/desk/journal": {"trades": []},
                },
                "steps": [
                    {"op": "tab", "name": "home"},
                    {"op": "stream", "payload": first},
                    {"op": "read", "id": "home-positions"},
                ],
            },
        )
        assert res["errors"] == []
        html = step(res, 2)["html"]
        for header in ("Alert at", "Taken at", "Alert entry", "Your entry", "Your prices"):
            assert header in html
        assert alert["created_at"][11:16] in html, "the alert's own time (IST) is on the row"
        assert f"{alert['economics']['entry_price'] + 1.0:.2f}" in html, "the owner's own entry"
        assert f'id="dx_{pid}"' in html and "undefined" not in html and "NaN" not in html

    def test_no_element_id_is_shared_by_the_home_and_the_live_desk_tables(self, rig, tmp_path):
        """Both tables are in the document at once (a tab is only hidden): a repeated id makes
        document.getElementById answer with the Home table's input for the Live Desk's row."""
        pid, _, first, second = self.streams(rig)
        res = run_ui(
            tmp_path,
            {
                "get": {
                    "/api/desk/signals": signals_payload([]),
                    "/api/desk/journal": {"trades": []},
                },
                "steps": [
                    {"op": "tab", "name": "home"},
                    {"op": "stream", "payload": first},
                    {"op": "tab", "name": "live"},
                    {"op": "stream", "payload": second},
                    {"op": "ids", "containers": ["home-positions", "desk-positions"]},
                ],
            },
        )
        assert res["errors"] == []
        assert step(res, 4)["duplicated"] == []

    def test_a_price_typed_on_the_live_desk_row_is_the_one_that_is_saved(self, rig, tmp_path):
        pid, alert, first, second = self.streams(rig)
        res = run_ui(
            tmp_path,
            {
                "get": {
                    "/api/desk/signals": signals_payload([]),
                    "/api/desk/journal": {"trades": []},
                },
                "post": {f"/api/desk/position/{pid}/report": {"ok": True, "diff_pnl": 0.0}},
                "steps": [
                    {"op": "tab", "name": "home"},
                    {"op": "stream", "payload": first},
                    {"op": "tab", "name": "live"},
                    {"op": "stream", "payload": second},
                    {
                        "op": "setIn",
                        "container": "desk-positions",
                        "id": f"dx_{pid}",
                        "value": "987.65",
                    },
                    {
                        "op": "clickData",
                        "container": "desk-positions",
                        "attr": "dreport",
                        "value": pid,
                    },
                ],
            },
        )
        assert res["errors"] == []
        posts = [f for f in res["fetches"] if f["method"] == "POST"]
        assert [p["body"] for p in posts] == [
            {"entry_price": alert["economics"]["entry_price"] + 1.0, "exit_price": 987.65}
        ], (
            posts,
            [t["text"] for t in res["toasts"]],
        )

    def test_a_price_typed_on_the_home_row_is_saved(self, rig, tmp_path):
        pid, alert, first, _ = self.streams(rig)
        res = run_ui(
            tmp_path,
            {
                "get": {
                    "/api/desk/signals": signals_payload([]),
                    "/api/desk/journal": {"trades": []},
                },
                "post": {f"/api/desk/position/{pid}/report": {"ok": True, "diff_pnl": 0.0}},
                "steps": [
                    {"op": "tab", "name": "home"},
                    {"op": "stream", "payload": first},
                    {
                        "op": "setIn",
                        "container": "home-positions",
                        "id": f"dx_{pid}",
                        "value": "987.65",
                    },
                    {
                        "op": "clickData",
                        "container": "home-positions",
                        "attr": "dreport",
                        "value": pid,
                    },
                ],
            },
        )
        assert res["errors"] == []
        posts = [f for f in res["fetches"] if f["method"] == "POST"]
        assert [p["body"] for p in posts] == [
            {"entry_price": alert["economics"]["entry_price"] + 1.0, "exit_price": 987.65}
        ]

    @pytest.mark.parametrize("tab, box", [("home", "home-positions"), ("live", "desk-positions")])
    def test_a_price_being_typed_survives_the_next_poll_that_redraws_the_table(
        self, rig, tmp_path, tab, box
    ):
        """keepInputs() is there so that a snapshot every few seconds never wipes the price the
        owner is in the middle of typing, on either tab."""
        pid, _, first, second = self.streams(rig)
        desk = copy.deepcopy(second["state"]["desk"])  # its own positions: `second` is untouched
        redrawn = {
            **second,
            "state": {
                **second["state"],
                "desk": {
                    **desk,
                    "version": desk["version"] + 1,
                    "updated_at": "2026-11-05T11:00:05",
                },
            },
        }
        for p in redrawn["state"]["desk"]["positions"]:
            p["unrealized"] = (p.get("unrealized") or 0) + 7.0  # the table is drawn again
        res = run_ui(
            tmp_path,
            {
                "get": {
                    "/api/desk/signals": signals_payload([]),
                    "/api/desk/journal": {"trades": []},
                },
                "steps": [
                    {"op": "tab", "name": "home"},
                    {"op": "stream", "payload": first},
                    {"op": "tab", "name": tab},
                    {"op": "stream", "payload": second},
                    {"op": "setIn", "container": box, "id": f"dx_{pid}", "value": "987.65"},
                    {"op": "stream", "payload": redrawn},
                    {"op": "getIn", "container": box, "id": f"dx_{pid}"},
                ],
            },
        )
        assert res["errors"] == []
        assert step(res, 6)["value"] == "987.65"


# ══ OF-3: the Data tab filters the coverage by source and by timeframe ══════════════════════
class TestDataTabFilters:
    @staticmethod
    def coverage_payload(tmp_path):
        """GET /api/lab/coverage exactly as the app answers it, over a small mixed corpus."""
        import warnings
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        import numpy as np
        import pandas as pd

        from onelot.api.server import create_app
        from onelot.data import store
        from onelot.store import init_database

        def bars(days, freq, start):
            rng = np.random.default_rng(3)
            idx = []
            day = pd.Timestamp(start, tz=H.IST)
            while len({d.date() for d in idx}) < days:
                if day.weekday() < 5:
                    idx += list(
                        pd.date_range(
                            day + pd.Timedelta(hours=9, minutes=15),
                            periods=375 // freq,
                            freq=f"{freq}min",
                        )
                    )
                day += pd.Timedelta(days=1)
            c = 100 * np.exp(np.cumsum(rng.normal(0, 0.001, len(idx))))
            return pd.DataFrame(
                {"open": c, "high": c + 0.1, "low": c - 0.1, "close": c, "volume": 1000.0},
                index=pd.DatetimeIndex(idx),
            )

        db = init_database(f"sqlite:///{tmp_path / 'cov.db'}")
        try:
            store.save_bars(db.engine, "AAA", "1m", bars(30, 1, "2026-06-01"), "dhan")
            store.save_bars(db.engine, "AAA", "15m", bars(10, 15, "2026-07-13"), "yahoo")
            store.save_bars(db.engine, "BBB", "5m", bars(12, 5, "2026-07-06"), "yahoo")
            store.save_bars(db.engine, "BBB", "60m", bars(60, 60, "2026-05-04"), "yahoo")
            store.save_bars(db.engine, "CCC", "2m", bars(5, 2, "2026-10-05"), "yahoo")
            engine = MagicMock()
            engine.db = SimpleNamespace(engine=db.engine)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                from fastapi.testclient import TestClient
            return TestClient(create_app(engine, "t")).get("/api/lab/coverage").json()
        finally:
            db.dispose()

    def rows(self, res, i):
        return step(res, i)["n"]

    def test_filters_narrow_both_views_and_flag_the_short_intraday_history(self, tmp_path):
        payload = self.coverage_payload(tmp_path)
        eff, raw = payload["effective"], payload["coverage"]
        assert eff and raw
        want = {
            "all": len(eff),
            "yahoo": sum(1 for r in eff if r["source"] == "yahoo"),
            "dhan_15": sum(1 for r in eff if r["source"] == "dhan" and r["timeframe"] == "15m"),
            "raw_1m": sum(1 for r in raw if r["resolution"] == "1m"),
            "raw_yahoo_5m": sum(
                1 for r in raw if r["source"] == "yahoo" and r["resolution"] == "5m"
            ),
            "q": sum(1 for r in eff if "BB" in r["series_key"]),
        }
        assert 0 < want["yahoo"] < want["all"] and want["dhan_15"] == 1 and want["raw_1m"] == 1
        trs = "<tr "
        res = run_ui(
            tmp_path,
            {
                "hash": "#data",
                "get": {"/api/lab/coverage": payload, "/api/data/jobs": {"jobs": []}},
                "steps": [
                    {"op": "count", "id": "data-coverage", "needle": trs},  # 0
                    {"op": "text", "id": "cov-meta"},  # 1
                    {"op": "change", "id": "cov-source", "value": "yahoo"},
                    {"op": "count", "id": "data-coverage", "needle": trs},  # 3
                    {"op": "change", "id": "cov-source", "value": "dhan"},
                    {"op": "change", "id": "cov-tf", "value": "15m"},
                    {"op": "count", "id": "data-coverage", "needle": trs},  # 6
                    {"op": "change", "id": "cov-source", "value": ""},
                    {"op": "change", "id": "cov-tf", "value": ""},
                    {"op": "input", "id": "cov-q", "value": "bb"},
                    {"op": "count", "id": "data-coverage", "needle": trs},  # 10
                    {"op": "input", "id": "cov-q", "value": ""},
                    {"op": "change", "id": "cov-view", "value": "stored"},
                    {"op": "change", "id": "cov-tf", "value": "1m"},
                    {"op": "count", "id": "data-coverage", "needle": trs},  # 14
                    {"op": "change", "id": "cov-tf", "value": "5m"},
                    {"op": "change", "id": "cov-source", "value": "yahoo"},
                    {"op": "count", "id": "data-coverage", "needle": trs},  # 17
                ],
            },
        )
        assert res["errors"] == []
        assert self.rows(res, 0) == want["all"]
        assert "intraday with under 120 days" in step(res, 1)["text"], "the short history is said"
        assert self.rows(res, 3) == want["yahoo"]
        assert self.rows(res, 6) == want["dhan_15"]
        assert self.rows(res, 10) == want["q"]
        assert self.rows(res, 14) == want["raw_1m"]
        assert self.rows(res, 17) == want["raw_yahoo_5m"]


# ══ OF-4: the leaderboard controls ask the server for the order the owner picked ═════════════
class TestLeaderboardControls:
    @staticmethod
    def lab_payloads(tmp_path):
        """The Lab's static GET payloads exactly as the app serves them (no database content)."""
        import warnings
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        from onelot.api.server import create_app
        from onelot.desk.config import load_desk_config
        from onelot.store import init_database

        db = init_database(f"sqlite:///{tmp_path / 'lab.db'}")
        try:
            engine = MagicMock()
            engine.db = SimpleNamespace(engine=db.engine)
            engine.desk = SimpleNamespace(cfg=load_desk_config())
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                from fastapi.testclient import TestClient
            client = TestClient(create_app(engine, "t"))
            return {
                url: client.get(url).json()
                for url in ("/api/lab/strategies", "/api/lab/instruments", "/api/lab/focus")
            }
        finally:
            db.dispose()

    def test_sort_order_and_the_primary_filter_reach_the_leaderboard_request(self, tmp_path):
        get = {
            **self.lab_payloads(tmp_path),
            "/api/lab/runs": {"runs": []},
            "/api/lab/leaderboard": {"rows": [], "summary": {}},
        }
        res = run_ui(
            tmp_path,
            {
                "hash": "#lab",
                "get": get,
                "steps": [
                    {"op": "change", "id": "board-strats", "value": "primary"},
                    {"op": "change", "id": "board-sort", "value": "win_rate"},
                    {"op": "click", "id": "board-order"},
                    {"op": "change", "id": "board-sort", "value": "profit_pct"},
                    {"op": "change", "id": "board-strats", "value": ""},
                    {"op": "text", "id": "board-order"},
                ],
            },
        )
        assert res["errors"] == []
        urls = [f["url"] for f in res["fetches"] if f["url"].startswith("/api/lab/leaderboard")]
        assert len(urls) >= 4, urls
        from urllib.parse import parse_qs, urlsplit

        queries = [parse_qs(urlsplit(u).query) for u in urls]
        primary = ",".join(get["/api/lab/focus"]["primary_strategies"])
        wins_high = next(q for q in queries if q.get("sort") == ["win_rate"])
        assert wins_high["order"] == ["desc"] and wins_high["strategies"] == [primary]
        wins_low = next(
            q for q in queries if q.get("sort") == ["win_rate"] and q["order"] == ["asc"]
        )
        assert wins_low["strategies"] == [primary]
        profit = next(q for q in queries if q.get("sort") == ["profit_pct"])
        assert profit["order"] == ["asc"], "the order the owner picked is kept"
        assert "strategies" not in queries[-1], "'all strategies' sends no filter"
        assert "low first" in step(res, 5)["text"]
