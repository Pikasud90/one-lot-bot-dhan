/* The Simulator tab (US-094): a replay workspace in the manner of a bar-replay tool.
   Talks to the engine through the second contract (.agile/stories/SIM-API.md and SIM-API-2.md). The server owns the
   cursor; the page asks for CHUNKS of minutes and reveals them one by one on its own clock (requestAnimationFrame) at
   the chosen speed, never drawing a minute beyond the cursor. Pure helpers live in sim_core.js (window.SimCore), the
   chart in sim_chart.js (window.SimChart). With ?simfake=1 the calls go to sim_fake.js (demo data, not market data). */
(() => {
"use strict";
const OL = window.OneLot, C = window.SimCore, SC = window.SimChart;
if (!OL || !C || !SC) return;
const { $, store, toast, api } = OL;
const cssv = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const errText = (e) => String((e && e.message) || "no answer").replace(/\.\s*$/, "");
const NO_HISTORY = "No stored history for this instrument yet: run the backfill on the Data tab, or use Live";

/* ── state ──────────────────────────────────────────────────────────── */
const VIEW0 = { ctf: "3m", speed: 10, smooth: true, pauseOnAlert: true, slowInTrade: true, autoNextDay: false, hideDate: false, sound: true, panel: null, ptab: "trades" };
const S = {
  fake: null, opts: null, offline: false, booted: false, chart: null,
  days: new Map(),                                   // instrument -> its stored days (or a promise of them)
  choice: Object.assign({ instrument: null, strategy: null, timeframe: null, mode: "past", day: null }, store.get("sim.choice", {}) || {}),
  view: Object.assign({}, VIEW0, store.get("sim.view", {}) || {}),
  req: 0, ac: null, tfReq: 0,                        // the newest choice (a late answer never overwrites it)
  meta: null, empty: false, loading: false, error: null, hidden: false,
  startIso: null, drawn: null,                       // where the next session starts; the start of the last minute drawn
  sess: null, starting: false, acting: false, moving: false, want: null, poll: null, build: null,
  chunks: [], inflight: null, owe: 0, frac: 0, hold: false, playing: false, pausedByAlert: false, lastFrame: 0, liveAcc: 0,
  alert: null, trade: null, lastClosed: null, summary: null, trades: [], allTrades: null, tview: "session",
  events: [], markers: [], overlays: {}, mark: null, exitSignal: false, dayOver: null, over: false,
  resume: null, hover: false, flagTimer: 0, note: "", audio: null,
};
const saveChoice = () => store.set("sim.choice", S.choice);
const saveView = () => store.set("sim.view", S.view);
const instOf = (key) => ((S.opts && S.opts.instruments) || []).find(i => i.key === key) || null;
const stratOf = (name) => ((S.opts && S.opts.strategies) || []).find(s => s.name === name) || null;
const chartTfs = () => (S.opts && S.opts.chart_timeframes) || C.CHART_TFS;
const speeds = () => (S.opts && S.opts.speeds) || C.SPEEDS;
const ctfm = () => C.tfMinutes(S.view.ctf);
const live = () => S.choice.mode === "live";
const tradeOpen = () => !!(S.trade && S.trade.status === "OPEN");
const sessReady = () => !!(S.sess && (S.sess.status === "RUNNING" || S.sess.status === "DAY_END"));
const force = () => C.speedInForce(S.view.speed, !!S.trade, S.view.slowInTrade);
const phone = () => window.innerWidth < 700;

/* ── calls ──────────────────────────────────────────────────────────── */
async function getJson(url, signal) {   // reads are plain fetches: a missing engine must not toast
  if (S.fake) return S.fake.handle(url);
  const r = await fetch(url, { signal });
  let j = null;
  try { j = await r.json(); } catch (e) {}
  if (!r.ok) { const e = new Error((j && j.detail) || String(r.status)); e.status = r.status; throw e; }
  return j;
}
async function post(url, body) {
  if (S.fake) return S.fake.handle(url, body || {});
  return api(url, body || {});
}
const q = (o) => Object.entries(o).filter(([, v]) => v !== null && v !== undefined && v !== "").map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
function daysOf(key) {
  if (!S.days.has(key)) {
    S.days.set(key, getJson(`/api/sim/days?${q({ instrument: key })}`).then(r => { const d = (r && r.days) || []; S.days.set(key, d); return d; })
      .catch(() => { S.days.delete(key); return []; }));
  }
  const d = S.days.get(key);
  return Array.isArray(d) ? Promise.resolve(d) : d;
}
const daysNow = (key) => { const d = S.days.get(key); return Array.isArray(d) ? d : null; };

/* ── small view helpers ─────────────────────────────────────────────── */
const set = (id, text) => { const el = $(id); if (el && el.textContent !== text) el.textContent = text; };
const show = (id, on) => { const el = $(id); if (el && el.hidden === !!on) el.hidden = !on; };
const setTone = (id, t) => { const el = $(id); if (el) { el.classList.toggle("pos", t === "pos"); el.classList.toggle("neg", t === "neg"); } };
function disable(id, why) {
  const el = $(id);
  if (!el) return;
  el.disabled = !!why;
  if (why) { if (!el.dataset.tip) el.dataset.tip = el.title || ""; el.title = why; } else if (el.dataset.tip !== undefined) { el.title = el.dataset.tip; delete el.dataset.tip; }
}
function status(text) { S.note = text || ""; set("sim-status", S.note); }
function flag(text, kind, ms) {
  clearTimeout(S.flagTimer);
  const el = $("sim-flag");
  if (!text) { el.hidden = true; return; }
  el.textContent = text; el.className = "sim-flag " + (kind || ""); el.hidden = false;
  if (ms) S.flagTimer = setTimeout(() => { if (!(S.alert && !S.trade)) el.hidden = true; else renderFlag(); }, ms);
}
function veil(text, sub, kind) {
  const v = $("sim-veil");
  if (!text) { v.hidden = true; return; }
  set("sim-veil-text", text); set("sim-veil-sub", sub || "");
  v.className = "sim-veil" + (kind === "err" ? " err" : "");
  show("sim-veil-spin", kind === "load");
  show("sim-retry", kind === "err");
  v.hidden = false;
}
function beep() {
  if (!S.view.sound) return;
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    S.audio = S.audio || new AC();
    [0, 0.22].forEach(d => {
      const o = S.audio.createOscillator(), g = S.audio.createGain(), t0 = S.audio.currentTime + d;
      o.frequency.value = 880; g.gain.setValueAtTime(0.18, t0); g.gain.exponentialRampToValueAtTime(0.001, t0 + 0.18);
      o.connect(g); g.connect(S.audio.destination); o.start(t0); o.stop(t0 + 0.2);
    });
  } catch (e) {}
}
/* An in-page question in plain words (Esc or No: false). Opening it pauses play. */
function confirm(text, yes = "Yes", no = "No") {
  setPlaying(false);
  return new Promise((resolve) => {
    const box = $("sim-ask"), y = $("sim-ask-yes"), n = $("sim-ask-no");
    set("sim-ask-text", text); y.textContent = yes; n.textContent = no; box.hidden = false; y.focus();
    const done = (v) => { box.hidden = true; y.onclick = n.onclick = null; S.answer = null; resolve(v); };
    y.onclick = () => done(true); n.onclick = () => done(false); S.answer = done;
  });
}

/* ── the workspace's size ───────────────────────────────────────────── */
function fitRoot() {
  const root = $("sim-root");
  if (!root || !OL.tabShown("sim")) return;
  if (document.fullscreenElement === root || phone()) { root.style.removeProperty("--sim-h"); return; }
  const top = root.getBoundingClientRect().top + window.scrollY, h = Math.max(420, Math.floor(window.innerHeight - top - 6));
  root.style.setProperty("--sim-h", h + "px");
  // the open panel gives way so the chart keeps its height (at least 530px wide-screen, 390px with the dock as a row)
  const wide = window.innerWidth >= 1200, strip = $("sim-strip").offsetHeight;
  root.style.setProperty("--panel-h", Math.max(128, Math.min(212, h - strip - (wide ? 16 + 530 : 15 + 100 + 390))) + "px");
}
function panelOpen() { return S.view.panel === null ? window.innerHeight >= 800 : !!S.view.panel; }
function renderPanelOpen() {
  const open = panelOpen();
  $("sim-root").dataset.panel = open ? "open" : "closed";
  $("sim-panel-toggle").setAttribute("aria-expanded", String(open));
}

/* ── the strip: choices ─────────────────────────────────────────────── */
const optHtml = (items, sel) => items.map(([v, l, dis]) => `<option value="${C.esc(v)}"${String(v) === String(sel) ? " selected" : ""}${dis ? " disabled" : ""}>${C.esc(l)}</option>`).join("");
/* Make the choice whole: an instrument, a strategy that fits it, one of its timeframes (the options' defaults first). */
function settleChoice() {
  const o = S.opts, c = S.choice, d = (o && o.defaults) || {};
  if (!o) return;
  const insts = o.instruments || [];
  const inst = instOf(c.instrument) || instOf(d.instrument) || insts[0];
  if (!inst) return;
  c.instrument = inst.key;
  const strats = C.strategiesFor(o, inst);
  const strat = strats.find(s => s.name === c.strategy) || strats.find(s => s.name === d.strategy) || strats[0];
  c.strategy = strat ? strat.name : null;
  const tfs = strat ? strat.timeframes || [] : [];
  c.timeframe = tfs.includes(c.timeframe) ? c.timeframe : (strat ? (tfs.includes(d.timeframe) ? d.timeframe : strat.default_timeframe || tfs[0]) : null);
  if (c.mode === "live" && !inst.live) c.mode = "past";
}
function renderChoice() {
  const o = S.opts, c = S.choice;
  if (!o) return;
  const inst = instOf(c.instrument);
  $("sim-inst").innerHTML = optHtml((o.instruments || []).map(i => [i.key, i.label + (i.trade === false ? " (training only)" : "")]), c.instrument);
  const strats = C.strategiesFor(o, inst);
  $("sim-strat").innerHTML = strats.length ? optHtml(strats.map(s => [s.name, s.label]), c.strategy) : `<option value="">No strategy fits</option>`;
  const strat = stratOf(c.strategy);
  $("sim-tf").innerHTML = optHtml(((strat && strat.timeframes) || []).map(t => [t, t]), c.timeframe);
  $("sim-strat").title = strat ? `${strat.label}: ${strat.description || ""}` : "No strategy fits this instrument yet.";
  renderDay();
  renderMode();
}
function renderDay() {
  const c = S.choice, days = daysNow(c.instrument), hidden = S.hidden;
  set("sim-day", live() ? "Live today" : (hidden ? "A hidden day" : (c.day ? C.dayText(c.day, false) : "–")));
  $("sim-day").title = live() ? "Live practice follows today" : (hidden ? "The date shows when the day is over" : (c.day ? `Replaying ${C.dayText(c.day)}: choose another day` : "Choose the day to replay"));
  const i = days && c.day ? days.indexOf(c.day) : -1;
  disable("sim-day-prev", live() ? "Live practice follows today" : (!days || i <= 0 ? "No stored day before this one" : null));
  disable("sim-day-next", live() ? "Live practice follows today" : (!days || i < 0 || i >= days.length - 1 ? "No stored day after this one" : null));
  disable("sim-day-rand", live() ? "Live practice follows today" : (!days || !days.length ? "No stored days for this instrument" : null));
  disable("sim-day", live() ? "Live practice follows today" : (!days || !days.length ? "No stored days for this instrument" : null));
  const inp = $("sim-day-input");
  if (days && days.length) { inp.min = days[0]; inp.max = days[days.length - 1]; }
  inp.value = c.day || "";
}
function renderMode() {
  const inst = instOf(S.choice.instrument), lv = live();
  $("sim-mode-past").setAttribute("aria-pressed", String(!lv));
  $("sim-mode-live").setAttribute("aria-pressed", String(lv));
  disable("sim-mode-live", inst && !inst.live ? "The running desk has no live feed for this instrument" : null);
}
function buildSpeeds() {
  const sp = speeds();
  $("sim-speeds").innerHTML = sp.map(v => `<button type="button" data-speed="${v}" aria-pressed="false" title="${v === 1 ? "Real time: one market minute a minute" : `${v} market minutes a minute`} (↑ ↓)">${v}x</button>`).join("");
  $("sim-speed-sel").innerHTML = optHtml(sp.map(v => [v, v + "x"]), S.view.speed);
  const tfs = chartTfs();
  $("sim-ctfs").innerHTML = tfs.map(t => `<button type="button" data-ctf="${C.esc(t)}" aria-pressed="false" title="Chart timeframe ${C.esc(t)} ([ and ])">${C.esc(t)}</button>`).join("");
  $("sim-ctf").innerHTML = optHtml(tfs.map(t => [t, "Chart " + t]), S.view.ctf);
  const keys = $("sim-keys-list");
  keys.innerHTML = C.KEYS.map(([k, w]) => `<dt>${k.split(" ").map(p => /^[+]$/.test(p) ? " + " : `<kbd>${C.esc(p)}</kbd>`).join("")}</dt><dd>${C.esc(w)}</dd>`).join("");
}
function renderSpeeds() {
  const f = force(), lv = live();
  $("sim-speeds").querySelectorAll("button").forEach(b => {
    const v = Number(b.dataset.speed);
    b.setAttribute("aria-pressed", String(v === f));
    b.classList.toggle("chosen", v === S.view.speed);
    b.disabled = lv;
    b.title = lv ? "Live practice runs at the market's own pace" : (v === 1 ? "Real time: one market minute a minute (↑ ↓)" : `${v} market minutes a minute (↑ ↓)`);
  });
  const sel = $("sim-speed-sel");
  if (String(sel.value) !== String(S.view.speed)) sel.value = String(S.view.speed);
  sel.disabled = lv;
  $("sim-ctfs").querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.ctf === S.view.ctf)));
  if ($("sim-ctf").value !== S.view.ctf) $("sim-ctf").value = S.view.ctf;
}

