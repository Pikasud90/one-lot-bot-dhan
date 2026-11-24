/* A fake of the simulator's engine (US-094), for looking at the tab without the server and for the node tests.
   Loaded only with ?simfake=1. It serves the second contract (.agile/stories/SIM-API.md and SIM-API-2.md) from
   generated bars: an index (NIFTY near 25,000), a training index, two shares at very different prices (RELIANCE near
   1,400 and TATASTEEL near 150) and a share with no stored bars. Each day rallies after the first alert and pulls back,
   so a session shows an alert, a fill, the stop moving through its stages and a close. Demo data, not market data.
   Switches in the page's address: &simslow=1 (a session takes 3 s to build, as a cold server would), &simfail=candles,
   &simfail=create, &simfail=advance or &simfail=options (that call fails), &simlat=40 (every answer waits 40 ms). */
(function (root) {
"use strict";
const IST = 19800, DAY_MIN = 375;
const fail = (status, detail) => { const e = new Error(detail); e.status = status; return e; };
const r2 = (v) => Math.round(v * 100) / 100;
const snap = (v, tick) => r2(Math.round(v / tick) * tick);
const iso = (unix) => new Date((unix + IST) * 1000).toISOString().slice(0, 19) + "+05:30";
const unixOf = (s) => Math.floor(Date.parse(s) / 1000);
const dayOpen = (d) => Date.UTC(+d.slice(0, 4), +d.slice(5, 7) - 1, +d.slice(8, 10), 9, 15) / 1000 - IST;
function rng(seed) { let a = seed >>> 0; return () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }
function binStart(unix, tfm) { const rel = (((unix + IST) % 86400) + 86400) % 86400 - 33300, w = tfm * 60; return unix - (((rel % w) + w) % w); }
const minuteOfDay = (unix) => Math.floor(((((unix + IST) % 86400) + 86400) % 86400 - 33300) / 60);

/* Fifteen trading days to Fri 20 Nov 2026 (weekends left out, and Wed 11 Nov as a holiday). */
const DAYS = (() => {
  const out = [];
  for (let t = Date.UTC(2026, 10, 20); out.length < 15; t -= 86400000) {
    const d = new Date(t), s = d.toISOString().slice(0, 10);
    if (d.getUTCDay() !== 0 && d.getUTCDay() !== 6 && s !== "2026-11-11") out.unshift(s);
  }
  return out;
})();
const FIRST = DAYS[0] + "T09:15:00+05:30", LAST = DAYS[DAYS.length - 1] + "T15:29:00+05:30";
const INSTRUMENTS = [
  { key: "NIFTY", label: "NIFTY 50", kind: "INDEX", live: true, trade: true, tick: 0.05, lot: 75, unit: "lot", base: 25000, vol: 0 },
  { key: "MIDCPNIFTY", label: "NIFTY MIDCAP SELECT", kind: "INDEX", live: false, trade: false, tick: 0.05, lot: 120, unit: "lot", base: 12800, vol: 0 },
  { key: "RELIANCE", label: "Reliance Industries", kind: "STOCK", live: false, trade: true, tick: 0.1, lot: 1, unit: "share", base: 1400, vol: 26000 },
  { key: "TATASTEEL", label: "Tata Steel", kind: "STOCK", live: false, trade: true, tick: 0.01, lot: 1, unit: "share", base: 150, vol: 310000 },
  { key: "ETERNAL", label: "Eternal (no stored bars)", kind: "STOCK", live: false, trade: true, tick: 0.05, lot: 1, unit: "share", base: 300, vol: 0, empty: true },
];
const STRATEGIES = [
  { name: "microtrend", label: "Micro trend (demo)", kinds: ["INDEX"], timeframes: ["3m", "5m", "15m"], default_timeframe: "3m",
    description: "Demo rule: buys a call when the trend turns up.", plots: [{ id: "ema", label: "EMA 20", pane: "price" }] },
  { name: "pullback", label: "Pullback (demo)", kinds: ["STOCK"], timeframes: ["5m", "15m", "60m"], default_timeframe: "15m",
    description: "Demo rule for a share: buys a dip in an up day.", plots: [{ id: "vwap", label: "VWAP", pane: "price" }, { id: "ema", label: "EMA 20", pane: "price" }] },
];
const instView = ({ base, vol, empty, ...i }) => ({ ...i, first: empty ? null : FIRST, last: empty ? null : LAST });
const OPTIONS = { instruments: INSTRUMENTS.map(instView), strategies: STRATEGIES, speeds: [1, 2, 5, 10, 60, 300],
  defaults: { capital: 100000, stop_atr_k: 1.5, target_r: 3.0, extend: true, instrument: "NIFTY", strategy: "microtrend", timeframe: "3m" },
  chart_timeframes: ["1m", "3m", "5m", "10m", "15m", "30m", "60m"] };
const ALERT_MINUTES = [14, 160, 230];     // minute of the day (0 = 09:15) at whose close an alert is raised
const EXIT_MINUTE = DAY_MIN - 15;          // 15:15: nothing is carried overnight

/* ── switches ──────────────────────────────────────────────────────── */
const query = (s) => { const q = {}; String(s || "").replace(/^\?/, "").split("&").forEach(p => { const [k, v] = p.split("="); if (k) q[k] = decodeURIComponent(v || ""); }); return q; };
const CFG = { slow: false, fail: "", latency: 0 };
if (typeof location !== "undefined" && location.search) {
  const q = query(location.search);
  CFG.slow = q.simslow === "1"; CFG.fail = q.simfail || ""; CFG.latency = Math.max(0, Number(q.simlat) || 0);
}
const now = () => Date.now();

/* ── bars ──────────────────────────────────────────────────────────── */
const barCache = new Map();
function barsFor(inst) {
  if (inst.empty) return [];
  if (barCache.has(inst.key)) return barCache.get(inst.key);
  const out = [], b = inst.base;
  let px = b;
  DAYS.forEach((d, di) => {
    const rnd = rng(7919 * (di + 1) + Math.round(b)), t0 = dayOpen(d), aim = b * (1 + (rnd() - 0.5) * 0.02);
    px = px * (1 + (rnd() - 0.5) * 0.004);   // a small overnight gap from the close before
    for (let i = 0; i < DAY_MIN; i++) {
      const drift = i >= 15 && i < 75 ? b * 0.00045 : (i >= 75 && i < 110 ? -b * 0.0006 : (i >= 161 && i < 200 ? b * 0.0003 : (i >= 230 ? (aim - px) * 0.02 : 0)));
      const o = px, c = o + drift + (rnd() - 0.5) * b * 0.0004;
      const h = Math.max(o, c) + rnd() * b * 0.00015, l = Math.min(o, c) - rnd() * b * 0.00015;
      const v = inst.vol ? Math.round(inst.vol * (0.5 + rnd()) * (1 + 2 * Math.exp(-i / 20))) : 0;
      out.push([t0 + i * 60, snap(o, inst.tick), snap(h, inst.tick), snap(l, inst.tick), snap(c, inst.tick), v]); px = c;
    }
  });
  barCache.set(inst.key, out);
  return out;
}
/* index of the last bar at or before unix (-1 when none) */
function idxAtOrBefore(bars, unix) {
  let lo = 0, hi = bars.length - 1, ans = -1;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (bars[m][0] <= unix) { ans = m; lo = m + 1; } else hi = m - 1; }
  return ans;
}
/* Closed candles of the first n bars, and the forming one (or null). A candle is closed once its last minute is in. */
function candlesOf(bars, n, tfm) {
  const out = [];
  for (let i = 0; i < n; i++) {
    const b = bars[i], t = binStart(b[0], tfm), last = out[out.length - 1];
    if (last && last[0] === t) { last[2] = Math.max(last[2], b[2]); last[3] = Math.min(last[3], b[3]); last[4] = b[4]; last[5] += b[5]; }
    else out.push([t, b[1], b[2], b[3], b[4], b[5]]);
  }
  let forming = null;
  if (out.length && n > 0) {
    const lastMin = bars[n - 1][0], mod = minuteOfDay(lastMin), closes = mod === DAY_MIN - 1 || binStart(lastMin + 60, tfm) !== out[out.length - 1][0];
    if (!closes) forming = out.pop();
  }
  return { candles: out, forming };
}

/* ── overlays (the strategy's lines) ───────────────────────────────── */
/* For each plot, the point of every candle of the strategy timeframe, keyed by the index of the bar that closes it. */
function overlayPlan(bars, tfm, plots) {
  const byClose = new Map(), k = 2 / 21;
  let ema = null, cs = null, pv = 0, vv = 0, day = null;
  for (let i = 0; i < bars.length; i++) {
    const b = bars[i], t = binStart(b[0], tfm), d = Math.floor((b[0] + IST) / 86400);
    if (d !== day) { day = d; pv = 0; vv = 0; }
    const w = b[5] || 1; pv += (b[2] + b[3] + b[4]) / 3 * w; vv += w;
    if (!cs || cs[0] !== t) cs = [t, b[4]]; else cs[1] = b[4];
    const next = bars[i + 1], closes = !next || binStart(next[0], tfm) !== t || minuteOfDay(b[0]) === DAY_MIN - 1;
    if (!closes) continue;
    ema = ema === null ? cs[1] : ema + k * (cs[1] - ema);
    const pts = {};
    plots.forEach(p => { pts[p.id] = [t, r2(p.id === "vwap" ? pv / vv : ema)]; });
    byClose.set(i, pts);
  }
  return byClose;
}
function overlaysBetween(s, from, to) {   // points of candles closed by bars from..to-1
  const out = {};
  s.plots.forEach(p => { out[p.id] = []; });
  for (let i = from; i < to; i++) { const pts = s.overlay.get(i); if (pts) s.plots.forEach(p => out[p.id].push(pts[p.id])); }
  return out;
}

/* ── sessions ──────────────────────────────────────────────────────── */
const sessions = new Map(), prepared = new Map();
let seq = 0;
const BUILD_MS = 3000;
function prepState(key) {
  if (!CFG.slow) return { status: "ready", ms: 40 };
  const p = prepared.get(key);
  if (!p) return null;
  const ms = now() - p;
  return ms >= BUILD_MS ? { status: "ready", ms: BUILD_MS } : { status: "building", ms };
}
function summaryOf(s) {
  const t = s.closed, w = t.filter(x => x.net > 0), l = t.filter(x => x.net <= 0), sum = (a, k) => a.reduce((p, x) => p + x[k], 0);
  return { trades: t.length, winners: w.length, net: r2(sum(t, "net")), gross: r2(sum(t, "gross")), charges: r2(sum(t, "charges")),
    avg_win: w.length ? r2(sum(w, "net") / w.length) : 0, avg_loss: l.length ? r2(sum(l, "net") / l.length) : 0,
    best: t.length ? Math.max(...t.map(x => x.net)) : 0, worst: t.length ? Math.min(...t.map(x => x.net)) : 0, equity: r2(100000 + sum(t, "net")) };
}
const isIndex = (s) => s.inst.kind === "INDEX";
const deltaOf = (s) => isIndex(s) ? 0.5 : 1;
const priceOf = (s, t, managed) => isIndex(s) ? r2(Math.max(0.05, t.p0 + 0.5 * t.sgn * (managed - t.entry0))) : r2(managed);
function tradeView(s, t) {
  if (!t) return null;
  const src = isIndex(s) ? "model" : "real", open = t.status === "OPEN", ep = t.entry === null ? null : priceOf(s, t, t.entry);
  const v = { id: t.id, status: t.status, side: t.side, action: t.action, qty: t.qty, lot: t.qty, unit: s.inst.unit,
    entry_ts: t.entry_ts || null, entry: t.entry, entry_price: ep, source: src,
    stop: t.stop ?? null, target: t.target ?? null, stop0: t.stop0 ?? null, target0: t.target0 ?? null,
    stop_price: t.stop == null ? null : priceOf(s, t, t.stop), target_price: t.target == null ? null : priceOf(s, t, t.target),
    risk_inr: t.entry === null ? null : r2(t.risk * deltaOf(s) * t.qty),
    stage: t.stage, best: t.best ?? null, progress: t.progress || 0, mark: t.mark ?? null, mark_price: t.mark == null ? null : priceOf(s, t, t.mark), open_pnl: 0, open_r: 0,
    minutes_in_trade: t.entryIdx == null ? 0 : (t.exitIdx ?? s.idx) - t.entryIdx,
    exit_ts: t.exit_ts || null, exit: t.exit ?? null, exit_price: t.exit == null ? null : priceOf(s, t, t.exit), exit_reason: t.exit_reason || null,
    gross: t.gross ?? null, charges: t.charges ?? null, net: t.net ?? null, r_multiple: t.r_multiple ?? null, traded: { what: t.what, source: src } };
  if (open && v.mark_price !== null) { v.open_pnl = r2((v.mark_price - v.entry_price) * t.qty); v.open_r = r2(v.open_pnl / v.risk_inr); }
  return v;
}
function alertView(s) {
  if (!s.alert) return null;
  const a = { ...s.alert }, left = Math.max(0, Math.round((unixOf(a.expires) - (s.bars[s.idx - 1][0] + 60)) / 60));
  delete a.sgn;
  return { ...a, expires_in_min: left };
}
function sessionView(s) {
  const cur = s.idx > 0 ? s.bars[s.idx - 1][0] : s.bars[s.startIdx][0] - 60;
  return { id: s.id, instrument: { key: s.inst.key, label: s.inst.label, kind: s.inst.kind }, strategy: s.strategy, timeframe: s.timeframe, tf_minutes: s.tfm, mode: s.mode,
    start: iso(s.bars[s.startIdx][0]), cursor: iso(cur), status: s.status, capital: 100000, chart: { tick: s.inst.tick, lot: s.inst.lot, unit: s.inst.unit },
    params: { stop_atr_k: 1.5, target_r: 3.0, extend: true, auto_exit: true, stop_atr_tf: "60m" }, premium_note: isIndex(s) ? "demo: modelled premiums only" : "share prices from the demo bars" };
}
function building(s) {
  if (s.status !== "BUILDING") return null;
  const ms = now() - s.born;
  if (CFG.fail === "create" && ms >= 1500) { s.status = "FAILED"; s.detail = "The strategy could not be built from these bars (a demo failure)."; return null; }
  if (ms >= s.buildMs) { s.status = "RUNNING"; return null; }
  return { step: ms < s.buildMs * 0.33 ? "reading bars" : (ms < s.buildMs * 0.75 ? "building signals" : "pricing"), elapsed_ms: ms };
}
function marksOpen(s) {   // marks of the open trade since its entry (at most 400)
  const t = s.trade;
  if (!t || t.status !== "OPEN") return [];
  return t.marks.slice(-400);
}
function stateOf(s, withCandles) {
  const build = building(s);
  const out = { session: sessionView(s), alert: build ? null : alertView(s), trade: build ? null : tradeView(s, s.trade), summary: summaryOf(s),
    trades: s.closed.slice().reverse().map(t => tradeView(s, t)), events: s.events.slice(-200), marks: marksOpen(s) };
  if (build) out.build = build;
  if (s.status === "FAILED") out.detail = s.detail;
  if (withCandles) {
    const c = candlesOf(s.bars, s.idx, s.tfm);
    out.candles = c.candles.slice(-300);
    const from = c.candles.length ? c.candles[Math.max(0, c.candles.length - 300)][0] : Infinity;
    const ov = overlaysBetween(s, 0, s.idx);
    Object.keys(ov).forEach(k => { ov[k] = ov[k].filter(p => p[0] >= from); });
    out.overlays = ov;
  }
  return out;
}
function push(s, ev, kind, ts, text, data) { const e = { seq: ++s.evSeq, ts, kind, text, data: data || {} }; s.events.push(e); ev.push(e); }
function raiseAlert(s, bar, ev) {
  const sgn = 1, risk = r2(s.inst.base * 0.0018), entry = bar[4], delta = deltaOf(s), q = s.inst.lot;
  const p0 = isIndex(s) ? r2(s.inst.base * 0.0165) : entry;
  s.alertSeq += 1;
  s.alert = { id: "a_" + s.alertSeq, ts: iso(bar[0] + 60), expires: iso(bar[0] + 60 + 600), side: "LONG", action: isIndex(s) ? "BUY CALL" : "BUY",
    entry, stop: r2(entry - risk), target: r2(entry + 3 * risk), risk, reward: r2(3 * risk), qty: q, lot: q, unit: s.inst.unit,
    traded: { what: isIndex(s) ? `${s.inst.key} ${Math.round(entry / 50) * 50} CE 24 Nov` : s.inst.label, price: p0, source: isIndex(s) ? "model" : "real",
      stop_price: r2(p0 - risk * delta), target_price: r2(p0 + 3 * risk * delta) },
    risk_inr: r2(risk * delta * q), reward_inr: r2(3 * risk * delta * q), costs_inr: isIndex(s) ? 92.4 : 18.6,
    reasons: ["Demo alert: the price has risen since the open", "Demo data, not market data"], sgn };
  push(s, ev, "ALERT", s.alert.ts, `${s.alert.action}: demo signal`, { alert_id: s.alert.id, side: "LONG", action: s.alert.action });
}
function closeTrade(s, t, bar, price, reason, ev, atOpen) {
  const delta = deltaOf(s);
  t.status = "CLOSED"; t.exit = r2(price); t.exit_reason = reason; t.exit_ts = iso(bar[0] + (atOpen ? 0 : 60)); t.exitIdx = s.idx;
  t.gross = r2(t.sgn * (t.exit - t.entry) * delta * t.qty); t.charges = isIndex(s) ? 92.4 : 18.6; t.net = r2(t.gross - t.charges);
  t.r_multiple = r2(t.net / (t.risk * delta * t.qty)); t.mark = t.exit;
  s.closed.push(t); s.trade = null;
  push(s, ev, "CLOSED", t.exit_ts, `Closed (${reason}): net ${t.net}`, { trade_id: t.id, exit: t.exit, exit_price: priceOf(s, t, t.exit), exit_reason: reason, net: t.net, r_multiple: t.r_multiple });
}
function manage(s, t, bar, ev) {   // one minute of the stop / target ratchet (demo version of manage/ratchet.py): the stop first
  const sgn = t.sgn, hit = (lvl) => sgn > 0 ? bar[3] <= lvl : bar[2] >= lvl, ts = iso(bar[0] + 60);
  if (hit(t.stop)) return closeTrade(s, t, bar, t.stop, { FIRST_STOP: "STOP", BREAKEVEN: "BREAKEVEN_STOP", LOCKED: "LOCK_STOP", TRAILING: "TRAIL_STOP" }[t.stage], ev);
  if (sgn > 0 ? bar[2] >= t.target : bar[3] <= t.target) return closeTrade(s, t, bar, t.target, "TARGET", ev);
  t.best = sgn > 0 ? Math.max(t.best, bar[2]) : Math.min(t.best, bar[3]);
  t.mark = bar[4]; t.progress = Math.max(0, Math.min(1, sgn * (t.best - t.entry) / (t.target0 - t.entry)));
  const was = t.stage, stops = { BREAKEVEN: t.entry + sgn * 0.05 * t.risk, LOCKED: t.entry + 0.35 * (t.best - t.entry), TRAILING: t.best - sgn * 0.8 * t.risk };
  const want = t.progress >= 0.8 ? "TRAILING" : (t.progress >= 0.5 ? "LOCKED" : (t.progress >= 0.25 ? "BREAKEVEN" : was));
  const order = ["FIRST_STOP", "BREAKEVEN", "LOCKED", "TRAILING"];
  if (order.indexOf(want) > order.indexOf(was)) { t.stage = want; push(s, ev, "STAGE", ts, "Stage: " + want.toLowerCase(), { stage: want }); }
  if (t.stage !== "FIRST_STOP") {
    const ns = r2(stops[t.stage]);
    if (sgn * (ns - t.stop) > 0) { t.stop = ns; push(s, ev, "STOP_MOVED", ts, "Stop moved to " + ns, { stop: ns }); }
  }
  if (t.stage === "TRAILING" && sgn * (t.best + sgn * 0.5 * t.risk - t.target) > 0) {   // the target is moved out once the trail runs
    t.target = r2(t.target + sgn * t.risk); push(s, ev, "TARGET_MOVED", ts, "Target moved to " + t.target, { target: t.target });
  }
}
const STOPPERS = new Set(["ALERT", "EXIT_SIGNAL", "CLOSED", "CANCELLED", "DAY_END", "DATA_END"]);
function reveal(s, n, until) {
  const ev = [], minutes = [], marks = [], from = s.idx; let dayEnd = false, over = false;
  if (s.status === "DAY_END") s.status = "RUNNING";
  for (let k = 0; k < n; k++) {
    if (s.idx >= s.bars.length) { over = true; push(s, ev, "DATA_END", iso(s.bars[s.bars.length - 1][0] + 60), "The stored data has ended.", {}); s.status = "ENDED"; break; }
    const bar = s.bars[s.idx], i = minuteOfDay(bar[0]), before = ev.length;
    s.idx += 1; minutes.push(bar);
    const t = s.trade;
    if (t && t.status === "PENDING_FILL") {
      t.status = "OPEN"; t.entry = r2(bar[1]); t.entry0 = t.entry; t.entry_ts = iso(bar[0]); t.entryIdx = s.idx - 1; t.risk = s.alert0.risk; t.best = t.entry;
      t.stop = r2(t.entry - t.sgn * t.risk); t.target = r2(t.entry + t.sgn * 3 * t.risk); t.stop0 = t.stop; t.target0 = t.target; t.p0 = s.alert0.traded.price; t.mark = t.entry; t.marks = [];
      push(s, ev, "FILLED", iso(bar[0]), "Filled at " + t.entry, { trade_id: t.id, entry: t.entry, entry_price: priceOf(s, t, t.entry), stop: t.stop, target: t.target, source: isIndex(s) ? "model" : "real" });
    }
    if (s.trade && s.trade.status === "OPEN") {
      if (s.trade.closing) closeTrade(s, s.trade, bar, bar[1], "MANUAL", ev, true);
      else if (i === EXIT_MINUTE) closeTrade(s, s.trade, bar, bar[1], "SESSION_END", ev, true);
      else manage(s, s.trade, bar, ev);
    }
    if (s.trade && s.trade.status === "OPEN") {
      const v = tradeView(s, s.trade), row = [bar[0], bar[4], v.mark_price, v.open_pnl];
      s.trade.marks.push(row); marks.push(row);
    }
    if (s.alert && unixOf(s.alert.expires) <= bar[0] + 60) { s.alert = null; push(s, ev, "ALERT_EXPIRED", iso(bar[0] + 60), "The alert ran out of time.", {}); }
    if (!s.alert && !s.trade && ALERT_MINUTES.includes(i)) { raiseAlert(s, bar, ev); s.alert0 = s.alert; }
    if (i === DAY_MIN - 1) { dayEnd = true; s.status = "DAY_END"; push(s, ev, "DAY_END", iso(bar[0] + 60), "The trading day is finished.", {}); break; }
    const mine = ev.slice(before);
    if (until === "event" && mine.length) break;
    if (until === "alert" && mine.some(e => STOPPERS.has(e.kind))) break;
  }
  const cur = s.bars[s.idx - 1];
  return { cursor: iso(cur[0]), minutes, events: ev, alert: alertView(s), trade: tradeView(s, s.trade), summary: summaryOf(s), last_price: cur[4], over, day_end: dayEnd,
    marks, overlays: overlaysBetween(s, from, s.idx) };
}
function all(sessionId) {
  const rows = [];
  sessions.forEach(s => { if (!sessionId || s.id === sessionId) s.closed.forEach(t => rows.push({ ...tradeView(s, t), session_id: s.id, instrument: s.inst.key, strategy: s.strategy, timeframe: s.timeframe, mode: s.mode })); });
  return rows.sort((a, b) => String(b.exit_ts).localeCompare(String(a.exit_ts)));
}
function csv() {
  const head = "session_id,instrument,strategy,timeframe,mode,entry_ts,exit_ts,entry,exit,entry_price,exit_price,exit_reason,gross,charges,net,r_multiple";
  return [head].concat(all().map(t => [t.session_id, t.instrument, t.strategy, t.timeframe, t.mode, t.entry_ts, t.exit_ts, t.entry, t.exit, t.entry_price, t.exit_price, t.exit_reason, t.gross, t.charges, t.net, t.r_multiple].join(","))).join("\n");
}
function instOf(key) {
  const inst = INSTRUMENTS.find(i => i.key === key);
  if (!inst) throw fail(400, `Unknown instrument. Allowed: ${INSTRUMENTS.map(i => i.key).join(", ")}.`);
  return inst;
}
function candlesRoute(q) {
  if (CFG.fail === "candles") throw fail(500, "The store could not be read (a demo failure).");
  const inst = instOf(q.instrument), tf = q.timeframe || "3m";
  if (!OPTIONS.chart_timeframes.includes(tf)) throw fail(400, `Unknown timeframe. Allowed: ${OPTIONS.chart_timeframes.join(", ")}.`);
  const tfm = parseInt(tf, 10), bars = barsFor(inst), count = Math.max(50, Math.min(1000, Number(q.count) || 300));
  const meta = { key: inst.key, label: inst.label, kind: inst.kind, tick: inst.tick };
  if (!bars.length) return { instrument: meta, timeframe: tf, tf_minutes: tfm, until: null, candles: [], forming: null, note: "No stored bars for this instrument." };
  let note;
  let until = q.until ? unixOf(q.until) : bars[bars.length - 1][0];
  if (until < bars[0][0]) { until = bars[0][0]; note = "The time asked for is before the first stored minute: the chart starts there."; }
  if (until > bars[bars.length - 1][0]) { until = bars[bars.length - 1][0]; note = "The time asked for is after the last stored minute."; }
  const n = idxAtOrBefore(bars, until) + 1, c = candlesOf(bars, n, tfm);
  const out = { instrument: meta, timeframe: tf, tf_minutes: tfm, until: iso(bars[n - 1][0]), candles: c.candles.slice(-count), forming: c.forming };
  if (note) out.note = note;
  return out;
}
function create(body) {
  const inst = instOf(body.instrument), strat = STRATEGIES.find(x => x.name === body.strategy);
  if (!strat || !strat.kinds.includes(inst.kind)) throw fail(400, "That strategy does not fit that instrument.");
  if (!strat.timeframes.includes(body.timeframe)) throw fail(400, `That strategy runs on ${strat.timeframes.join(", ")}.`);
  const bars = barsFor(inst);
  if (!bars.length) throw fail(400, "There are no stored bars for this instrument.");
  const want = body.mode === "live" || !body.start ? bars[bars.length - DAY_MIN][0] : unixOf(body.start);
  if (want <= bars[0][0]) throw fail(400, "A start needs one stored minute before it.");
  const startIdx = bars.findIndex(b => b[0] >= want);
  if (startIdx < 0) throw fail(400, "The start is after the last stored minute.");
  const key = `${inst.key}|${strat.name}|${body.timeframe}`, ready = prepState(key);
  const tfm = parseInt(body.timeframe, 10), plots = strat.plots || [];
  const s = { id: "s_fake" + (++seq), inst, strategy: strat.name, timeframe: body.timeframe, tfm, mode: body.mode === "live" ? "live" : "past", bars, startIdx, idx: startIdx,
    status: "RUNNING", alert: null, alert0: null, alertSeq: 0, evSeq: 0, trade: null, closed: [], events: [], plots, overlay: overlayPlan(bars, tfm, plots), born: now(), buildMs: 0 };
  if (CFG.slow && !(ready && ready.status === "ready")) { s.status = "BUILDING"; s.buildMs = ready ? Math.max(300, BUILD_MS - ready.ms) : BUILD_MS; }
  if (CFG.fail === "create") { s.status = "BUILDING"; s.buildMs = 60000; }
  sessions.set(s.id, s);
  const st = stateOf(s, true);
  return { session: st.session, candles: st.candles, overlays: st.overlays, events: st.events, marks: st.marks, build: st.build, state: st };
}
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
async function handle(url, body) {
  if (CFG.latency) await sleep(CFG.latency);
  const u = new URL(url, "http://fake.local"), p = u.pathname, q = query(u.search);
  if (!body) {
    if (p === "/api/sim/options") { if (CFG.fail === "options") throw fail(503, "The desk is still starting."); return OPTIONS; }
    if (p === "/api/sim/days") { const inst = instOf(q.instrument); return { instrument: inst.key, days: inst.empty ? [] : DAYS.slice(), first: inst.empty ? null : FIRST, last: inst.empty ? null : LAST }; }
    if (p === "/api/sim/candles") return candlesRoute(q);
    if (p === "/api/sim/prepare") {
      const key = `${q.instrument}|${q.strategy}|${q.timeframe}`;
      if (CFG.slow && !prepared.has(key)) prepared.set(key, now());
      return { key, ...(prepState(key) || { status: "building", ms: 0 }) };
    }
    if (p === "/api/sim/trades") return { trades: all(q.session_id).slice(0, Number(q.limit) || 200) };
    if (p === "/api/sim/trades.csv") return { csv: csv() };
    const m = p.match(/^\/api\/sim\/sessions\/([^/]+)$/);
    if (m) { const s = sessions.get(m[1]); if (!s) throw fail(404, "No such session."); return stateOf(s, true); }
    throw fail(404, "Unknown route.");
  }
  if (p === "/api/sim/sessions") return create(body);
  const m = p.match(/^\/api\/sim\/sessions\/([^/]+)\/(advance|take|skip|close|end)$/);
  if (!m) throw fail(404, "Unknown route.");
  const s = sessions.get(m[1]);
  if (!s) throw fail(404, "No such session.");
  building(s);
  if (s.status === "BUILDING") throw fail(409, "The session is still being prepared.");
  if (s.status === "FAILED") throw fail(409, s.detail);
  const done = (extra) => ({ ...stateOf(s, false), events: [], minutes: [], marks: [], ...extra });
  if (m[2] === "advance") {
    if (CFG.fail === "advance") throw fail(500, "The engine stopped answering (a demo failure).");
    if (s.status === "ENDED") throw fail(409, "The session has ended.");
    const n = Number(body.minutes);
    if (!Number.isInteger(n) || n < 1 || n > 390) throw fail(400, "minutes must be a whole number from 1 to 390.");
    if (body.until != null && body.until !== "event" && body.until !== "alert") throw fail(400, "until must be null, \"event\" or \"alert\".");
    return reveal(s, s.mode === "live" ? 1 : n, body.until || null);
  }
  if (m[2] === "take") {
    if (!s.alert || s.alert.id !== body.alert_id) throw fail(409, "That alert is gone.");
    if (s.trade) throw fail(409, "A trade is already open.");
    const a = s.alert; s.alert = null;
    s.trade = { id: s.closed.length + 1 + 40, status: "PENDING_FILL", side: a.side, sgn: a.side === "LONG" ? 1 : -1, action: a.action, qty: a.qty, what: a.traded.what, entry: null, stage: "FIRST_STOP", risk: a.risk };
    return done();
  }
  if (m[2] === "skip") { if (!s.alert) throw fail(409, "There is no alert."); s.alert = null; return done(); }
  if (m[2] === "close") {
    if (!s.trade) throw fail(409, "There is no open trade.");
    if (s.trade.status !== "OPEN") {   // not filled yet: cancelled, and said so
      const id = s.trade.id, ev = []; s.trade = null;
      push(s, ev, "CANCELLED", sessionView(s).cursor, "You closed the trade before it filled", { trade_id: id });
      return done({ events: ev });
    }
    s.trade.closing = true; return done();
  }
  // end: an open trade closes at the next minute's open (one minute revealed); an unfilled one is cancelled
  let extra = {};
  if (s.trade && s.trade.status === "OPEN" && s.idx < s.bars.length) {
    s.trade.closing = true;
    const r = reveal(s, 1, null);
    extra = { events: r.events, minutes: r.minutes, marks: r.marks };
  } else if (s.trade) {
    const ev = [], id = s.trade.id; s.trade = null;
    push(s, ev, "CANCELLED", sessionView(s).cursor, "The session ended before the trade filled", { trade_id: id });
    extra = { events: ev };
  }
  s.status = "ENDED"; s.alert = null;
  return done(extra);
}
const api = { handle, OPTIONS, DAYS, configure(c) { Object.assign(CFG, c || {}); }, reset() { sessions.clear(); prepared.clear(); seq = 0; Object.assign(CFG, { slow: false, fail: "", latency: 0 }); } };
if (typeof module !== "undefined" && module.exports) module.exports = api;
if (root) root.SimFake = api;
})(typeof window !== "undefined" ? window : null);
