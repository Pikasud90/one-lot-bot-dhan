/* The Simulator tab (US-076): bar replay and live practice trading.
   Calls the engine's JSON (.agile/stories/SIM-API.md). The server owns the cursor; this page asks for the next
   minute(s) when its own clock reaches them and builds the candles from the one-minute bars it is given.
   With ?simfake=1 the calls go to src/onelot/ui/sim_fake.js instead (demo data, not market data). */
(() => {
"use strict";
const OL = window.OneLot;
if (!OL) return;
const { $, fmt, cls, esc, hhmm, store, toast, api, table } = OL;
const IST = 19800;                 // seconds: the chart and the clock read Indian time
const DAY_OPEN = 33300;            // 09:15 as seconds into the day
const DAY_MINUTES = 375;           // 09:15 to 15:30

/* ── pure helpers (unit-tested in node) ─────────────────────────────── */
const tfMinutes = (tf) => Math.max(1, (parseInt(tf, 10) || 1) * (/h$/i.test(String(tf)) ? 60 : 1));
const unixOf = (iso) => Math.floor(Date.parse(iso) / 1000);
const money = (v) => (v === null || v === undefined || Number.isNaN(Number(v))) ? "–" : (Number(v) < 0 ? "−" : "") + "₹" + Math.abs(Number(v)).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const minutePath = (b) => b[4] >= b[1] ? [b[1], b[3], b[2], b[4]] : [b[1], b[2], b[3], b[4]];
const minuteSeconds = (speed) => 60 / Math.max(1, speed);
const minutesToAsk = (speed, owedSeconds) => speed <= 10 ? 1 : Math.max(0, Math.min(390, Math.floor(owedSeconds / 60)));
const sourceWord = (s) => s === "real" ? "real" : "modelled";
const sideWord = (side) => side === "SHORT" ? "short" : "long";
const clampPct = (p) => Math.max(0, Math.min(100, Math.round((Number(p) || 0) * 100)));
const tradedBy = (t) => (t.traded && t.traded.what) || "–";
const STAGE_TEXT = {
  INITIAL: "First stop: the stop has not moved yet",
  BREAKEVEN: "Stop above breakeven",
  LOCKED: "35% of the move locked in",
  TRAILING: "Trailing behind the best price",
};
const REASON_TEXT = {
  STOP: "Stopped out at the first stop",
  BREAKEVEN_STOP: "Stopped at breakeven",
  LOCK_STOP: "Stopped with 35% locked",
  TRAIL_STOP: "Trailing stop",
  TARGET: "Target reached",
  SIGNAL_EXIT: "Strategy exit",
  SESSION_END: "Session end",
  MANUAL: "Closed by you",
  DATA_END: "End of the stored data",
};
const stageText = (stage) => STAGE_TEXT[stage] || STAGE_TEXT.INITIAL;
function reasonText(reason) {
  const gap = /^GAP_/.test(String(reason || ""));
  const base = REASON_TEXT[String(reason || "").replace(/^GAP_/, "")];
  if (!base) return reason ? String(reason) : "–";
  return gap ? base + " (the price gapped past it)" : base;
}
/* Bin start (unix seconds) of the candle holding `unix`; bins are anchored at 09:15 each day. */
function binStart(unix, tfm) {
  const rel = (((unix + IST) % 86400) + 86400) % 86400 - DAY_OPEN, w = tfm * 60;
  return unix - (((rel % w) + w) % w);
}
/* The candle after adding one-minute bar to prev (the forming candle, or null). Candle = [t, o, h, l, c, v]. */
function mergeBar(prev, bar, tfm) {
  const t = binStart(bar[0], tfm);
  if (prev && prev[0] === t) return [t, prev[1], Math.max(prev[2], bar[2]), Math.min(prev[3], bar[3]), bar[4], (prev[5] || 0) + (bar[5] || 0)];
  return [t, bar[1], bar[2], bar[3], bar[4], bar[5] || 0];
}
function foldMinutes(candles, minutes, tfm) {
  const out = candles.slice();
  for (const m of minutes) {
    const last = out[out.length - 1], c = mergeBar(last, m, tfm);
    if (last && last[0] === c[0]) out[out.length - 1] = c; else out.push(c);
  }
  return out;
}
/* The minute drawn so far at fraction f of its length: open -> low -> high -> close when it closed up, open -> high -> low -> close when down. */
function partialMinute(bar, f) {
  const path = minutePath(bar), x = Math.min(1, Math.max(0, f)) * 3, seg = Math.min(2, Math.floor(x));
  const price = path[seg] + (path[seg + 1] - path[seg]) * (x - seg);
  const seen = path.slice(0, seg + 1).concat([price]);
  return [bar[0], bar[1], Math.max(...seen), Math.min(...seen), x >= 3 ? bar[4] : price, bar[5] || 0];
}
/* Minutes to ask for so that one Step ends at the close of a candle. */
function stepMinutes(cursorIso, tfm) {
  const next = unixOf(cursorIso) + 60, o = Math.floor((((next + IST) % 86400) - DAY_OPEN) / 60);
  if (o < 0 || o >= DAY_MINUTES) return tfm;
  return Math.max(1, Math.min(tfm - (o % tfm), DAY_MINUTES - o));
}
function strategiesFor(opts, inst) {
  const kind = inst && inst.kind;
  return ((opts && opts.strategies) || []).filter(s => (s.kinds || []).includes(kind));
}
const toIsoIst = (local) => local ? (local.length === 16 ? local + ":00" : local) + "+05:30" : null;
const defaultStart = (inst) => inst && inst.last ? String(inst.last).slice(0, 10) + "T09:15" : "";
/* A session needs one stored minute before its start, so the earliest start is the first stored minute plus one. */
function plusMinute(local) {
  const d = new Date(String(local).slice(0, 16) + ":00Z");
  d.setUTCMinutes(d.getUTCMinutes() + 1);
  return d.toISOString().slice(0, 16);
}
function clampStart(local, inst) {
  if (!inst || !local) return local || "";
  const lo = plusMinute(inst.first), hi = String(inst.last).slice(0, 16);
  return local < lo ? lo : (local > hi ? hi : local);
}
const WEEK = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTH = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
function clockText(unix) {
  if (unix === null || unix === undefined) return "–";
  const d = new Date((unix + IST) * 1000), s = d.toISOString();
  return `${WEEK[d.getUTCDay()]} ${s.slice(8, 10)} ${MONTH[d.getUTCMonth()]} ${s.slice(0, 4)}  ${s.slice(11, 16)}`;
}
const liveStaleSeconds = () => 600;
const minutesLeft = (alert, cursorIso) => Math.max(0, Math.round((Date.parse(alert.expires) - Date.parse(cursorIso) - 60000) / 60000));
/* The alert card: what to do, the managed price levels, rupees at risk and reward, the traded instrument, the reasons. */
function alertCardHtml(a, cursorIso) {
  if (!a) return "";
  const t = a.traded || {};
  const left = cursorIso && a.expires ? minutesLeft(a, cursorIso) : null;
  const reasons = (a.reasons || []).map(r => `<li>${esc(r)}</li>`).join("");
  return `<div class="card sim-alertcard ${a.side === "SHORT" ? "short" : "long"}" data-alert="${esc(a.id)}">`
    + `<div class="sim-do"><b>${esc(a.action)}</b> <span class="muted">${esc(sideWord(a.side))}, quantity ${esc(a.qty)}</span></div>`
    + `<div class="sim-levels"><span>Entry <b>${fmt(a.entry)}</b></span><span>Stop <b class="neg">${fmt(a.stop)}</b></span><span>Target <b class="pos">${fmt(a.target)}</b></span></div>`
    + `<div class="sim-levels"><span>Risk <b class="neg">${money(a.risk_inr)}</b></span><span>Reward <b class="pos">${money(a.reward_inr)}</b></span><span>Charges <b>${money(a.costs_inr)}</b></span></div>`
    + `<div class="sim-traded">You would trade <b>${esc(t.what || "–")}</b> at <b>${fmt(t.price)}</b> <span class="sim-src ${esc(sourceWord(t.source))}">${esc(sourceWord(t.source))}</span></div>`
    + (reasons ? `<ul class="sim-reasons">${reasons}</ul>` : "")
    + (left === null ? "" : `<div class="muted">This alert ends in ${left} market minute${left === 1 ? "" : "s"}.</div>`)
    + `<div class="actions"><button class="btn take" data-act="take">Take</button><button class="btn" data-act="skip">Skip</button></div></div>`;
}
/* The position card: waiting for the fill, the open trade (stage, progress to the target with its 25/50/80 marks, open profit), or the closed result. */
function positionCardHtml(t, exitSignal) {
  if (!t) return "";
  const head = `<b>${esc(t.action || "")}</b> <span class="muted">${esc(sideWord(t.side))}, quantity ${esc(t.qty)}, ${esc(tradedBy(t))} <span class="sim-src ${esc(sourceWord(t.source))}">${esc(sourceWord(t.source))}</span></span>`;
  if (t.status === "PENDING_FILL") {
    return `<div class="card sim-poscard"><div class="sim-do">${head}</div><div>Waiting for the fill at the next minute's open. Press Play to move to that minute.</div></div>`;
  }
  if (t.status === "CLOSED" || t.exit_reason) {
    return `<div class="card sim-poscard closed ${Number(t.net) >= 0 ? "won" : "lost"}"><div class="sim-do">${head}</div>`
      + `<div class="sim-result"><b>${esc(reasonText(t.exit_reason))}</b>: net <b class="${cls(t.net)}">${money(t.net)}</b>`
      + (t.r_multiple === null || t.r_multiple === undefined ? "" : ` (${fmt(t.r_multiple, 2)} R)`) + `</div>`
      + `<div class="sim-levels"><span>In <b>${fmt(t.entry)}</b></span><span>Out <b>${fmt(t.exit)}</b></span><span>Gross <b class="${cls(t.gross)}">${money(t.gross)}</b></span><span>Charges <b>${money(t.charges)}</b></span></div></div>`;
  }
  const w = clampPct(t.progress);
  return `<div class="card sim-poscard open"><div class="sim-do">${head}</div>`
    + (exitSignal ? `<div class="sim-exitsignal">The strategy says close this trade.</div>` : "")
    + `<div class="sim-levels"><span>Entry <b>${fmt(t.entry)}</b></span><span>Stop <b class="neg">${fmt(t.stop)}</b></span><span>Target <b class="pos">${fmt(t.target)}</b></span><span>Now <b>${fmt(t.mark)}</b></span></div>`
    + `<div class="sim-stage">${esc(stageText(t.stage))}</div>`
    + `<div class="sim-progress" title="progress to the target"><div class="sim-fill" style="width:${w}%"></div><i style="left:25%"></i><i style="left:50%"></i><i style="left:80%"></i></div>`
    + `<div class="sim-marks"><span style="left:25%">25%</span><span style="left:50%">50%</span><span style="left:80%">80%</span><span class="end">target</span></div>`
    + `<div class="sim-levels"><span>Open profit <b class="${cls(t.open_pnl)}">${money(t.open_pnl)}</b></span><span>${w}% of the way to the target</span></div>`
    + `<div class="actions"><button class="btn danger" data-act="close">Close</button></div></div>`;
}
function summaryTilesHtml(s, capital) {
  if (!s) return "";
  const tile = (l, v, c = "", i = "") => `<div class="tile"><small>${esc(l)}</small><b class="${c}">${v}</b>${i ? `<i>${esc(i)}</i>` : ""}</div>`;
  return tile("Trades", esc(s.trades ?? 0), "", `${s.winners ?? 0} winners`)
    + tile("Net result", money(s.net), cls(s.net)) + tile("Charges", money(s.charges))
    + tile("Average winner", money(s.avg_win), cls(s.avg_win)) + tile("Average loser", money(s.avg_loss), cls(s.avg_loss))
    + tile("Best", money(s.best), cls(s.best)) + tile("Worst", money(s.worst), cls(s.worst))
    + tile("Equity", money(s.equity), "", capital ? "started with " + money(capital) : "");
}
const eventLine = (e) => `<div class="sim-ev ${esc(String(e.kind || "").toLowerCase())}"><span class="muted">${esc(hhmm(e.ts))}</span> <b>${esc(String(e.kind || "").replace("_", " "))}</b> ${esc(e.text || "")}</div>`;
const tsfLocal = (s) => s ? String(s).replace("T", " ").slice(0, 16) : "–";

/* ── state ──────────────────────────────────────────────────────────── */
const S = {
  opts: null, offline: false, id: null, session: null, tfm: 3, mode: "past", candles: [], alert: null, trade: null, lastClosed: null,
  summary: null, trades: [], allTrades: null, view: "session", events: [], alertMarks: [], exitSignal: false,
  playing: false, speed: 1, acc: 0, liveAcc: 0, anim: null, busy: false, needRefresh: false, pausedByAlert: false,
  over: false, wantClosed: false, cursorIso: null, shownUnix: null, note: "", fake: null, resumeState: null,
  healT: null, chart: null, series: null, marks: null, lines: [], lineKey: "", ac: null,
  pauseOnAlert: true, sound: true, setup: store.get("sim.setup", {}) || {},
};
const cssv = (n) => (window.Charts && window.Charts.cssVar ? window.Charts.cssVar(n) : getComputedStyle(document.documentElement).getPropertyValue(n).trim());
const toT = (unix) => unix + IST;
const instOf = (key) => ((S.opts && S.opts.instruments) || []).find(i => i.key === key);

/* ── calls ──────────────────────────────────────────────────────────── */
async function getJson(url) {   // reads are plain fetches: a missing engine must not toast
  if (S.fake) return S.fake.handle(url);
  const r = await fetch(url);
  if (!r.ok) throw new Error(String(r.status));
  return r.json();
}
async function post(url, body) {
  if (S.fake) { try { return await S.fake.handle(url, body || {}); } catch (e) { toast(e.message, "bad"); throw e; } }
  return api(url, body || {});
}

/* ── chart ──────────────────────────────────────────────────────────── */
function chartOpts() {
  return {
    layout: { background: { type: "solid", color: cssv("--panel") }, textColor: cssv("--plot-font"), fontFamily: "JetBrains Mono, Menlo, monospace", fontSize: 13, attributionLogo: false },
    grid: { vertLines: { color: cssv("--grid") }, horzLines: { color: cssv("--grid") } },
    rightPriceScale: { borderColor: cssv("--line2") },
    timeScale: { borderColor: cssv("--line2"), timeVisible: true, secondsVisible: false, rightOffset: 6 },
    localization: { locale: "en-IN" },
  };
}
function ensureChart() {
  const el = $("sim-chart");
  if (!el) return false;
  if (S.chart) return true;
  const LW = window.LightweightCharts;
  if (!LW) { el.innerHTML = `<div class="muted sim-pad">The chart library is missing (static/vendor).</div>`; return false; }
  S.chart = LW.createChart(el, { ...chartOpts(), autoSize: true });
  const up = cssv("--ok"), dn = cssv("--bad");
  S.series = S.chart.addSeries(LW.CandlestickSeries, { upColor: up, downColor: dn, borderVisible: false, wickUpColor: up, wickDownColor: dn, priceLineVisible: false });
  S.marks = LW.createSeriesMarkers(S.series, []);
  return true;
}
function retheme() {
  if (!S.chart) return;
  S.chart.applyOptions(chartOpts());
  const up = cssv("--ok"), dn = cssv("--bad");
  S.series.applyOptions({ upColor: up, downColor: dn, wickUpColor: up, wickDownColor: dn });
  S.lineKey = ""; syncLines(); syncMarkers();
}
const barOf = (c) => ({ time: toT(c[0]), open: c[1], high: c[2], low: c[3], close: c[4] });
function drawAll() { if (ensureChart()) { S.series.setData(S.candles.map(barOf)); S.chart.timeScale().fitContent(); } }
function drawLast(c) { if (S.series) S.series.update(barOf(c)); }
function syncLines() {
  if (!S.series) return;
  const t = S.trade && S.trade.status === "OPEN" ? S.trade : null, a = !t && !S.trade && S.alert ? S.alert : null, src = t || a;
  const key = src ? [t ? "t" : "a", src.entry, src.stop, src.target, t && t.stop0, t && t.target0].join("|") : "";
  if (key === S.lineKey) return;
  S.lineKey = key;
  S.lines.forEach(l => { try { S.series.removePriceLine(l); } catch (e) {} });
  S.lines = [];
  if (!src) return;
  const add = (price, name, title, faint) => {
    if (!Number.isFinite(Number(price))) return;
    S.lines.push(S.series.createPriceLine({ price: Number(price), color: cssv(name), lineWidth: faint ? 1 : 2, lineStyle: faint ? 3 : (a ? 2 : 0), axisLabelVisible: !faint, title }));
  };
  add(src.entry, "--cyan", a ? "alert entry" : "entry");
  add(src.stop, "--bad", "stop");
  add(src.target, "--ok", "target");
  if (t && Number(t.stop0) !== Number(t.stop)) add(t.stop0, "--bad", "first stop", true);
  if (t && Number(t.target0) !== Number(t.target)) add(t.target0, "--ok", "first target", true);
}
function syncMarkers() {
  if (!S.marks) return;
  const m = [], seen = new Map();
  S.alertMarks.forEach(a => m.push({ time: toT(binStart(a.t - 60, S.tfm)), position: a.long ? "belowBar" : "aboveBar", shape: a.long ? "arrowUp" : "arrowDown", color: cssv("--accent2"), text: "alert", size: 1 }));
  S.trades.concat(S.trade ? [S.trade] : []).forEach(t => seen.set(t.id, t));
  seen.forEach(t => {
    const long = t.side !== "SHORT";
    if (t.entry_ts && t.status !== "PENDING_FILL") m.push({ time: toT(binStart(unixOf(t.entry_ts), S.tfm)), position: long ? "belowBar" : "aboveBar", shape: "circle", color: cssv("--cyan"), text: "in", size: 1 });
    if (t.exit_ts) m.push({ time: toT(binStart(unixOf(t.exit_ts), S.tfm)), position: long ? "aboveBar" : "belowBar", shape: "square", color: Number(t.net) >= 0 ? cssv("--ok") : cssv("--bad"), text: t.r_multiple == null ? "out" : fmt(t.r_multiple, 1) + "R", size: 1 });
  });
  m.sort((x, y) => x.time - y.time);
  S.marks.setMarkers(m);
}

/* ── sound ──────────────────────────────────────────────────────────── */
function beep() {
  if (!S.sound) return;
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    S.ac = S.ac || new AC();
    [0, 0.22].forEach(d => {
      const o = S.ac.createOscillator(), g = S.ac.createGain(), t0 = S.ac.currentTime + d;
      o.frequency.value = 880; g.gain.setValueAtTime(0.18, t0); g.gain.exponentialRampToValueAtTime(0.001, t0 + 0.18);
      o.connect(g); g.connect(S.ac.destination); o.start(t0); o.stop(t0 + 0.2);
    });
  } catch (e) {}
}

/* ── setup bar ──────────────────────────────────────────────────────── */
const optHtml = (items, sel) => items.map(([v, l]) => `<option value="${esc(v)}"${String(v) === String(sel) ? " selected" : ""}>${esc(l)}</option>`).join("");
function renderSetup() {
  const o = S.opts, st = S.setup;
  if (!o) return;
  const insts = o.instruments || [];
  if (!insts.length) return;
  const inst = instOf(st.instrument) || insts[0];
  st.instrument = inst.key;
  st.mode = st.mode === "live" && inst.live ? "live" : "past";
  $("sim-mode").innerHTML = optHtml([["past", "Past data"]].concat(inst.live ? [["live", "Live feed"]] : []), st.mode);
  $("sim-inst").innerHTML = optHtml(insts.map(i => [i.key, i.label + (i.trade === false ? " (training only)" : "")]), inst.key);
  const strats = strategiesFor(o, inst);
  const strat = strats.find(s => s.name === st.strategy) || strats[0];
  st.strategy = strat ? strat.name : "";
  $("sim-strat").innerHTML = optHtml(strats.map(s => [s.name, s.label]), st.strategy);
  const tfs = strat ? strat.timeframes || [] : [];
  st.timeframe = tfs.includes(st.timeframe) ? st.timeframe : (strat ? strat.default_timeframe || tfs[0] : "");
  $("sim-tf").innerHTML = optHtml(tfs.map(t => [t, t]), st.timeframe);
  $("sim-start-wrap").hidden = st.mode === "live";
  const start = $("sim-start");
  start.min = plusMinute(inst.first); start.max = String(inst.last).slice(0, 16);
  st.start = clampStart(st.start && st.startFor === inst.key ? st.start : defaultStart(inst), inst); st.startFor = inst.key;
  start.value = st.start;
  $("sim-go").disabled = !strat;
  $("sim-note").textContent = !strat ? "No strategy fits this instrument yet."
    : (inst.trade === false ? "Training only: you can practise on this instrument, the desk does not trade it. " : "") + (strat.description || "");
  store.set("sim.setup", st);
}
function readSetup() {
  const st = S.setup;
  st.mode = $("sim-mode").value; st.instrument = $("sim-inst").value; st.strategy = $("sim-strat").value;
  st.timeframe = $("sim-tf").value; st.start = $("sim-start").value;
}

/* ── session ────────────────────────────────────────────────────────── */
function resetRun(session) {
  S.id = session.id; S.session = session; S.tfm = session.tf_minutes || tfMinutes(session.timeframe);
  S.mode = session.mode; S.cursorIso = session.cursor; S.shownUnix = unixOf(session.cursor) + 60;
  S.alert = null; S.trade = null; S.lastClosed = null; S.summary = null; S.trades = []; S.events = []; S.alertMarks = [];
  S.exitSignal = false; S.anim = null; S.acc = 0; S.liveAcc = 0; S.over = false; S.note = ""; S.pausedByAlert = false; S.needRefresh = false;
  S.playing = false; S.lineKey = ""; S.view = "session"; S.allTrades = null; S.healT = null;
}
/* A session that starts (or resumes) in the middle of a candle only reveals that candle's later minutes, so the page draws it
   from a wrong open. Remember its start; when it has closed, take the server's version of it. */
function markPartial() {
  const next = unixOf(S.cursorIso) + 60, b = binStart(next, S.tfm);
  S.healT = next !== b ? b : null;
}
async function healCandle() {
  const t = S.healT; S.healT = null;
  if (t === null || !S.id) return;
  try {
    const got = await getJson(`/api/sim/sessions/${S.id}`);
    const c = (got.candles || []).find(x => x[0] === t), i = S.candles.findIndex(x => x[0] === t);
    if (c && i >= 0) { S.candles[i] = c; if (S.series) S.series.update(barOf(c)); }
  } catch (e) {}
}
function takeState(st) {
  if (!st) return;
  if (st.session) { S.session = st.session; S.cursorIso = st.session.cursor || S.cursorIso; S.over = st.session.status === "ENDED"; }
  S.alert = st.alert || null; S.trade = st.trade || null; S.summary = st.summary || S.summary; S.trades = st.trades || S.trades;
  if (S.trade && S.trade.status === "CLOSED") { S.lastClosed = S.trade; S.trade = null; }
}
async function startSession() {
  readSetup();
  const st = S.setup;
  if (!st.strategy) return;
  if (S.id && !S.over) { try { await post(`/api/sim/sessions/${S.id}/end`, {}); } catch (e) {} }
  S.playing = false;
  let r;
  try {
    r = await post("/api/sim/sessions", { instrument: st.instrument, strategy: st.strategy, timeframe: st.timeframe, mode: st.mode,
      start: st.mode === "live" ? null : toIsoIst(st.start), params: {} });
  } catch (e) { return; }
  resetRun(r.session);
  S.candles = r.candles || [];
  $("sim-run").hidden = false;
  drawAll(); markPartial();
  takeState(r.state);
  store.set("sim.last", { id: S.id });
  S.note = S.mode === "live" ? "Following the live feed. Press Play." : "Press Play to start the replay.";
  render();
  try { if (!S.ac) { const AC = window.AudioContext || window.webkitAudioContext; if (AC) S.ac = new AC(); } } catch (e) {}
}
async function refresh() {
  if (!S.id) return;
  try { takeState(await getJson(`/api/sim/sessions/${S.id}`)); } catch (e) {}
  if (S.wantClosed && !S.trade && S.trades[0]) { S.lastClosed = S.trades[0]; S.wantClosed = false; }
  render();
}

/* ── clock ──────────────────────────────────────────────────────────── */
function pushMinutes(minutes) {
  for (const m of minutes) {
    const last = S.candles[S.candles.length - 1], c = mergeBar(last, m, S.tfm);
    if (last && last[0] === c[0]) S.candles[S.candles.length - 1] = c; else S.candles.push(c);
    drawLast(c);
    if (S.healT !== null && c[0] > S.healT) healCandle();
  }
  if (minutes.length) S.shownUnix = minutes[minutes.length - 1][0] + 60;
}
function absorb(res) {
  S.cursorIso = res.cursor || S.cursorIso;
  S.alert = res.alert || null; S.summary = res.summary || S.summary;
  if (res.trade && res.trade.status === "CLOSED") { S.lastClosed = res.trade; S.trade = null; } else S.trade = res.trade || null;
  let reload = false;
  for (const ev of res.events || []) {
    S.events.unshift(ev);
    if (ev.kind === "ALERT") {
      S.lastClosed = null; S.wantClosed = false; S.exitSignal = false;
      const a = res.alert;
      S.alertMarks.push({ t: unixOf(ev.ts), long: !a || a.side !== "SHORT" });
      toast("Alert: " + ((a && a.action) || ev.text), "warn"); beep();
      if (S.pauseOnAlert && S.playing) { S.playing = false; S.pausedByAlert = true; S.note = "Paused: there is an alert to take or skip."; }
    } else if (ev.kind === "FILLED") toast("Filled: " + (ev.text || "the trade is open"));
    else if (ev.kind === "EXIT_SIGNAL") { S.exitSignal = true; toast("The strategy says close this trade.", "warn"); }
    else if (ev.kind === "CLOSED") { S.exitSignal = false; reload = true; S.wantClosed = true; toast(ev.text || "The trade is closed."); }
    else if (ev.kind === "ALERT_EXPIRED") toast("The alert ran out of time.");
  }
  S.events = S.events.slice(0, 60);
  if (res.day_end) { S.needRefresh = true; S.playing = false; S.note = "The trading day is finished. Press Play to go on to the next day."; }
  if (res.over) { S.playing = false; S.over = true; S.note = "The stored data has ended."; }
  syncLines(); syncMarkers(); render();
  if (reload || S.needRefresh) { S.needRefresh = false; refresh(); }
}
async function advance(n) {
  const id = S.id;
  S.busy = true;
  try {
    const res = await post(`/api/sim/sessions/${id}/advance`, { minutes: n });
    return id === S.id ? res : null;
  } catch (e) { S.playing = false; render(); return null; } finally { S.busy = false; }
}
async function startMinute() {
  const res = await advance(1);
  if (!res) return;
  const mins = res.minutes || [];
  if (!mins.length) { absorb(res); return; }
  pushMinutes(mins.slice(0, -1));
  S.anim = { res, bar: mins[mins.length - 1], base: S.candles[S.candles.length - 1] || null, f: 0 };
}
function finishMinute() {
  const a = S.anim;
  if (!a) return;
  S.anim = null;
  pushMinutes([a.bar]);
  absorb(a.res);
}
async function runFast(n) {
  const res = await advance(n);
  if (!res) return;
  pushMinutes(res.minutes || []);
  absorb(res);
}
function tick(dtMs) {
  if (!S.id || !S.playing || S.busy || S.over || document.hidden) return;
  if (S.mode === "live") {
    S.liveAcc += dtMs;
    if (S.liveAcc >= 5000) { S.liveAcc = 0; runFast(1); }
    return;
  }
  if (S.speed <= 10) {
    if (!S.anim) { startMinute(); return; }
    S.anim.f += dtMs / (minuteSeconds(S.speed) * 1000);
    if (S.anim.f >= 1) { finishMinute(); return; }
    drawLast(mergeBar(S.anim.base, partialMinute(S.anim.bar, S.anim.f), S.tfm));
    S.shownUnix = S.anim.bar[0] + 60 * S.anim.f; renderClock();
    return;
  }
  if (S.anim) { finishMinute(); return; }
  S.acc += dtMs / 1000 * S.speed;
  const n = minutesToAsk(S.speed, S.acc);
  if (n > 0) { S.acc -= n * 60; runFast(n); }
}
function setPlaying(on) {
  if (!S.id || S.over) on = false;
  S.playing = on; S.pausedByAlert = false;
  if (on) { S.note = ""; S.acc = 0; S.liveAcc = 4500; }
  render();
}
async function stepOnce() {
  if (!S.id || S.over || S.busy || S.mode === "live") return;
  S.playing = false;
  if (S.anim) { finishMinute(); return; }
  await runFast(stepMinutes(S.cursorIso, S.tfm));
}
async function act(kind) {
  if (!S.id) return;
  const base = `/api/sim/sessions/${S.id}`;
  try {
    if (kind === "take" && S.alert) await post(base + "/take", { alert_id: S.alert.id });
    else if (kind === "skip" && S.alert) await post(base + "/skip", { alert_id: S.alert.id });
    else if (kind === "close") await post(base + "/close", {});
    else if (kind === "end") { await post(base + "/end", {}); S.playing = false; S.note = "The session has ended."; S.over = true; }
  } catch (e) { return; }
  const resume = S.pausedByAlert && (kind === "take" || kind === "skip");
  if (kind === "take") S.lastClosed = null;
  if (S.anim) S.needRefresh = true; else await refresh();
  if (kind === "close") S.note = "Closing at the next minute's open. Press Play.";
  if (resume) setPlaying(true);
  render();
}

/* ── rendering ──────────────────────────────────────────────────────── */
function renderSpeeds() {
  const sp = (S.opts && S.opts.speeds) || [1, 2, 5, 10, 60, 300];
  $("sim-speeds").innerHTML = S.mode === "live" ? "" : sp.map(v => `<span class="chip${v === S.speed ? " active" : ""}" data-speed="${v}">${v}x</span>`).join("");
}
function renderClock() { $("sim-clock").textContent = clockText(S.shownUnix); }
function renderLog() {
  const common = [
    { l: "In", f: t => esc(hhmm(t.entry_ts)) }, { l: "Out", f: t => esc(hhmm(t.exit_ts)) },
    { l: "Traded", f: t => esc(tradedBy(t)) + ` <span class="sim-src ${esc(sourceWord(t.source))}">${esc(sourceWord(t.source))}</span>` },
    { l: "Entry", n: 1, f: t => fmt(t.entry) }, { l: "Exit", n: 1, f: t => fmt(t.exit) },
    { l: "Reason", f: t => esc(reasonText(t.exit_reason)) },
    { l: "Gross", n: 1, c: t => cls(t.gross), f: t => money(t.gross) }, { l: "Charges", n: 1, f: t => money(t.charges) },
    { l: "Net", n: 1, c: t => cls(t.net), f: t => money(t.net) }, { l: "R", n: 1, f: t => t.r_multiple == null ? "–" : fmt(t.r_multiple, 2) },
  ];
  const all = S.view === "all";
  const cols = all ? [{ l: "Day", f: t => esc(String(t.entry_ts || "").slice(0, 10)) }, { l: "Instrument", f: t => esc(t.instrument || "") }, { l: "Strategy", f: t => esc((t.strategy || "") + " " + (t.timeframe || "")) }].concat(common) : common;
  const rows = all ? S.allTrades : S.trades;
  $("sim-log").innerHTML = rows === null ? `<div class="muted">Loading…</div>` : table(cols, rows, { empty: all ? "No simulator trades yet." : "No closed trades in this session yet." });
  $("sim-view-session").classList.toggle("active", !all); $("sim-view-all").classList.toggle("active", all);
  $("sim-events").innerHTML = S.events.length ? S.events.slice(0, 12).map(eventLine).join("") : `<div class="muted">Nothing yet.</div>`;
}
function render() {
  if (!S.id) return;
  renderClock();
  $("sim-badge").textContent = S.mode === "live" ? "LIVE" : (S.over ? "ENDED" : (S.playing ? "PLAYING" : "PAUSED"));
  $("sim-badge").className = "sim-badge " + (S.mode === "live" ? "live" : (S.playing ? "on" : ""));
  $("sim-play").textContent = S.playing ? "Pause" : "Play";
  $("sim-play").disabled = S.over;
  $("sim-step").hidden = S.mode === "live"; $("sim-step").disabled = S.over;
  renderSpeeds();
  $("sim-premium").textContent = S.session && S.session.premium_note ? "Prices: " + S.session.premium_note : "";
  const stale = S.mode === "live" && S.cursorIso && Date.now() / 1000 - (unixOf(S.cursorIso) + 60) > liveStaleSeconds();
  $("sim-status").textContent = (stale ? `Waiting for live minutes: the newest one the engine has is from ${clockText(unixOf(S.cursorIso) + 60)}. ` : "") + (S.note || (S.session ? `${S.session.instrument.label} · ${S.session.strategy} · ${S.session.timeframe}` : ""));
  $("sim-alert").innerHTML = S.trade ? "" : alertCardHtml(S.alert, S.cursorIso);
  $("sim-pos").innerHTML = positionCardHtml(S.trade || S.lastClosed, S.exitSignal);
  $("sim-summary").innerHTML = summaryTilesHtml(S.summary, S.session && S.session.capital);
  renderLog(); syncLines(); syncMarkers();
}
async function showView(v) {
  S.view = v;
  if (v === "all") { S.allTrades = null; renderLog(); try { S.allTrades = (await getJson("/api/sim/trades?limit=200")).trades || []; } catch (e) { S.allTrades = []; } }
  renderLog();
}

/* ── start-up ───────────────────────────────────────────────────────── */
function bannerHtml() {
  $("sim-banner").innerHTML = S.fake ? `<div class="card sim-demo"><b>Demo data, not market data.</b> This tab is running on a built-in fake of the engine (?simfake=1).</div>` : "";
}
async function offerResume() {
  const last = store.get("sim.last", null);
  if (!last || !last.id || S.id) return;
  let st;
  try { st = await getJson(`/api/sim/sessions/${last.id}`); } catch (e) { return; }
  if (!st || !st.session || st.session.status === "ENDED") return;
  const s = st.session;
  $("sim-resume").innerHTML = `<div class="card sim-resumecard">Resume your last session? <b>${esc(s.instrument.label)}</b>, ${esc(s.strategy)} ${esc(s.timeframe)}, at ${esc(tsfLocal(s.cursor))}. `
    + `<button class="btn primary small" data-act="resume">Resume</button> <button class="btn small" data-act="dismiss">No thanks</button></div>`;
  S.resumeState = st;
}
function resume() {
  const st = S.resumeState;
  if (!st) return;
  resetRun(st.session);
  S.candles = st.candles || [];
  $("sim-run").hidden = false;
  drawAll(); markPartial(); takeState(st);
  S.note = "Resumed at the server's clock. Press Play.";
  $("sim-resume").innerHTML = ""; S.resumeState = null;
  render();
}
async function loadTab() {
  bannerHtml();
  if (S.opts || S.offline) { if (S.chart) S.chart.timeScale().fitContent(); return; }
  try {
    S.opts = await getJson("/api/sim/options");
    if (!S.opts || !S.opts.instruments) throw new Error("no options");
  } catch (e) {
    S.offline = true; S.opts = null;
    $("sim-note").textContent = "The simulator's engine is not running in this build.";
    $("sim-go").disabled = true;
    return;
  }
  S.speed = Number(S.setup.speed) || 1;
  S.pauseOnAlert = S.setup.pauseOnAlert !== false; S.sound = S.setup.sound !== false;
  $("sim-pause-alert").checked = S.pauseOnAlert; $("sim-sound").checked = S.sound;
  renderSetup();
  offerResume();
}
function wire() {
  const root = $("sim-root");
  root.addEventListener("change", (e) => {
    const id = e.target.id;
    if (id === "sim-mode" || id === "sim-inst" || id === "sim-strat" || id === "sim-tf" || id === "sim-start") {
      readSetup();
      if (id === "sim-inst") S.setup.start = "";
      renderSetup();
    } else if (id === "sim-pause-alert" || id === "sim-sound") {
      S.pauseOnAlert = $("sim-pause-alert").checked; S.sound = $("sim-sound").checked;
      S.setup.pauseOnAlert = S.pauseOnAlert; S.setup.sound = S.sound; store.set("sim.setup", S.setup);
    }
  });
  root.addEventListener("click", (e) => {
    const sp = e.target.closest("[data-speed]");
    if (sp) { S.speed = Number(sp.dataset.speed); S.setup.speed = S.speed; store.set("sim.setup", S.setup); S.acc = 0; render(); return; }
    const b = e.target.closest("[data-act]");
    if (!b) return;
    const a = b.dataset.act;
    if (a === "start") startSession();
    else if (a === "play") setPlaying(!S.playing);
    else if (a === "step") stepOnce();
    else if (a === "view-session") showView("session");
    else if (a === "view-all") showView("all");
    else if (a === "resume") resume();
    else if (a === "dismiss") { $("sim-resume").innerHTML = ""; store.set("sim.last", null); }
    else act(a);
  });
  document.addEventListener("keydown", (e) => {
    if (!OL.tabShown("sim") || !S.id) return;
    const tag = (e.target && e.target.tagName) || "";
    if (/^(INPUT|SELECT|TEXTAREA|BUTTON)$/.test(tag) || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.code === "Space") { e.preventDefault(); setPlaying(!S.playing); }
    else if (e.code === "ArrowRight") { e.preventDefault(); stepOnce(); }
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && S.playing) { S.playing = false; S.note = "Paused while this tab was in the background."; render(); }
  });
  new MutationObserver(retheme).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  let prev = performance.now();
  setInterval(() => { const now = performance.now(), dt = Math.min(now - prev, 1000); prev = now; tick(dt); }, 100);
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