/* ── the strip: play state and the clock ────────────────────────────── */
function clockUnix() {
  const ch = S.chunks[0], bar = ch && ch.bars[ch.i];
  if (bar && S.frac > 0) return bar[0] + S.frac * 60;
  return S.drawn === null ? null : S.drawn + 60;
}
function renderClock() {
  const t = clockUnix();
  set("sim-clock", t === null ? "–" : (phone() ? C.hhmmOf(t) : C.clockText(t, { hidden: S.hidden })));   // a phone has the day in the strip
  $("sim-clock").title = t === null ? "" : (!S.sess && !live() ? C.readyText(t, S.hidden) : "The replay clock (Indian time)");
  set("sim-countdown", t === null ? "" : (C.countdownText(t, ctfm()) ? "closes in " + C.countdownText(t, ctfm()) : ""));
}
function whyNoPlay() {
  if (S.offline) return "The simulator's engine is not running in this build.";
  if (!S.opts) return "The simulator is still loading.";
  if (S.empty) return NO_HISTORY + ".";
  if (!S.choice.strategy) return "No strategy fits this instrument yet.";
  if (S.loading) return "The chart is still loading.";
  if (S.error) return "The chart could not be loaded: press Try again.";
  if (S.over) return "This session has reached the end of the stored data: choose another day.";
  if (live() && !C.marketOpenAt(Date.now())) return "The market is closed: live practice starts at 09:15 on a trading day.";
  return null;
}
function renderPlay() {
  const why = whyNoPlay(), busy = S.starting || S.moving;
  const p = $("sim-play");
  p.classList.toggle("on", S.playing);
  set("sim-play-word", S.playing ? "Pause" : "Play");
  p.setAttribute("aria-label", S.playing ? "Pause" : "Play");
  disable("sim-play", why);
  if (!why) p.title = S.playing ? "Pause (Space)" : "Play (Space)";
  disable("sim-step", why || (live() ? "Live practice runs at the market's own pace" : null));
  disable("sim-next", why || (live() ? "Live practice runs at the market's own pace" : null));
  disable("sim-end", S.sess && !S.over ? null : "No session is running");
  const st = $("sim-state");
  let word = "Paused", k = "";
  if (S.offline) { word = "Engine off"; k = "bad"; }
  else if (S.error) { word = "Error"; k = "bad"; }
  else if (S.starting || (S.sess && S.sess.status === "BUILDING")) { word = "Preparing"; k = "wait"; }
  else if (S.failed && !S.sess) { word = "Not prepared"; k = "bad"; }
  else if (busy) { word = "Moving…"; k = "wait"; }
  else if (S.playing) { word = live() ? "Live" : `Playing ${force()}x` + (force() < S.view.speed ? " (in a trade)" : ""); k = "on"; }
  else if (S.alert && !S.trade && sessReady()) { word = "Alert: your move"; k = "wait"; }
  else if (S.over) word = "Ended";
  else if (S.dayOver) word = "Day over";
  else if (!S.sess) word = live() ? "Ready: live" : "Ready: press Play";
  st.textContent = word; st.className = "sim-state " + k;
  renderSpeeds();
}

/* ── the legend ─────────────────────────────────────────────────────── */
function renderLegend(idx) {
  const ch = S.chart;
  if (!ch) return;
  const n = ch.bars.length, i = idx === undefined || idx === null ? n - 1 : Math.max(0, Math.min(n - 1, idx));
  const c = ch.candleAt(i), prev = ch.candleAt(i - 1), m = S.meta;
  const p = C.legendParts(m ? m.label : "–", S.view.ctf, c, prev, !!(m && m.hasVolume));
  set("sim-lg-head", p.head); set("sim-lg-o", p.o); set("sim-lg-h", p.h); set("sim-lg-l", p.l); set("sim-lg-c", p.c);
  set("sim-lg-chg", p.chg); setTone("sim-lg-chg", p.chgTone); set("sim-lg-vol", p.vol ? "Vol " + p.vol : "");
  const lines = $("sim-lg-lines");
  const plots = S.sess && S.sess.plots || [];
  if (!plots.length) return;
  const on = S.sess && S.view.ctf === S.sess.session.timeframe;
  if (!on) { lines.dataset.mode !== "off" && (lines.textContent = `The strategy's lines show on the ${S.sess.session.timeframe} chart.`); lines.dataset.mode = "off"; return; }
  if (lines.dataset.mode !== "on") {
    lines.innerHTML = plots.map((pl, j) => `<span><i style="--sw:var(${ch.overlayColour(j)})"></i>${C.esc(pl.label)}<b data-plot="${C.esc(pl.id)}">–</b></span>`).join("");
    lines.dataset.mode = "on";
  }
  lines.querySelectorAll("b[data-plot]").forEach(b => { const v = c ? ovValue(b.dataset.plot, c[0]) : null; const tx = v === null ? "–" : C.num(v); if (b.textContent !== tx) b.textContent = tx; });
}
/* The strategy line's value at candle t (the newest point at or before it). */
function ovValue(id, t) {
  const d = S.overlays[id] || [];
  let lo = 0, hi = d.length - 1, ans = null;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (d[m][0] <= t) { ans = d[m][1]; lo = m + 1; } else hi = m - 1; }
  return ans;
}
function resetLegendLines() { const l = $("sim-lg-lines"); l.innerHTML = ""; l.dataset.mode = ""; }

