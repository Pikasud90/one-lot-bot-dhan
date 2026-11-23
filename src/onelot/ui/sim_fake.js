/* A fake of the simulator's engine (US-076), for looking at the tab without US-075 and for the node tests.
   Loaded only with ?simfake=1. It serves the contract of .agile/stories/SIM-API.md from generated bars:
   a rally after the first alert, then a pullback, so one session shows an alert, a fill, the stop moving
   through its stages and a close. Demo data, not market data. */
(function (root) {
"use strict";
const IST = 19800, DAY_MIN = 375, HIST_DAYS = ["2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"];
const INSTRUMENTS = [
  { key: "NIFTY", label: "NIFTY 50", kind: "INDEX", first: "2026-10-01T09:15:00+05:30", last: "2026-10-09T15:29:00+05:30", live: true, trade: true, base: 25000 },
  { key: "RELIANCE", label: "Reliance Industries", kind: "STOCK", first: "2026-10-01T09:15:00+05:30", last: "2026-10-09T15:29:00+05:30", live: false, trade: true, base: 1400 },
  { key: "MIDCPNIFTY", label: "Midcap Select", kind: "INDEX", first: "2026-10-01T09:15:00+05:30", last: "2026-10-09T15:29:00+05:30", live: false, trade: false, base: 12000 },
];
const STRATEGIES = [
  { name: "microtrend", label: "Micro trend (demo)", kinds: ["INDEX"], timeframes: ["3m", "5m", "15m"], default_timeframe: "3m", description: "Demo strategy: buys a call after the first quarter hour." },
  { name: "pullback", label: "Pullback (demo)", kinds: ["STOCK"], timeframes: ["15m", "60m"], default_timeframe: "15m", description: "Demo strategy for a share." },
];
const OPTIONS = { instruments: INSTRUMENTS.map(({ base, ...i }) => i), strategies: STRATEGIES, speeds: [1, 2, 5, 10, 60, 300],
  defaults: { capital: 100000, stop_atr_k: 1.5, target_r: 3.0, extend: true } };
const ALERT_MINUTES = [14, 160, 230];     // minute of the day (0 = 09:15) at whose close an alert is raised
const iso = (unix) => { const s = new Date((unix + IST) * 1000).toISOString(); return s.slice(0, 19) + "+05:30"; };
const dayOpen = (d) => Date.UTC(+d.slice(0, 4), +d.slice(5, 7) - 1, +d.slice(8, 10), 9, 15) / 1000 - IST;
const r2 = (v) => Math.round(v * 100) / 100;
function rng(seed) { let a = seed >>> 0; return () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }
function barsFor(inst) {
  const out = []; let px = inst.base;
  HIST_DAYS.forEach((d, di) => {
    const rnd = rng(1000 * di + inst.base), t0 = dayOpen(d);
    px = inst.base * (1 + (rnd() - 0.5) * 0.004);   // each day opens near the base: the demo rally does not compound
    for (let i = 0; i < DAY_MIN; i++) {
      const drift = i >= 15 && i < 75 ? inst.base * 0.00045 : (i >= 75 && i < 110 ? -inst.base * 0.0006 : 0);
      const o = px, c = o + drift + (rnd() - 0.5) * inst.base * 0.0004;
      const h = Math.max(o, c) + rnd() * inst.base * 0.00015, l = Math.min(o, c) - rnd() * inst.base * 0.00015;
      out.push([t0 + i * 60, r2(o), r2(h), r2(l), r2(c), Math.round(rnd() * 900)]); px = c;
    }
  });
  return out;
}
function binStart(unix, tfm) { const rel = (((unix + IST) % 86400) + 86400) % 86400 - 33300, w = tfm * 60; return unix - (((rel % w) + w) % w); }
function candlesUpTo(bars, n, tfm) {   // closed candles of the first n minute bars (a candle is closed once the bar before the next bin is in)
  const out = [];
  for (let i = 0; i < n; i++) {
    const b = bars[i], t = binStart(b[0], tfm), last = out[out.length - 1];
    if (last && last[0] === t) { last[2] = Math.max(last[2], b[2]); last[3] = Math.min(last[3], b[3]); last[4] = b[4]; last[5] += b[5]; }
    else out.push([t, b[1], b[2], b[3], b[4], b[5]]);
  }
  if (out.length && n > 0 && (bars[n - 1][0] + 60) < out[out.length - 1][0] + tfm * 60) out.pop();
  return out.slice(-300);
}
const sessions = new Map();
let seq = 0;
const fail = (status, detail) => { const e = new Error(detail); e.status = status; return e; };
function summaryOf(s) {
  const t = s.closed, w = t.filter(x => x.net > 0), l = t.filter(x => x.net <= 0), sum = (a, k) => a.reduce((p, x) => p + x[k], 0);
  return { trades: t.length, winners: w.length, net: r2(sum(t, "net")), gross: r2(sum(t, "gross")), charges: r2(sum(t, "charges")),
    avg_win: w.length ? r2(sum(w, "net") / w.length) : 0, avg_loss: l.length ? r2(sum(l, "net") / l.length) : 0,
    best: t.length ? Math.max(...t.map(x => x.net)) : 0, worst: t.length ? Math.min(...t.map(x => x.net)) : 0, equity: r2(100000 + sum(t, "net")) };
}
const priceOf = (s, t, managed) => s.inst.kind === "INDEX" ? r2(t.p0 + 0.5 * t.sgn * (managed - t.entry0)) : r2(managed);
function tradeView(s, t) {
  if (!t) return null;
  const v = { id: t.id, status: t.status, side: t.side, action: t.action, qty: t.qty, entry_ts: t.entry_ts || null, entry: t.entry, entry_price: t.entry === null ? null : priceOf(s, t, t.entry),
    source: s.inst.kind === "INDEX" ? "model" : "real", stop: t.stop, target: t.target, stop0: t.stop0, target0: t.target0, stage: t.stage, best: t.best,
    progress: t.progress || 0, mark: t.mark || null, mark_price: t.mark == null ? null : priceOf(s, t, t.mark), open_pnl: 0,
    exit_ts: t.exit_ts || null, exit: t.exit ?? null, exit_price: t.exit == null ? null : priceOf(s, t, t.exit), exit_reason: t.exit_reason || null,
    gross: t.gross ?? null, charges: t.charges ?? null, net: t.net ?? null, r_multiple: t.r_multiple ?? null, traded: { what: t.what, source: s.inst.kind === "INDEX" ? "model" : "real" } };
  if (t.status === "OPEN") v.open_pnl = r2((v.mark_price - v.entry_price) * t.qty);
  return v;
}
function stateOf(s) {
  const alert = s.alert ? { ...s.alert } : null;
  return { session: sessionView(s), alert, trade: tradeView(s, s.trade), summary: summaryOf(s), trades: s.closed.slice().reverse().map(t => tradeView(s, t)) };
}
function sessionView(s) {
  const cur = s.idx > s.startIdx ? s.bars[s.idx - 1] : null;
  return { id: s.id, instrument: { key: s.inst.key, label: s.inst.label, kind: s.inst.kind }, strategy: s.strategy, timeframe: s.timeframe, tf_minutes: s.tfm, mode: s.mode,
    start: iso(s.bars[s.startIdx][0]), cursor: iso(cur ? cur[0] : s.bars[s.startIdx][0] - 60), status: s.status, capital: 100000,
    params: { stop_atr_k: 1.5, target_r: 3.0, extend: true }, premium_note: "demo: modelled premiums only" };
}
function raiseAlert(s, bar, ev) {
  const sgn = 1, risk = r2(s.inst.base * 0.0018), entry = bar[4], delta = s.inst.kind === "INDEX" ? 0.5 : 1;
  const p0 = s.inst.kind === "INDEX" ? 412.5 : entry;
  s.alertSeq += 1;
  s.alert = { id: "a_" + s.alertSeq, ts: iso(bar[0] + 60), expires: iso(bar[0] + 60 + 600), side: "LONG", action: s.inst.kind === "INDEX" ? "BUY CALL" : "BUY",
    entry, stop: r2(entry - risk), target: r2(entry + 3 * risk), risk, reward: r2(3 * risk), qty: s.inst.kind === "INDEX" ? 75 : 1,
    traded: { what: s.inst.kind === "INDEX" ? s.inst.label + " 25000 CE" : s.inst.label, price: p0, source: s.inst.kind === "INDEX" ? "model" : "real", stop_price: r2(p0 - risk * delta), target_price: r2(p0 + 3 * risk * delta) },
    risk_inr: r2(risk * delta * (s.inst.kind === "INDEX" ? 75 : 1)), reward_inr: r2(3 * risk * delta * (s.inst.kind === "INDEX" ? 75 : 1)), costs_inr: 92.4,
    reasons: ["Demo alert: the price has risen since the open", "Demo data, not market data"], sgn };
  ev.push({ seq: ++s.evSeq, ts: s.alert.ts, kind: "ALERT", text: "Buy a call: demo signal", data: {} });
}
function closeTrade(s, t, bar, price, reason, ev) {
  const sgn = t.sgn, delta = s.inst.kind === "INDEX" ? 0.5 : 1;
  t.status = "CLOSED"; t.exit = r2(price); t.exit_reason = reason; t.exit_ts = iso(bar[0] + (reason === "MANUAL" || reason === "SESSION_END" ? 0 : 60));
  t.gross = r2(sgn * (t.exit - t.entry) * delta * t.qty); t.charges = 92.4; t.net = r2(t.gross - t.charges);
  t.r_multiple = r2(t.net / (t.risk * delta * t.qty)); t.mark = t.exit;
  s.closed.push(t); s.trade = null;
  ev.push({ seq: ++s.evSeq, ts: iso(bar[0] + 60), kind: "CLOSED", text: `Closed (${reason}): net ${t.net}`, data: { reason } });
}
function manage(s, t, bar, ev) {   // one minute of the stop / target ratchet (demo version of manage/ratchet.py)
  const sgn = t.sgn, hit = (lvl) => sgn > 0 ? bar[3] <= lvl : bar[2] >= lvl;
  if (hit(t.stop)) return closeTrade(s, t, bar, t.stop, { INITIAL: "STOP", BREAKEVEN: "BREAKEVEN_STOP", LOCKED: "LOCK_STOP", TRAILING: "TRAIL_STOP" }[t.stage], ev);
  if (sgn > 0 ? bar[2] >= t.target : bar[3] <= t.target) return closeTrade(s, t, bar, t.target, "TARGET", ev);
  t.best = sgn > 0 ? Math.max(t.best, bar[2]) : Math.min(t.best, bar[3]);
  t.mark = bar[4]; t.progress = Math.max(0, Math.min(1, sgn * (t.best - t.entry) / (t.target - t.entry)));
  const was = t.stage, stops = { BREAKEVEN: t.entry + sgn * 0.05 * t.risk, LOCKED: t.entry + 0.35 * (t.best - t.entry), TRAILING: t.best - sgn * 0.8 * t.risk };
  const want = t.progress >= 0.8 ? "TRAILING" : (t.progress >= 0.5 ? "LOCKED" : (t.progress >= 0.25 ? "BREAKEVEN" : was));
  const order = ["INITIAL", "BREAKEVEN", "LOCKED", "TRAILING"];
  if (order.indexOf(want) > order.indexOf(was)) { t.stage = want; ev.push({ seq: ++s.evSeq, ts: iso(bar[0] + 60), kind: "STAGE", text: "Stage: " + want.toLowerCase(), data: { stage: want } }); }
  if (t.stage !== "INITIAL") {
    const ns = r2(stops[t.stage]);
    if (sgn * (ns - t.stop) > 0) { t.stop = ns; ev.push({ seq: ++s.evSeq, ts: iso(bar[0] + 60), kind: "STOP_MOVED", text: "Stop moved to " + ns, data: { stop: ns } }); }
  }
}
function reveal(s, n) {
  const ev = [], minutes = []; let dayEnd = false, over = false;
  if (s.status === "DAY_END") s.status = "RUNNING";
  for (let k = 0; k < n; k++) {
    if (s.idx >= s.bars.length) { over = true; ev.push({ seq: ++s.evSeq, ts: iso(s.bars[s.bars.length - 1][0] + 60), kind: "DATA_END", text: "The stored data has ended.", data: {} }); s.status = "ENDED"; break; }
    const bar = s.bars[s.idx], i = s.idx - Math.floor(s.idx / DAY_MIN) * DAY_MIN;
    s.idx += 1; minutes.push(bar);
    const t = s.trade;
    if (t && t.status === "PENDING_FILL") {
      t.status = "OPEN"; t.entry = r2(bar[1]); t.entry0 = t.entry; t.entry_ts = iso(bar[0]); t.risk = s.alert0.risk; t.best = t.entry;
      t.stop = r2(t.entry - t.sgn * t.risk); t.target = r2(t.entry + t.sgn * 3 * t.risk); t.stop0 = t.stop; t.target0 = t.target; t.p0 = s.alert0.traded.price; t.mark = t.entry;
      ev.push({ seq: ++s.evSeq, ts: iso(bar[0]), kind: "FILLED", text: "Filled at " + t.entry, data: {} });
    }
    if (s.trade && s.trade.status === "OPEN") {
      if (s.trade.closing) closeTrade(s, s.trade, bar, bar[1], "MANUAL", ev);
      else if (i === DAY_MIN - 1) closeTrade(s, s.trade, bar, bar[4], "SESSION_END", ev);
      else manage(s, s.trade, bar, ev);
    }
    if (s.alert && Date.parse(s.alert.expires) <= (bar[0] + 60) * 1000) { s.alert = null; ev.push({ seq: ++s.evSeq, ts: iso(bar[0] + 60), kind: "ALERT_EXPIRED", text: "The alert ran out of time.", data: {} }); }
    if (!s.alert && !s.trade && ALERT_MINUTES.includes(i)) { raiseAlert(s, bar, ev); s.alert0 = s.alert; }
    if (i === DAY_MIN - 1) { dayEnd = true; s.status = "DAY_END"; ev.push({ seq: ++s.evSeq, ts: iso(bar[0] + 60), kind: "DAY_END", text: "The trading day is finished.", data: {} }); break; }
  }
  const cur = s.bars[s.idx - 1];
  return { cursor: iso(cur[0]), minutes, events: ev, alert: s.alert ? { ...s.alert } : null, trade: tradeView(s, s.trade), summary: summaryOf(s), last_price: cur[4], over, day_end: dayEnd };
}
function csv(s) {
  const head = "session_id,instrument,strategy,timeframe,mode,entry_ts,exit_ts,entry,exit,exit_reason,gross,charges,net,r_multiple";
  return [head].concat(all().map(t => [t.session_id, t.instrument, t.strategy, t.timeframe, t.mode, t.entry_ts, t.exit_ts, t.entry, t.exit, t.exit_reason, t.gross, t.charges, t.net, t.r_multiple].join(","))).join("\n");
}
function all(sessionId) {
  const rows = [];
  sessions.forEach(s => { if (!sessionId || s.id === sessionId) s.closed.forEach(t => rows.push({ ...tradeView(s, t), session_id: s.id, instrument: s.inst.key, strategy: s.strategy, timeframe: s.timeframe, mode: s.mode })); });
  return rows.sort((a, b) => String(b.exit_ts).localeCompare(String(a.exit_ts)));
}
async function handle(url, body) {
  const u = new URL(url, "http://fake.local"), p = u.pathname;
  if (!body) {
    if (p === "/api/sim/options") return OPTIONS;
    if (p === "/api/sim/trades") return { trades: all(u.searchParams.get("session_id")).slice(0, Number(u.searchParams.get("limit")) || 200) };
    if (p === "/api/sim/trades.csv") return { csv: csv() };
    const m = p.match(/^\/api\/sim\/sessions\/([^/]+)$/);
    if (m) { const s = sessions.get(m[1]); if (!s) throw fail(404, "No such session."); return stateOf(s); }
    throw fail(404, "Unknown route.");
  }
  if (p === "/api/sim/sessions") {
    const inst = INSTRUMENTS.find(i => i.key === body.instrument), strat = STRATEGIES.find(x => x.name === body.strategy);
    if (!inst || !strat || !strat.kinds.includes(inst.kind)) throw fail(400, "That strategy does not fit that instrument.");
    const bars = barsFor(inst), want = body.mode === "live" || !body.start ? bars[bars.length - DAY_MIN][0] : Math.floor(Date.parse(body.start) / 1000);
    let startIdx = bars.findIndex(b => b[0] >= want);
    if (startIdx < 0) throw fail(400, "The start is after the last stored minute.");
    const s = { id: "s_fake" + (++seq), inst, strategy: strat.name, timeframe: body.timeframe, tfm: parseInt(body.timeframe, 10), mode: body.mode, bars, startIdx, idx: startIdx,
      status: "RUNNING", alert: null, alert0: null, alertSeq: 0, evSeq: 0, trade: null, closed: [] };
    sessions.set(s.id, s);
    return { session: sessionView(s), candles: candlesUpTo(bars, startIdx, s.tfm), state: stateOf(s) };
  }
  const m = p.match(/^\/api\/sim\/sessions\/([^/]+)\/(advance|take|skip|close|end)$/);
  if (!m) throw fail(404, "Unknown route.");
  const s = sessions.get(m[1]);
  if (!s) throw fail(404, "No such session.");
  if (m[2] === "advance") return reveal(s, s.mode === "live" ? 1 : Math.max(1, Math.min(390, Number(body.minutes) || 1)));
  if (m[2] === "take") {
    if (!s.alert || s.alert.id !== body.alert_id) throw fail(409, "That alert is gone.");
    if (s.trade) throw fail(409, "A trade is already open.");
    const a = s.alert; s.alert = null;
    s.trade = { id: s.closed.length + 1, status: "PENDING_FILL", side: a.side, sgn: a.side === "LONG" ? 1 : -1, action: a.action, qty: a.qty, what: a.traded.what, entry: null, stage: "INITIAL", risk: a.risk };
    return { ...stateOf(s), events: [], minutes: [] };
  }
  if (m[2] === "skip") { if (!s.alert) throw fail(409, "There is no alert."); s.alert = null; return { ...stateOf(s), events: [], minutes: [] }; }
  if (m[2] === "close") {
    if (!s.trade) throw fail(409, "There is no open trade.");
    if (s.trade.status !== "OPEN") {   // not filled yet: cancelled, and said so
      const id = s.trade.id; s.trade = null;
      return { ...stateOf(s), events: [{ seq: ++s.evSeq, ts: sessionView(s).cursor, kind: "CANCELLED", text: "You closed the trade before it filled", data: { trade_id: id } }], minutes: [] };
    }
    s.trade.closing = true; return { ...stateOf(s), events: [], minutes: [] };
  }
  if (s.trade && s.trade.status === "OPEN") s.trade.closing = true;
  s.status = "ENDED";
  return { ...stateOf(s), events: [], minutes: [] };
}
const api = { handle, OPTIONS, reset() { sessions.clear(); seq = 0; } };
if (typeof module !== "undefined" && module.exports) module.exports = api;
if (root) root.SimFake = api;
})(typeof window !== "undefined" ? window : null);
