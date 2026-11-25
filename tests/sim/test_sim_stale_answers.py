"""Rules review R3: a late answer never draws over a newer choice (the owner's switch race).

sim.js itself runs in node on a minimal page (every element a plain record), a stub chart and a fetch whose answers the
test releases in any order. Like a browser that does NOT honour the abort, the old answer still arrives: the guards
`my !== S.req` in loadPicture, redrawTo and fetchChunk must drop it. Removing any one of them fails a test here."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .test_sim_ui import _node, needs_node

UI = Path(__file__).resolve().parents[2] / "src" / "onelot" / "ui"

HARNESS = r"""
globalThis.window = globalThis;
window.innerWidth = 1400; window.innerHeight = 900; window.scrollY = 0; window.addEventListener = () => {}; window.scrollTo = () => {};
const els = {};
function makeEl(id) {
  const e = { id, hidden: false, textContent: "", innerHTML: "", title: "", value: "", className: "", disabled: false,
    dataset: {}, children: [], offsetHeight: 0, offsetTop: 0, clientWidth: 1000, clientHeight: 500, isConnected: true,
    style: { setProperty() {}, removeProperty() {}, getPropertyValue() { return ""; } },
    classList: { toggle() {}, add() {}, remove() {}, contains() { return false; } },
    setAttribute() {}, getAttribute() { return null; }, addEventListener() {}, appendChild(c) { e.children.push(c); },
    removeChild() {}, append() {}, querySelectorAll() { return []; }, querySelector() { return null; }, focus() {},
    getBoundingClientRect() { return { top: 0, bottom: 0, left: 0, right: 0 }; }, scrollTop: 0, scrollHeight: 0 };
  e.parentElement = e;
  return e;
}
const $ = (id) => (els[id] = els[id] || makeEl(id));
globalThis.document = { documentElement: makeEl("html"), hidden: false, fullscreenElement: null, activeElement: null,
  querySelector: (s) => $(s), addEventListener() {}, createElement: () => makeEl("x"), head: makeEl("head") };
globalThis.location = { search: "" };
globalThis.getComputedStyle = () => ({ getPropertyValue: () => "" });
globalThis.requestAnimationFrame = (f) => setTimeout(() => f(Date.now()), 0);
globalThis.MutationObserver = class { observe() {} };
// fetch: every request waits until the test answers it
const pending = [];
globalThis.fetch = (url) => new Promise((resolve) => pending.push({ url, resolve }));
const answer = (match, body) => {
  const i = pending.findIndex((p) => p.url.includes(match));
  if (i < 0) throw new Error("no pending request for " + match + " in " + pending.map((p) => p.url).join(", "));
  const p = pending.splice(i, 1)[0];
  p.resolve({ ok: true, status: 200, json: async () => body });
};
const posts = [];
window.OneLot = { $, store: { get: (k, d) => d, set() {} }, toast() {}, tabShown: () => false, tabLoaders: {},
  api: (url, body) => new Promise((resolve) => posts.push({ url, body, resolve })) };
window.LightweightCharts = {};
const drawn = [];
window.SimCore = require(%(core)s);
window.SimChart = { create: () => ({
  bars: [], chart: { subscribeCrosshairMove() {}, timeScale: () => ({ subscribeVisibleLogicalRangeChange() {} }) },
  reset() { drawn.push("reset"); }, load(meta, tf, tfm, list) { drawn.push("load " + meta.key + " " + list[0][4]); },
  setCandles(tf, tfm, list) { drawn.push("candles " + list[0][4]); }, startView() {}, margins() {}, hideDates() {},
  candleAt() { return null; }, setLevels() {}, setZones() {}, setMarkers() {}, theme() {} }) };
require(%(sim)s);
const P = window.SimPage, S = P.state();
const tick = () => new Promise((r) => setTimeout(r, 5));
const candles = (close) => ({ candles: [[1791171900, close, close, close, close, 0]], forming: null, instrument: {} });
"""


def run_page(body: str) -> Any:
    script = (
        HARNESS
        % {"core": json.dumps(str(UI / "sim_core.js")), "sim": json.dumps(str(UI / "sim.js"))}
        + f"\n(async () => {{ const out = await (async () => {{ {body} }})(); console.log(JSON.stringify(out)); }})();"
    )
    return _node(script)


@needs_node
def test_a_late_candles_answer_of_the_old_instrument_never_draws():
    out = run_page(
        """S.choice.instrument = "NIFTY"; S.view.ctf = "3m";
        const a = P.loadPicture(); await tick();
        S.choice.instrument = "RELIANCE";
        const b = P.loadPicture(); await tick();
        answer("instrument=RELIANCE", candles(1200)); await b;   // the newer answer first
        answer("instrument=NIFTY", candles(24000)); await a;     // then the old one, late
        return { drawn: drawn.filter((d) => d.startsWith("load")), loading: S.loading, meta: S.meta.key };"""
    )
    assert out["drawn"] == ["load RELIANCE 1200"]
    assert out["loading"] is False and out["meta"] == "RELIANCE"


@needs_node
def test_a_late_redraw_after_a_switch_never_draws():
    out = run_page(
        """S.choice.instrument = "NIFTY"; S.view.ctf = "3m";
        const l = P.loadPicture(); await tick(); answer("instrument=NIFTY", candles(23000)); await l;
        const r = P.redrawTo(1791171900 + 600); await tick();
        S.req += 1;   // a switch happened meanwhile
        answer("instrument=NIFTY", candles(24000)); await r;
        return drawn.filter((d) => d.startsWith("candles"));"""
    )
    assert out == []


@needs_node
def test_a_late_chunk_after_a_switch_is_dropped():
    out = run_page(
        """S.sess = { id: "s_old", status: "RUNNING", session: { status: "RUNNING" } };
        const p = P.fetchChunk(5, "event"); await tick();
        S.req += 1; S.sess = { id: "s_new", status: "RUNNING", session: { status: "RUNNING" } };
        posts[0].resolve({ minutes: [[1791171900, 1, 1, 1, 1, 0]], events: [] });
        const got = await p;
        return [got, S.chunks.length];"""
    )
    assert out == [None, 0]