/* ── the chart ──────────────────────────────────────────────────────── */
function ensureChart() {
  if (S.chart) return true;
  const LW = window.LightweightCharts;
  if (!LW) { veil("The chart library is missing (static/vendor).", "", "err"); return false; }
  S.chart = SC.create($("sim-chart"), LW, { cssv, pinBox: $("sim-pins") });
  S.chart.chart.subscribeCrosshairMove((param) => {
    const inside = param && param.point && param.logical !== undefined && param.time !== undefined;
    S.hover = !!inside;
    renderLegend(inside ? Math.round(param.logical) : null);
  });
  S.chart.chart.timeScale().subscribeVisibleLogicalRangeChange((r) => {
    const n = S.chart.bars.length;
    show("sim-tonow", !!(r && n && r.to < n - 1.5));
  });
  return true;
}
/* The present levels, zones and markers on the chart, from the state (an alert's plan or the open trade's). */
function syncChart() {
  if (!S.chart) return;
  const t = tradeOpen() ? S.trade : null, a = !S.trade && S.alert ? S.alert : null;
  if (t) S.chart.setLevels(t, "trade", C.levelLabels(t, "trade"));
  else if (a) S.chart.setLevels(a, "alert", C.levelLabels(a, "alert"));
  else S.chart.setLevels(null);
  S.chart.setZones(t && t.entry_ts ? { minute: C.unixOf(t.entry_ts), entry: t.entry, stop: t.stop, target: t.target } : null);
}
function overlaysOn() { return !!(S.sess && S.view.ctf === S.sess.session.timeframe); }
function drawOverlays() {
  if (!S.chart || !S.sess) return;
  const on = overlaysOn();
  (S.sess.plots || []).forEach(p => S.chart.setOverlay(p.id, on ? S.overlays[p.id] || [] : []));
  S.chart.showOverlays(on);
}

/* ── a choice: cancel what is in flight, veil, draw the new candles, prepare ── */
async function loadPicture() {
  const my = ++S.req;
  if (S.ac) try { S.ac.abort(); } catch (e) {}
  S.ac = typeof AbortController === "function" ? new AbortController() : null;
  if (!ensureChart()) return;
  try { performance.mark("sim-switch-start"); } catch (e) {}
  const c = S.choice, inst = instOf(c.instrument) || { key: c.instrument, label: c.instrument || "–", kind: "INDEX" };
  S.meta = { key: inst.key, label: inst.label, kind: inst.kind, tick: inst.tick, hasVolume: inst.kind !== "INDEX" };
  S.loading = true; S.error = null; S.empty = false; S.failed = null; status("");
  S.chart.reset();
  resetLegendLines(); flag(null); show("sim-build", false);
  veil(`Loading ${inst.label} · ${S.view.ctf} …`, live() ? "Live feed" : (S.hidden ? "A hidden day" : (c.day ? C.dayText(c.day) : "")), "load");
  renderLegend(); renderAll();
  if (inst.first === null && inst.last === null && !live()) {
    S.loading = false; S.empty = true;
    veil(`No stored bars for ${inst.label} yet`, "Run the backfill on the Data tab, or choose another instrument.", "info");
    renderAll(); return;
  }
  const until = !live() && S.startIso ? C.minuteBefore(S.startIso) : null;
  let r;
  try {
    r = await getJson(`/api/sim/candles?${q({ instrument: inst.key, timeframe: S.view.ctf, until, count: 300 })}`, S.ac && S.ac.signal);
  } catch (e) {
    if (my !== S.req || (e && e.name === "AbortError")) return;
    S.loading = false; S.error = errText(e);
    veil("The chart could not be loaded", `The engine said: ${S.error}`, "err");
    renderAll(); return;
  }
  if (my !== S.req) return;
  S.loading = false;
  const meta = r.instrument || {};
  S.meta = { ...S.meta, label: meta.label || S.meta.label, tick: meta.tick || S.meta.tick, kind: meta.kind || S.meta.kind };
  S.meta.hasVolume = (r.candles || []).some(x => x[5] > 0);   // an index has no volume
  if (!(r.candles || []).length && !r.forming) {
    S.empty = true;
    veil(`No stored bars for ${S.meta.label} yet`, "Run the backfill on the Data tab, or choose another instrument.", "info");
    renderAll(); return;
  }
  S.chart.load(S.meta, S.view.ctf, ctfm(), r.candles || [], r.forming || null);
  S.drawn = until ? C.unixOf(S.startIso) - 60 : (r.until ? C.unixOf(r.until) : null);
  veil(null);
  try { performance.mark("sim-switch-drawn"); performance.measure("sim-switch", "sim-switch-start", "sim-switch-drawn"); } catch (e) {}
  if (r.note) status(r.note);
  renderLegend(); renderAll();
}
function prepare() {
  const c = S.choice, my = S.req;
  if (!c.strategy || S.empty || S.error || live()) return;
  const url = `/api/sim/prepare?${q({ instrument: c.instrument, strategy: c.strategy, timeframe: c.timeframe, start: S.startIso })}`;
  let tries = 0;
  const once = () => getJson(url).then((r) => {
    if (my !== S.req || S.sess) return;
    if (r && r.status === "ready") status(`Ready: ${stratOf(c.strategy) ? stratOf(c.strategy).label : c.strategy} · ${c.timeframe} is prepared. Press Play.`);
    else if (r && r.status === "building" && ++tries < 30) { status("Preparing the strategy in the background…"); setTimeout(once, 1000); }
  }).catch(() => {});
  once();
}
/* Throw the session away on the page (the server's is ended separately). Everything it drew goes with the chart's reset. */
function dropSession() {
  stopPoll();
  S.sess = null; S.chunks = []; S.inflight = null; S.owe = 0; S.frac = 0; S.playing = false; S.pausedByAlert = false; S.want = null;
  S.alert = null; S.trade = null; S.lastClosed = null; S.summary = null; S.trades = []; S.allTrades = null; S.tview = "session";
  S.events = []; S.markers = []; S.overlays = {}; S.mark = null; S.exitSignal = false; S.dayOver = null; S.over = false; S.build = null;
  $("sim-log").innerHTML = ""; renderTrades(); renderNumbers(); renderLogCount();
}
function endOld() {
  const s = S.sess;
  if (!s || S.over) return;
  post(`/api/sim/sessions/${s.id}/end`, {}).catch(() => {});   // never waited for: the switch is instant
}
/* Change the choice. keepClock: an instrument, strategy or timeframe change in the middle of a session starts the new one
   at the old cursor plus one minute. */
async function choose(patch, opts = {}) {
  if (!S.opts) return;
  const mid = S.sess && !S.over && !live() && S.drawn !== null;
  if (S.sess && tradeOpen() && !opts.asked) {
    const t = S.trade;
    const ok = await confirm(`You have an open ${t.action} on ${(S.sess.session.instrument || {}).label || ""}. Changing now closes it at the next minute's open and ends this session. Go ahead?`, "Close it and change", "Keep the trade");
    if (!ok) { renderChoice(); return; }
  }
  const keepClock = mid && opts.keepClock !== false && !patch.day && patch.mode === undefined;
  if (S.sess) endOld();
  dropSession();
  Object.assign(S.choice, patch);
  settleChoice();
  if (!keepClock) { if (opts.random) S.hidden = !!S.view.hideDate; else if (patch.day || patch.instrument || patch.mode) S.hidden = false; }
  if (S.chart) S.chart.hideDates(S.hidden);
  // The switch is instant: the old picture goes and the veil comes in this same task. When the new instrument's days are
  // not known yet they are read IN PARALLEL with the candles (the chosen day is the guess); a day the store lacks is
  // corrected when they arrive (one more candles read, only then).
  const known = keepClock ? null : daysNow(S.choice.instrument);
  const place = (days) => { S.choice.day = C.snapDay(days, S.choice.day); S.startIso = live() ? null : C.startIsoOf(S.choice.day); };
  if (keepClock && S.drawn !== null) { S.startIso = C.isoOf(S.drawn + 60); S.choice.day = S.startIso.slice(0, 10); }
  else if (known) place(known);
  else S.startIso = live() || !S.choice.day ? null : C.startIsoOf(S.choice.day);
  saveChoice(); renderChoice();
  const pic = loadPicture(), my = S.req, guess = S.choice.day;
  if (keepClock || known) { await pic; prepare(); return; }
  const days = await daysOf(S.choice.instrument);
  if (my !== S.req) return;
  place(days);
  saveChoice(); renderDay();
  if (S.choice.day !== guess) await loadPicture(); else await pic;
  prepare();
}

/* ── the session ────────────────────────────────────────────────────── */
function stopPoll() { if (S.poll) { clearTimeout(S.poll); S.poll = null; } }
/* Create the session at the first Play, Step or Next alert; poll while it is being built; then do what was asked. */
async function ensureSession(want) {
  if (sessReady()) return true;
  S.want = want;
  if (S.starting) return;   // a second press while the first create runs does nothing
  if (S.sess && S.sess.status === "BUILDING") return false;
  if (whyNoPlay()) return false;
  const c = S.choice, my = S.req;
  S.starting = true; S.failed = null; renderAll();
  let r;
  try {
    r = await post("/api/sim/sessions", { instrument: c.instrument, strategy: c.strategy, timeframe: c.timeframe, mode: c.mode, start: live() ? null : S.startIso, params: {} });
  } catch (e) {
    S.starting = false;
    if (my === S.req) { status(`The session could not start: ${errText(e)}.`); flag(`The session could not start: ${errText(e)}`, "lost", 6000); }
    renderAll(); return false;
  }
  S.starting = false;
  if (my !== S.req) { if (r && r.session) post(`/api/sim/sessions/${r.session.id}/end`, {}).catch(() => {}); return false; }
  await adopt(r);
  return false;
}
async function adopt(r, resumed) {
  const st = r.state || r, s = r.session || st.session, my = S.req;
  const strat = stratOf(s.strategy);
  S.sess = { id: s.id, session: s, status: s.status, mode: s.mode, plots: (strat && strat.plots) || [] };
  store.set("sim.last", { id: s.id });
  if (s.status === "BUILDING") { showBuild(r.build || st.build); pollBuild(my); renderAll(); return; }
  if (s.status === "FAILED") { failed(st.detail || r.detail); return; }
  await running(st, r, resumed);
}
function showBuild(b) {
  S.build = b || { step: "reading bars", elapsed_ms: 0 };
  const words = { "reading bars": "reading the bars", "building signals": "building the signals", pricing: "pricing the options" }[S.build.step] || S.build.step;
  set("sim-build-text", `Preparing the strategy: ${words} · ${(Number(S.build.elapsed_ms || 0) / 1000).toFixed(1)} s`);
  show("sim-build", true);
  status(`Preparing the strategy: ${words}. Play starts by itself when it is ready.`);
  renderHint();
}
function pollBuild(my) {
  stopPoll();
  S.poll = setTimeout(async () => {
    if (my !== S.req || !S.sess) return;
    let st;
    try { st = await getJson(`/api/sim/sessions/${S.sess.id}`); } catch (e) { if (my === S.req) pollBuild(my); return; }
    if (my !== S.req || !S.sess || st.session.id !== S.sess.id) return;
    S.sess.session = st.session; S.sess.status = st.session.status;
    if (st.session.status === "BUILDING") { showBuild(st.build); pollBuild(my); return; }
    show("sim-build", false);
    if (st.session.status === "FAILED") { failed(st.detail); return; }
    await running(st, st);
  }, 250);
}
function failed(detail) {
  show("sim-build", false);
  S.sess = null; S.want = null;
  S.failed = detail || "The engine gave no reason.";   // the picture stays usable: Play or Try again starts it again
  status("The session could not be prepared: " + errText({ message: S.failed }) + ".");
  renderAll();
}
/* A RUNNING session: take its whole state (resume restores the log and the markers), then do what was asked. */
async function running(st, r, resumed) {
  const s = st.session;
  S.sess.session = s; S.sess.status = s.status;
  show("sim-build", false);
  const cursor = C.unixOf(s.cursor);
  if (S.drawn === null || cursor !== S.drawn) await redrawTo(cursor);
  takeState(st, true);
  const evs = r.events || st.events || [];
  S.events = []; S.markers = []; $("sim-log").innerHTML = "";
  evs.forEach(ev => logEvent(ev, true));
  renderLogCount();
  S.chart.setMarkers(S.markers);
  const ov = r.overlays || st.overlays || {};
  S.overlays = {};
  Object.keys(ov).forEach(k => { S.overlays[k] = (ov[k] || []).filter(p => p[0] + S.sess.session.tf_minutes * 60 <= S.drawn + 60); });
  S.chart.setOverlayDefs(S.sess.plots);
  resetLegendLines(); drawOverlays();
  syncChart();   // a resumed alert or open trade gets its levels and zones back at once
  const marks = r.marks || st.marks || [];
  S.mark = marks.length ? marks[marks.length - 1] : null;
  S.over = s.status === "ENDED";
  if (resumed && !S.trade && !S.alert && S.trades.length) S.lastClosed = S.trades[0];   // the last result, as it was
  if (s.premium_note) set("sim-premium", "Prices in this session: " + s.premium_note + ".");
  status(resumed ? "Resumed where you left off." : (live() ? "Following the live feed." : "The replay is ready."));
  renderAll(); renderLegend();
  const w = S.want; S.want = null;
  if (w) run(w);
}
/* Bring the picture to the server's cursor (a start in a hole of the store, a resume). */
async function redrawTo(cursor) {
  const my = S.req;
  try {
    const r = await getJson(`/api/sim/candles?${q({ instrument: S.choice.instrument, timeframe: S.view.ctf, until: C.isoOf(cursor), count: 300 })}`);
    if (my !== S.req) return;
    S.chart.setCandles(S.view.ctf, ctfm(), r.candles || [], r.forming || null);
    S.chart.startView();
  } catch (e) {}
  S.drawn = cursor;
}
function takeState(st, full) {
  if ("alert" in st) S.alert = st.alert || null;
  if ("trade" in st) {
    const t = st.trade || null;
    if (t && t.status === "CLOSED") { S.lastClosed = t; S.trade = null; } else S.trade = t;
  }
  if (st.summary) S.summary = st.summary;
  if (st.trades) { S.trades = st.trades; renderTrades(); }
  if (full && S.trade && S.trade.status === "OPEN" && !S.mark) S.mark = null;
  renderNumbers();
}
function run(what) {
  if (what === "play") setPlaying(true);
  else if (what === "step") forward(1, null);
  else if (what === "candle") forward(C.stepMinutes(S.drawn, ctfm()), "alert");
  else if (what === "next") forward(390, "alert");
}

/* ── the replay clock ───────────────────────────────────────────────── */
function setPlaying(on, note) {
  if (on && (whyNoPlay() || S.over)) on = false;
  if (on && !sessReady()) { ensureSession("play"); renderAll(); return; }
  if (on && S.dayOver) { S.dayOver = null; renderDock(); }
  S.playing = !!on; S.pausedByAlert = false; S.owe = 0; S.liveAcc = live() ? 4500 : 0;
  S.lastFrame = performance.now();
  if (on) { flag(null); tick(); }
  if (note) status(note); else if (on) status("");
  renderAll();
}
let raf = 0;
function tick() { if (!raf) raf = requestAnimationFrame(frame); }
function frame(now) {
  raf = 0;
  const dt = Math.min(5000, Math.max(0, now - S.lastFrame)); S.lastFrame = now;   // a slow frame still pays its time (a hidden window pauses)
  if (!S.playing) return;
  if (!OL.tabShown("sim") || document.hidden) { setPlaying(false, "Paused: you left the Simulator."); return; }
  if (live()) { liveTick(dt); tick(); return; }
  if (!S.hold) {
    S.owe += C.playMinutes(dt, force());
    drain();
    maybeFetch();
  }
  renderClock();
  if (!S.hover) renderLegend();
  if (S.playing) tick();
}
/* Reveal what the clock owes, minute by minute, from the queued chunks. Events apply at a chunk's end (rule 8). */
function drain() {
  while (S.owe > 0 && S.playing) {
    const ch = S.chunks[0];
    if (!ch) { if (!S.inflight && !sessReady()) S.owe = 0; return; }
    const bar = ch.bars[ch.i];
    if (!bar) { S.chunks.shift(); applyChunk(ch); continue; }
    const need = 1 - S.frac;
    if (S.owe >= need) {
      S.owe -= need; S.frac = 0; finishMinute(ch, bar);
      if (ch.i >= ch.bars.length) { S.chunks.shift(); applyChunk(ch); }
    } else {
      S.frac += S.owe; S.owe = 0;
      if (S.view.smooth) S.chart.drawMinute(bar, S.frac);
    }
  }
  if (!S.playing) S.owe = 0;
}
function finishMinute(ch, bar) {
  S.chart.drawMinute(bar, 1);
  S.drawn = bar[0]; ch.i += 1;
  const m = ch.marks && ch.marks.get(bar[0]);
  if (m) { S.mark = m; renderPositionLive(); }
}
const undrawn = () => S.chunks.reduce((a, ch) => a + ch.bars.length - ch.i, 0) - S.frac;
function maybeFetch() {
  if (!S.playing || S.inflight || !sessReady() || S.over || S.dayOver) return;
  const last = S.chunks[S.chunks.length - 1];
  if (last && ((last.res.events || []).length || last.res.day_end || last.res.over)) return;   // the user may need to act first
  if (undrawn() * 60 / force() > 0.5) return;
  const busy = !!(S.alert || S.trade);
  fetchChunk(Math.min(30, C.chunkSize(force(), busy) + Math.floor(S.owe)), "event");
}
function fetchChunk(n, until) {
  const id = S.sess.id, my = S.req;
  const p = (async () => {
    let res;
    try { res = await post(`/api/sim/sessions/${id}/advance`, { minutes: Math.max(1, Math.min(390, Math.round(n))), until: until || null }); }
    catch (e) {
      if (my === S.req && S.sess && S.sess.id === id) {
        S.playing = false;
        status(`The engine did not answer: ${errText(e)}. Press Play to try again.`);
        flag("The engine did not answer. Press Play to try again.", "lost", 6000);
        renderAll();
      }
      return null;
    }
    if (my !== S.req || !S.sess || S.sess.id !== id) return null;
    const ch = { bars: res.minutes || [], i: 0, res, marks: new Map((res.marks || []).map(m => [m[0], m])) };
    S.chunks.push(ch);
    return ch;
  })();
  S.inflight = p;
  p.finally(() => { if (S.inflight === p) S.inflight = null; if (S.playing) tick(); });
  return p;
}
/* Draw every queued minute at once and apply the chunks (Step, Next alert, an action: rule 9). */
async function settle() { while (S.inflight) await S.inflight; }
function flushAll() {
  while (S.chunks.length) {
    const ch = S.chunks[0];
    while (ch.i < ch.bars.length) finishMinute(ch, ch.bars[ch.i]);
    S.frac = 0; S.chunks.shift(); applyChunk(ch);
  }
  S.frac = 0;
}
/* Step, Shift+Right and Next alert: up to n minutes, from the queue first, stopping where the user is needed. */
async function forward(n, until) {
  if (S.moving || live()) return;
  if (!sessReady()) { await ensureSession(n === 1 ? "step" : (until === "alert" && n === 390 ? "next" : "candle")); return; }
  if (S.over) return;
  S.moving = true; S.playing = false; S.dayOver = null; status(""); renderAll();
  try {
    await settle();
    let left = n;
    while (left > 0 && S.chunks.length) {
      const ch = S.chunks[0];
      if (ch.i < ch.bars.length) { finishMinute(ch, ch.bars[ch.i]); S.frac = 0; left -= 1; }
      if (ch.i >= ch.bars.length) {
        S.chunks.shift();
        const stop = applyChunk(ch);
        if (stop && until) { left = 0; break; }
      }
    }
    if (left > 0 && sessReady() && !S.over) {
      const ch = await fetchChunk(left, until);
      if (ch) flushAll();
    }
  } finally { S.moving = false; renderAll(); renderLegend(); }
}
/* Apply a chunk's answer: the state after its minutes, its events, the strategy's lines. Returns true when it needs the user. */
function applyChunk(ch) {
  const res = ch.res || {};
  let needs = false, closed = null;
  const before = S.trade;
  takeState(res, false);
  for (const ev of res.events || []) {
    logEvent(ev, false);
    if (ev.kind === "ALERT") {
      needs = true; S.lastClosed = null; S.exitSignal = false; beep();
      if (S.view.pauseOnAlert && S.playing) { S.playing = false; S.pausedByAlert = true; S.owe = 0; status("Paused: an alert to take or skip."); }
      flashCard("sim-ticket");
    } else if (ev.kind === "FILLED") {
      const d = ev.data || {};
      flag(`Filled: in at ₹${C.num(d.entry_price ?? d.entry)}` + (S.trade && C.isOption(S.trade) ? ` (index ${C.num(d.entry)})` : ""), "info", 3500);
    } else if (ev.kind === "EXIT_SIGNAL") { S.exitSignal = true; needs = true; status("The strategy says close this trade."); }
    else if (ev.kind === "CLOSED") { needs = true; S.exitSignal = false; closed = closedTrade(ev, before); }
    else if (ev.kind === "CANCELLED") { needs = true; flag("The trade was cancelled before it filled.", "info", 4000); }
    else if (ev.kind === "ALERT_EXPIRED") status("The alert ran out of time.");
    else if (ev.kind === "DAY_END") needs = true;
    else if (ev.kind === "DATA_END") needs = true;
  }
  if ((res.events || []).length) { S.chart.setMarkers(S.markers); renderLogCount(); }
  if (res.marks && res.marks.length) S.mark = res.marks[res.marks.length - 1];
  if (!tradeOpen()) S.mark = null;
  const ov = res.overlays || {};
  Object.keys(ov).forEach(k => { if ((ov[k] || []).length) { S.overlays[k] = (S.overlays[k] || []).concat(ov[k]); if (overlaysOn()) S.chart.addOverlay(k, ov[k]); } });
  if (closed) {
    S.lastClosed = closed;
    const r = C.resultModel(closed);
    flag(`${r.reason} · ${r.net}` + (r.r !== "–" ? ` · ${r.r}` : ""), closed.net >= 0 ? "won" : "lost", 5000);
    flashCard("sim-res");
    refreshTrades();
  }
  if (res.day_end) {
    const day = S.drawn !== null ? C.isoOf(S.drawn).slice(0, 10) : null;
    if (S.hidden) { S.hidden = false; S.chart.hideDates(false); renderDay(); }
    if (S.view.autoNextDay) status(`${day ? C.dayText(day) : "The day"} is over. Going on to the next day.`);
    else { S.dayOver = day || "today"; S.playing = false; S.owe = 0; status("The trading day is over. Press Play to go on to the next day."); }
    if (day) { S.choice.day = day; saveChoice(); }
  }
  if (res.over) { S.over = true; S.playing = false; status("The stored data has ended. Choose another day to go on practising."); }
  syncChart(); renderAll(); renderLegend();
  return needs;
}
function closedTrade(ev, before) {
  const d = ev.data || {};
  const fromList = (S.trades || []).find(t => String(t.id) === String(d.trade_id));
  if (fromList) return fromList;
  const t = before || {};
  return { ...t, status: "CLOSED", exit: d.exit ?? t.mark, exit_price: d.exit_price ?? t.mark_price, exit_reason: d.exit_reason, net: d.net, r_multiple: d.r_multiple ?? null, exit_ts: ev.ts };
}
function refreshTrades() {
  if (!S.sess) return;
  const id = S.sess.id, my = S.req;
  getJson(`/api/sim/trades?${q({ session_id: id, limit: 200 })}`).then((r) => {
    if (my !== S.req || !S.sess || S.sess.id !== id) return;
    S.trades = (r && r.trades) || S.trades;
    const lc = S.lastClosed && S.trades.find(t => String(t.id) === String(S.lastClosed.id));
    if (lc) S.lastClosed = lc;
    renderTrades(); renderDock(); renderNumbers();
  }).catch(() => {});
}
function liveTick(dt) {
  S.liveAcc += dt;
  if (S.liveAcc < 5000 || S.inflight || !sessReady()) return;
  S.liveAcc = 0;
  fetchChunk(1, null).then((ch) => { if (ch) flushAll(); renderClock(); });
}

/* ── actions: take, skip, close, end ────────────────────────────────── */
async function act(kind) {
  if (!S.sess || S.acting) return;
  if (kind === "take" || kind === "skip") { if (!S.alert || S.trade) return; }
  if (kind === "close" && !S.trade) return;
  if (kind === "end" && !S.over && !(await confirm("End this practice session? An open trade closes at the next minute's open, and the replay of this session stops.", "End it", "Keep going"))) return;
  S.acting = true;
  try { await actNow(kind); } finally { S.acting = false; renderAll(); }
  // A close fills at the next minute's open (SIM-API close). Paused, that minute would never come: reveal it at once.
  if (kind === "close" && tradeOpen() && !S.playing && !S.over && !live()) await forward(1, null);
}
async function actNow(kind) {
  const wasPlaying = S.playing || S.pausedByAlert;
  await settle(); flushAll();   // the action follows the last minute on screen (rule 9)
  if ((kind === "take" || kind === "skip") && !S.alert) { flag("The alert has already ended.", "info", 3000); return; }
  const base = `/api/sim/sessions/${S.sess.id}`, my = S.req;
  let res;
  try {
    if (kind === "take") res = await post(base + "/take", { alert_id: S.alert.id });
    else if (kind === "skip") res = await post(base + "/skip", { alert_id: S.alert.id });
    else if (kind === "close") res = await post(base + "/close", {});
    else if (kind === "end") res = await post(base + "/end", {});
  } catch (e) { flag(`Not done: ${errText(e)}`, "lost", 5000); return; }
  if (my !== S.req) return;
  const ch = { bars: res.minutes || [], i: 0, res, marks: new Map((res.marks || []).map(m => [m[0], m])) };
  while (ch.i < ch.bars.length) finishMinute(ch, ch.bars[ch.i]);
  applyChunk(ch);
  if (kind === "take") { S.lastClosed = null; status("Taken: it fills at the next minute's open."); flag("Taken: it fills at the next minute's open.", "info", 3000); }
  if (kind === "skip") status("Skipped.");
  if (kind === "close") status("Closing at the next minute's open.");
  if (kind === "end") { S.over = true; S.playing = false; status("The session has ended. Change anything above to start a new one."); }
  if ((kind === "take" || kind === "skip") && wasPlaying && !S.over) setPlaying(true);
  renderAll();
}

/* ── the dock ───────────────────────────────────────────────────────── */
function flashCard(id) { const el = $(id); if (!el) return; el.classList.remove("sim-flash"); void el.offsetWidth; el.classList.add("sim-flash"); }
function renderHint(h) {
  const c = S.choice, strat = stratOf(c.strategy), inst = instOf(c.instrument);
  if (!h) {
    if (S.offline) h = { tag: "Engine off", title: "The simulator's engine is not running in this build.", text: "The rest of the desk works as usual." };
    else if (!S.opts) h = { tag: "Loading", title: "Loading the simulator…", text: "" };
    else if (S.empty) h = { tag: "No data", title: `No stored bars for ${inst ? inst.label : c.instrument}`, text: NO_HISTORY + "." };
    else if (!c.strategy) h = { tag: "No rule", title: "No strategy fits this instrument yet.", text: "Choose another instrument." };
    else if (S.error) h = { tag: "Error", title: "The chart could not be loaded", text: S.error, btns: [["retry", "Try again", "primary"]] };
    else if (S.loading) h = { tag: "Loading", title: `Loading ${inst ? inst.label : ""} · ${S.view.ctf}`, text: "" };
    else if (S.failed && !S.sess) h = { tag: "Failed", title: "The session could not be prepared", text: S.failed, btns: [["retry-session", "Try again", "primary"]] };
    else if (S.starting || (S.sess && S.sess.status === "BUILDING")) h = { tag: "Preparing", title: "Preparing the strategy", text: S.build ? `Step: ${S.build.step}. Play starts by itself when it is ready.` : "This takes a moment the first time." };
    else if (live() && !C.marketOpenAt(Date.now())) h = { tag: "Closed", title: "The market is closed", text: "Live practice starts at 09:15 on a trading day. Choose Past to practise on a stored day now." };
    else if (S.over) h = { tag: "Ended", title: "This session has ended", text: "Choose another day, instrument or strategy above to start a new one.", btns: [["day-rand", "A random day", ""], ["day-next", "The next day", ""]] };
    else if (!S.sess) h = { tag: "Ready", title: live() ? "Practise on the live feed" : `Replay ${S.hidden ? "a hidden day" : C.dayText(c.day)} from ${S.startIso ? S.startIso.slice(11, 16) : "09:15"}`,
      text: `<p>Press <b>Play</b> or <kbd>Space</kbd>. The chart shows the market one minute at a time; alerts from <b>${C.esc(strat ? strat.label : c.strategy)} · ${C.esc(c.timeframe)}</b> appear here.</p><p><kbd>N</kbd> jumps to the next alert, <kbd>→</kbd> moves one minute.</p>`, html: true };
    else h = { tag: "Watching", title: "No alert yet", text: `<p>Waiting for ${C.esc(strat ? strat.label : c.strategy)} · ${C.esc(c.timeframe)}.</p><p><kbd>N</kbd> jumps to the next alert.</p>`, html: true };
  }
  set("sim-hint-tag", h.tag); set("sim-hint-title", h.title);
  const tx = $("sim-hint-text");
  if (h.html) { if (tx.innerHTML !== h.text) tx.innerHTML = h.text; } else set("sim-hint-text", h.text || "");
  const btns = (h.btns || []).map(([a, w, k]) => `<button type="button" class="sim-btn ${k}" data-act="${a}">${C.esc(w)}</button>`).join("");
  if ($("sim-hint-btns").innerHTML !== btns) $("sim-hint-btns").innerHTML = btns;
}
function fillTicket() {
  const m = C.ticketModel(S.alert, S.drawn === null ? null : C.isoOf(S.drawn));
  if (!m) return;
  set("sim-tk-left", m.left); set("sim-tk-action", m.action); set("sim-tk-what", m.what);
  const src = $("sim-tk-src"); src.textContent = m.source; src.className = "sim-src " + m.source;
  set("sim-tk-buy", m.buy); set("sim-tk-stop", m.stop); set("sim-tk-target", m.target);
  set("sim-tk-risk", m.risk); set("sim-tk-reward", m.reward); set("sim-tk-costs", m.costs); set("sim-tk-ratio", m.ratio);
  set("sim-tk-trig", m.triggers); set("sim-tk-implied", m.implied);
  const ul = $("sim-tk-reasons"), key = S.alert.id;
  if (ul.dataset.alert !== key) { ul.dataset.alert = key; ul.innerHTML = ""; m.reasons.forEach(r => { const li = document.createElement("li"); li.textContent = r; ul.appendChild(li); }); }
}
function renderPositionLive() {
  if (!S.trade || $("sim-pos").hidden) return;
  const m = C.positionModel(S.trade, S.trade.status === "OPEN" ? S.mark : null, S.exitSignal);
  if (!m) return;
  set("sim-ps-action", m.action); set("sim-ps-what", m.what);
  const src = $("sim-ps-src"); src.textContent = m.source; src.className = "sim-src " + m.source;
  show("sim-ps-pending", m.pending); show("sim-ps-live", !m.pending);
  set("sim-ps-tag", m.pending ? "Waiting for the fill" : "Open trade");
  if (m.pending) { set("sim-ps-mins", ""); return; }
  set("sim-ps-pnl", m.pnl); setTone("sim-ps-pnl", m.pnlTone); set("sim-ps-pct", m.pct); set("sim-ps-r", m.r);
  set("sim-ps-inout", m.inOut); set("sim-ps-index", m.index); set("sim-ps-stage", m.stage);
  set("sim-ps-stop", m.stop); set("sim-ps-target", m.target); set("sim-ps-mins", m.minutes); set("sim-ps-way", m.way);
  const at = (id, pct) => { const el = $(id); el.hidden = pct === null; if (pct !== null) el.style.left = pct.toFixed(1) + "%"; };
  at("sim-ps-mprice", m.bar.price); at("sim-ps-mstop", m.bar.stop); at("sim-ps-mentry", m.bar.entry);
  at("sim-ps-t25", m.bar.ticks[0]); at("sim-ps-t50", m.bar.ticks[1]); at("sim-ps-t80", m.bar.ticks[2]);
  show("sim-ps-exit", !!m.exitSignal);
  if (phone()) renderActbar();
}
function fillResult() {
  const r = C.resultModel(S.lastClosed);
  if (!r) return;
  $("sim-res").className = "sim-card sim-res " + (r.won ? "won" : "lost");
  const tag = $("sim-rs-tag"); tag.textContent = r.won ? "Won" : "Lost"; tag.className = "sim-tag " + (r.won ? "won" : "lost");
  set("sim-rs-r", r.r); set("sim-rs-reason", r.reason); set("sim-rs-net", r.net); setTone("sim-rs-net", r.netTone);
  set("sim-rs-charges", r.charges); set("sim-rs-what", `${r.action} ${r.what}`); set("sim-rs-inout", r.inOut); set("sim-rs-rule", r.rule);
}
function fillDay() {
  const day = S.dayOver, list = (S.trades || []).filter(t => String(t.exit_ts || "").slice(0, 10) === day);
  const net = list.reduce((a, t) => a + (Number(t.net) || 0), 0);
  set("sim-dy-title", `${day && day !== "today" ? C.dayText(day) : "The day"} is over`);
  set("sim-dy-net", C.signedMoney(net)); setTone("sim-dy-net", C.tone(net));
  set("sim-dy-trades", list.length ? `${list.length} trade${list.length === 1 ? "" : "s"} that day` : "no trades that day");
}
function renderDock() {
  const resume = !!S.resume && !S.sess;
  const ticket = !resume && sessReady() && !!S.alert && !S.trade;
  const pos = !resume && !!S.trade;
  const day = !resume && !ticket && !pos && !!S.dayOver;
  const res = !resume && !ticket && !pos && !day && !!S.lastClosed;
  show("sim-ticket", ticket); show("sim-pos", pos); show("sim-dayc", day); show("sim-res", res);
  show("sim-hint", resume || (!ticket && !pos && !day && !res));
  if (resume) renderHint({ tag: "Welcome back", title: "Continue your last session?", text: C.resumeLine(S.resume.session), btns: [["resume", "Continue", "primary"], ["dismiss", "No thanks", ""]] });
  else if (ticket) fillTicket();
  else if (pos) renderPositionLive();
  else if (day) fillDay();
  else if (res) fillResult();
  else renderHint();
  renderFlag();
  renderActbar();
}
function renderFlag() {
  const el = $("sim-flag");
  if (sessReady() && S.alert && !S.trade) {
    clearTimeout(S.flagTimer);
    el.textContent = `Alert: ${S.alert.action} · take it (T) or skip it (S)`; el.className = "sim-flag"; el.hidden = false;
  } else if (el.className === "sim-flag" && !el.hidden) el.hidden = true;
}
function renderActbar() {
  const bar = $("sim-actbar");
  if (!phone()) { bar.hidden = true; return; }
  const a = sessReady() && S.alert && !S.trade, t = !!S.trade;
  bar.hidden = !(a || t);
  show("sim-ab-take", a); show("sim-ab-skip", a); show("sim-ab-close", t);
  if (a) set("sim-ab-text", `${S.alert.action} · risk ${C.money(S.alert.risk_inr)}`);
  else if (t) { const m = C.positionModel(S.trade, S.mark, false); set("sim-ab-text", m.pending ? "Waiting for the fill" : `${m.pnl} · ${m.r}`); }
}
function renderNumbers() {
  const s = S.summary || {}, cap = S.sess && S.sess.session && S.sess.session.capital;
  set("sim-n-trades", String(s.trades || 0));
  set("sim-n-win", s.trades ? `${s.winners || 0} of ${s.trades}` : "–");
  set("sim-n-net", C.signedMoney(s.net || 0)); setTone("sim-n-net", C.tone(s.net));
  set("sim-n-eq", C.money(s.equity ?? cap));
  const grid = C.numbersModel(s, cap).map(([l, v, t]) => `<div><span>${C.esc(l)}</span><b class="${t}">${C.esc(v)}</b></div>`).join("");
  if ($("sim-numgrid").innerHTML !== grid) $("sim-numgrid").innerHTML = grid;
  const nets = (S.trades || []).slice().reverse().map(t => t.net);
  $("sim-equity-path").setAttribute("d", C.equityPath(nets, cap || 100000, 600, 90));
}

/* ── the bottom panel ───────────────────────────────────────────────── */
function renderTrades() {
  const all = S.tview === "all", rows = all ? (S.allTrades || []) : (S.trades || []);
  const html = rows.map((t, i) => {
    const c = C.tradeCells(t), mine = !all || (S.sess && t.session_id === S.sess.id);
    return `<tr data-i="${i}" data-ts="${C.esc(t.entry_ts || "")}" class="${mine ? "" : "other"}" title="${mine ? "Show this trade on the chart" : "From another session"}">`
      + `<td>${C.esc(c.when)}<small>${C.esc(all ? `${c.day} · ${t.instrument || ""}` : c.day)}</small></td>`
      + `<td class="w">${C.esc(c.what)}<small>${C.esc(c.source)}</small></td>`
      + `<td class="n">${C.esc(c.price)}</td><td class="n">${C.esc(c.index || "–")}</td>`
      + `<td class="w">${C.esc(c.reason)}</td><td class="n ${c.netTone}">${C.esc(c.net)}</td><td class="n">${C.esc(c.r)}</td></tr>`;
  }).join("");
  $("sim-trades").innerHTML = html;
  show("sim-trades-empty", !rows.length);
  set("sim-trades-empty", all ? (S.allTrades === null ? "Loading…" : "No simulator trades yet.") : "No closed trades in this session yet.");
  set("sim-pt-trades-n", (S.trades || []).length ? String(S.trades.length) : "");
  $("sim-view-session").setAttribute("aria-pressed", String(!all));
  $("sim-view-all").setAttribute("aria-pressed", String(all));
}
/* One event into the log (appended at its own minute; the list is never rebuilt) and its marker. */
function logEvent(ev, restoring) {
  S.events.push(ev);
  const side = (ev.data && ev.data.side) || (S.trade && S.trade.side) || (S.alert && S.alert.side) || "LONG";
  const mk = C.markerFor(ev, side);
  if (mk) S.markers.push(mk);
  const c = C.eventCells(ev), li = document.createElement("li"), list = $("sim-log");
  li.className = c.kind;
  const a = document.createElement("span"); a.textContent = `${c.time} · ${c.day}`;
  const b = document.createElement("b"); b.textContent = c.word;
  const t = document.createElement("span"); t.textContent = c.text;
  li.append(a, b, t);
  const pane = list.parentElement, atEnd = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 30;
  list.appendChild(li);
  while (list.children.length > 500) list.removeChild(list.firstChild);
  if (!restoring && atEnd && !$("sim-p-log").hidden) pane.scrollTop = pane.scrollHeight;
}
function renderLogCount() {
  set("sim-pt-log-n", S.events.length ? String(S.events.length) : "");
  show("sim-log-empty", !S.events.length);
}
function showPanelTab(name, open) {
  S.view.ptab = name; saveView();
  ["trades", "log", "numbers", "how"].forEach(t => {
    $("sim-pt-" + t).setAttribute("aria-selected", String(t === name));
    $("sim-p-" + t).hidden = t !== name;
  });
  if (open && !panelOpen()) { S.view.panel = true; saveView(); renderPanelOpen(); fitRoot(); }
  if (name === "log") { const pane = $("sim-log").parentElement; pane.scrollTop = pane.scrollHeight; }
}
async function showTradesView(v) {
  S.tview = v;
  if (v === "all") {
    S.allTrades = null; renderTrades();
    try { S.allTrades = (await getJson("/api/sim/trades?limit=200")).trades || []; } catch (e) { S.allTrades = []; }
  }
  renderTrades();
}

/* ── everything that depends on the state, not per frame ────────────── */
function renderAll() {
  renderPlay(); renderClock(); renderDock(); renderDay();
  disable("sim-inst", S.offline ? "The engine is not running" : null);
}

/* ── the chart timeframe (a view only: the session, the clock and the trade stay) ── */
async function setChartTf(tf) {
  if (!tf || tf === S.view.ctf) return;
  S.view.ctf = tf; saveView(); renderSpeeds();
  if (!S.chart || S.empty) return;
  const my = ++S.tfReq, req = S.req;
  if (S.drawn === null) { await loadPicture(); return; }
  S.hold = true;
  set("sim-build-text", `Loading the ${tf} candles…`); show("sim-build", true);
  try {
    const r = await getJson(`/api/sim/candles?${q({ instrument: S.choice.instrument, timeframe: tf, until: C.isoOf(S.drawn), count: 300 })}`);
    if (my !== S.tfReq || req !== S.req) return;
    S.chart.setCandles(tf, C.tfMinutes(tf), r.candles || [], r.forming || null);
    S.chart.startView();
    drawOverlays(); resetLegendLines(); syncChart();
  } catch (e) {
    if (my === S.tfReq) flag(`The ${tf} candles could not be loaded.`, "lost", 4000);
  } finally {
    if (my === S.tfReq) { S.hold = false; show("sim-build", S.sess && S.sess.status === "BUILDING"); S.lastFrame = performance.now(); }
  }
  renderLegend(); renderClock();
}

/* ── start-up ───────────────────────────────────────────────────────── */
async function loadTab() {
  fitRoot(); renderPanelOpen();
  if (S.booted) { renderAll(); if (S.chart && !S.hover) renderLegend(); return; }
  S.booted = true;
  ensureChart();
  window.SimPage = { chart: () => S.chart, state: () => S };   // for the browser checks: read the chart and the state
  if (S.fake) { set("sim-banner", "Demo data, not market data"); show("sim-banner", true); Object.assign(window.SimPage, { forget() { dropSession(); S.chart.reset(); renderAll(); }, offerResume }); }   // a page that lost its session, then the offer
  buildSpeeds(); renderSpeeds(); showPanelTab(S.view.ptab || "trades"); renderPanelOpen();
  setChecks();
  renderAll();
  // never a blank page: the stored choice draws at once while the options and the days load
  const c = S.choice;
  if (c.instrument && c.day && !live()) { S.startIso = C.startIsoOf(c.day); loadPicture(); }
  try {
    S.opts = await getJson("/api/sim/options");
    if (!S.opts || !S.opts.instruments) throw new Error("no options");
  } catch (e) {
    S.opts = null;
    if (e && (e.status === 404 || e.message === "404")) { S.offline = true; veil("The simulator's engine is not running in this build.", "The rest of the desk works as usual.", "info"); }
    else veil("The simulator is not answering yet", "The desk may still be starting. Open this tab again to retry.", "err");
    S.booted = !!S.offline;   // a failure other than 404 may pass: the next visit tries again
    renderAll(); return;
  }
  buildSpeeds(); renderSpeeds();
  settleChoice();
  const days = await daysOf(S.choice.instrument);
  const want = S.choice.day && days.includes(S.choice.day) ? S.choice.day : C.defaultDay(days);
  const drew = S.startIso && S.choice.day === want && S.choice.instrument === c.instrument;
  S.choice.day = want;
  if (!live()) S.startIso = C.startIsoOf(want); else S.startIso = null;
  saveChoice(); renderChoice();
  if (!drew) await loadPicture(); else { S.meta = { ...S.meta, ...(instOf(S.choice.instrument) || {}) }; renderAll(); }
  prepare();
  offerResume();
}
async function offerResume() {
  const last = store.get("sim.last", null);
  if (!last || !last.id || S.sess) return;
  let st;
  try { st = await getJson(`/api/sim/sessions/${encodeURIComponent(last.id)}`); } catch (e) {
    if (e && (e.status === 404 || e.message === "404")) store.set("sim.last", null);   // a database that no longer has it: stop asking
    return;
  }
  if (!st || !st.session || /^(ENDED|FAILED)$/.test(st.session.status) || S.sess) return;
  const strat = stratOf(st.session.strategy);
  S.resume = { ...st, session: { ...st.session, strategy_label: strat ? strat.label : st.session.strategy } };
  renderDock();
}
async function resume() {
  const st = S.resume;
  if (!st) return;
  S.resume = null;
  const s = st.session, cursor = C.unixOf(s.cursor);
  dropSession();
  Object.assign(S.choice, { instrument: s.instrument.key, strategy: s.strategy, timeframe: s.timeframe, mode: s.mode });
  settleChoice();
  S.hidden = false;
  S.startIso = s.mode === "live" ? null : C.isoOf(cursor + 60);
  S.choice.day = S.startIso ? S.startIso.slice(0, 10) : S.choice.day;
  await daysOf(S.choice.instrument);
  saveChoice(); renderChoice();
  await loadPicture();
  if (s.mode !== "live") S.drawn = cursor;
  await adopt(st, true);
}
function setChecks() {
  const v = S.view;
  $("sim-o-smooth").checked = v.smooth; $("sim-o-pause").checked = v.pauseOnAlert; $("sim-o-slow").checked = v.slowInTrade;
  $("sim-o-nextday").checked = v.autoNextDay; $("sim-o-hide").checked = v.hideDate; $("sim-o-sound").checked = v.sound;
}
function openSheet(id) {
  ["sim-settings", "sim-keys"].forEach(x => { if (x !== id) $(x).hidden = true; });
  const el = $(id), open = el.hidden;
  el.hidden = !open;
  $("sim-gear").setAttribute("aria-expanded", String(id === "sim-settings" && open));
  if (open) setPlaying(false, "Paused while a sheet is open.");
}
function closeSheets() {
  let closed = false;
  ["sim-settings", "sim-keys"].forEach(x => { if (!$(x).hidden) { $(x).hidden = true; closed = true; } });
  $("sim-gear").setAttribute("aria-expanded", "false");
  if (S.answer) { S.answer(false); closed = true; }
  return closed;
}
function speedTo(v) {
  if (!Number.isFinite(v) || live()) return;
  S.view.speed = v; saveView(); renderPlay();
}
function dayMove(kind) {
  const days = daysNow(S.choice.instrument);
  if (!days || !days.length || live()) return;
  if (kind === "day-prev") { const d = C.shiftDay(days, S.choice.day, -1); if (d) choose({ day: d }); }
  else if (kind === "day-next") { const d = C.shiftDay(days, S.choice.day, 1); if (d) choose({ day: d }); }
  else if (kind === "day-rand") choose({ day: C.randomDay(days, S.choice.day) }, { random: true });
}
function dispatch(a) {
  if (a === "play") { if (S.dayOver) { S.dayOver = null; setPlaying(true); } else setPlaying(!S.playing); }
  else if (a === "step") forward(1, null);
  else if (a === "candle") forward(S.drawn === null ? ctfm() : C.stepMinutes(S.drawn, ctfm()), "alert");
  else if (a === "next") forward(390, "alert");
  else if (a === "nextday") { S.dayOver = null; setPlaying(true); }
  else if (a === "take" || a === "skip" || a === "close" || a === "end") act(a);
  else if (a === "fit") { if (S.chart) S.chart.fit(); }
  else if (a === "tonow") { if (S.chart) S.chart.toNow(); }
  else if (a === "settings") openSheet("sim-settings");
  else if (a === "help") openSheet("sim-keys");
  else if (a === "sheet-close") closeSheets();
  else if (a === "full") { const r = $("sim-root"); if (document.fullscreenElement) document.exitFullscreen().catch(() => {}); else if (r.requestFullscreen) r.requestFullscreen().catch(() => {}); }
  else if (a === "panel") { S.view.panel = !panelOpen(); saveView(); renderPanelOpen(); fitRoot(); }
  else if (a === "day-prev" || a === "day-next" || a === "day-rand") dayMove(a);
  else if (a === "day-pick") { const inp = $("sim-day-input"); try { inp.showPicker(); } catch (e) { inp.focus(); inp.click(); } }
  else if (a === "mode-past") { if (live()) choose({ mode: "past" }); }
  else if (a === "mode-live") { if (!live()) choose({ mode: "live" }); }
  else if (a === "retry") { if (S.error || S.empty) loadPicture().then(prepare); else loadTab(); }
  else if (a === "retry-session") { renderHint(); ensureSession("play"); }
  else if (a === "view-session") showTradesView("session");
  else if (a === "view-all") showTradesView("all");
  else if (a === "resume") resume();
  else if (a === "dismiss") { S.resume = null; store.set("sim.last", null); renderDock(); }
}
function wire() {
  const root = $("sim-root");
  root.addEventListener("click", (e) => {
    const sp = e.target.closest("[data-speed]");
    if (sp) { speedTo(Number(sp.dataset.speed)); return; }
    const tf = e.target.closest("[data-ctf]");
    if (tf) { setChartTf(tf.dataset.ctf); return; }
    const pt = e.target.closest("[data-ptab]");
    if (pt) { showPanelTab(pt.dataset.ptab, true); return; }
    const row = e.target.closest("#sim-trades tr");
    if (row && !row.classList.contains("other") && row.dataset.ts && S.chart) {
      root.querySelectorAll("#sim-trades tr.sel").forEach(r => r.classList.remove("sel"));
      row.classList.add("sel"); S.chart.scrollTo(C.unixOf(row.dataset.ts)); return;
    }
    const csv = e.target.closest("#sim-csv");
    if (csv && S.fake) {
      e.preventDefault();
      S.fake.handle("/api/sim/trades.csv").then(r => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([r.csv], { type: "text/csv" })); a.download = "sim_trades_demo.csv"; a.click(); });
      return;
    }
    const b = e.target.closest("[data-act]");
    if (b && !b.disabled) dispatch(b.dataset.act);
  });
  root.addEventListener("change", (e) => {
    const id = e.target.id;
    if (id === "sim-inst") choose({ instrument: e.target.value });
    else if (id === "sim-strat") choose({ strategy: e.target.value });
    else if (id === "sim-tf") choose({ timeframe: e.target.value });
    else if (id === "sim-ctf") setChartTf(e.target.value);
    else if (id === "sim-speed-sel") speedTo(Number(e.target.value));
    else if (id === "sim-day-input") { if (e.target.value) choose({ day: e.target.value }); }
    else if (/^sim-o-/.test(id)) {
      const v = S.view;
      v.smooth = $("sim-o-smooth").checked; v.pauseOnAlert = $("sim-o-pause").checked; v.slowInTrade = $("sim-o-slow").checked;
      v.autoNextDay = $("sim-o-nextday").checked; v.hideDate = $("sim-o-hide").checked; v.sound = $("sim-o-sound").checked;
      saveView(); renderPlay();
    }
  });
  document.addEventListener("keydown", (e) => {
    if (!OL.tabShown("sim")) return;
    const t = e.target || {};
    const a = C.keyAction({ key: e.key, code: e.code, shiftKey: e.shiftKey, ctrlKey: e.ctrlKey, metaKey: e.metaKey, altKey: e.altKey, tag: t.tagName, editable: !!t.isContentEditable });
    if (!a) return;
    if (a === "escape") { if (closeSheets()) e.preventDefault(); return; }
    if (!$("sim-ask").hidden) return;
    e.preventDefault();
    if (a === "help") openSheet("sim-keys");
    else if (a === "faster" || a === "slower") speedTo(C.speedStep(speeds(), S.view.speed, a === "faster" ? 1 : -1));
    else if (a === "tf-down" || a === "tf-up") setChartTf(C.tfStep(chartTfs(), S.view.ctf, a === "tf-up" ? 1 : -1));
    else if (a === "panel") dispatch("panel");
    else if (a === "play") { if (!$("sim-play").disabled) dispatch("play"); }
    else if ((a === "step" || a === "candle" || a === "next") && !$("sim-step").disabled) dispatch(a);
    else if (a === "take" || a === "skip" || a === "close" || a === "fit") dispatch(a);
  });
  document.addEventListener("visibilitychange", () => { if (document.hidden && S.playing) setPlaying(false, "Paused while the window was hidden."); });
  window.addEventListener("hashchange", () => { if (!OL.tabShown("sim") && S.playing) setPlaying(false, "Paused: you left the Simulator."); });
  window.addEventListener("resize", () => { requestAnimationFrame(() => { fitRoot(); renderPanelOpen(); renderActbar(); }); });
  document.addEventListener("fullscreenchange", () => requestAnimationFrame(fitRoot));
  if (typeof ResizeObserver === "function") {
    const ro = new ResizeObserver(() => requestAnimationFrame(fitRoot));
    [document.querySelector("header.top"), $("warnings"), root].forEach(el => { if (el) ro.observe(el); });
  }
  new MutationObserver(() => { if (S.chart) { S.chart.theme(); resetLegendLines(); renderLegend(); } }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
}
function boot() {
  if (!$("sim-root")) return;
  wire();
  OL.tabLoaders.sim = () => { loadTab(); };
  if (OL.tabShown("sim")) loadTab();
}
if (/[?&]simfake=1(&|$)/.test(location.search)) {
  const s = document.createElement("script");
  s.src = "/static/sim_fake.js";
  s.onload = () => { S.fake = window.SimFake; boot(); };
  s.onerror = () => { boot(); };
  document.head.appendChild(s);
} else boot();
})();
