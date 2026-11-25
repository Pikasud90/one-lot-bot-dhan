/* The Simulator's pure helpers (US-094): no DOM, no network, no chart. Loaded by the page before sim_chart.js and
   sim.js (window.SimCore) and by the node tests (require). Times are unix seconds (UTC) unless said; the page reads
   Indian time (IST, +05:30). Money is in rupees. Contract: .agile/stories/SIM-API.md and SIM-API-2.md. */
(function (root) {
"use strict";
const IST = 19800;                 // seconds: Indian time is UTC + 5:30 (no daylight saving)
const DAY_OPEN = 33300;            // 09:15 as seconds into the day
const DAY_MINUTES = 375;           // 09:15 to 15:30
const CHART_TFS = ["1m", "3m", "5m", "10m", "15m", "30m", "60m"];
const SPEEDS = [1, 2, 5, 10, 60, 300];
const WEEK = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTH = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/* ── numbers and words ─────────────────────────────────────────────── */
const isNum = (v) => v !== null && v !== undefined && v !== "" && Number.isFinite(Number(v));
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[m]));
/* 25010.5 -> "25,010.50" (Indian grouping), "–" when not a number */
const num = (v, d = 2) => isNum(v) ? Number(v).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d }) : "–";
/* -2287.5 -> "−₹2,287.50" */
const money = (v) => isNum(v) ? (Number(v) < 0 ? "−" : "") + "₹" + Math.abs(Number(v)).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : "–";
/* whole rupees with a sign, for chart labels: 6712.5 -> "+₹6,713", -2287.5 -> "−₹2,288" */
const rupees = (v) => isNum(v) ? (Number(v) < 0 ? "−" : (Number(v) > 0 ? "+" : "")) + "₹" + (Math.abs(Number(v)) < 100 ? Math.abs(Number(v)).toFixed(2) : Math.round(Math.abs(Number(v))).toLocaleString("en-IN")) : "–";   // paise under ₹100 (one share)
const signedMoney = (v) => isNum(v) ? (Number(v) > 0 ? "+" : "") + money(v) : "–";
const rText = (r) => isNum(r) ? (Number(r) > 0 ? "+" : (Number(r) < 0 ? "−" : "")) + Math.abs(Number(r)).toFixed(2) + " R" : "–";
const pctText = (p) => isNum(p) ? (Number(p) > 0 ? "+" : (Number(p) < 0 ? "−" : "")) + Math.abs(Number(p)).toFixed(2) + "%" : "–";
const tone = (v) => !isNum(v) ? "" : (Number(v) > 0 ? "pos" : (Number(v) < 0 ? "neg" : ""));
/* Volume in Indian units: 1234 -> "1.2 K", 123456 -> "1.2 L", 12345678 -> "1.2 Cr" */
function volText(v) {
  if (!isNum(v)) return "–";
  const x = Math.abs(Number(v));
  if (x >= 1e7) return (x / 1e7).toFixed(1) + " Cr";
  if (x >= 1e5) return (x / 1e5).toFixed(1) + " L";
  if (x >= 1e3) return (x / 1e3).toFixed(1) + " K";
  return String(Math.round(x));
}
const sourceWord = (s) => s === "real" ? "real" : "modelled";
const sideWord = (side) => side === "SHORT" ? "short" : "long";
const STAGE_TEXT = {
  FIRST_STOP: "First stop: the stop has not moved yet",
  BREAKEVEN: "Stop moved to breakeven",
  LOCKED: "35% of the move locked in",
  TRAILING: "Trailing behind the best price",
};
const STAGE_SHORT = { FIRST_STOP: "First stop", BREAKEVEN: "BE", LOCKED: "35%", TRAILING: "Trail" };
const REASON_TEXT = {
  STOP: "Stopped out at the first stop",
  BREAKEVEN_STOP: "Stopped at breakeven",
  LOCK_STOP: "Stopped with 35% locked",
  TRAIL_STOP: "Trailing stop",
  TARGET: "Target reached",
  SIGNAL_EXIT: "Strategy exit",
  SESSION_END: "Closed at the day's exit time",
  MANUAL: "Closed by you",
  DATA_END: "End of the stored data",
};
const REASON_SHORT = { STOP: "Stop", BREAKEVEN_STOP: "Breakeven", LOCK_STOP: "Lock", TRAIL_STOP: "Trail", TARGET: "Target", SIGNAL_EXIT: "Exit signal", SESSION_END: "Day end", MANUAL: "Closed", DATA_END: "Data end" };
const stageText = (stage) => STAGE_TEXT[stage === "INITIAL" ? "FIRST_STOP" : stage] || STAGE_TEXT.FIRST_STOP;
function reasonText(reason) {
  const gap = /^GAP_/.test(String(reason || ""));
  const base = REASON_TEXT[String(reason || "").replace(/^GAP_/, "")];
  if (!base) return reason ? String(reason) : "–";
  return gap ? base + " (the price gapped past it)" : base;
}
const reasonShort = (reason) => REASON_SHORT[String(reason || "").replace(/^GAP_/, "")] || "Out";

/* ── time ──────────────────────────────────────────────────────────── */
const tfMinutes = (tf) => Math.max(1, (parseInt(tf, 10) || 1) * (/h$/i.test(String(tf)) ? 60 : 1));
const unixOf = (iso) => Math.floor(Date.parse(iso) / 1000);
/* unix seconds -> "2026-11-03T11:42:00+05:30" */
const isoOf = (unix) => new Date((Math.floor(unix) + IST) * 1000).toISOString().slice(0, 19) + "+05:30";
const istParts = (unix) => { const d = new Date((Math.floor(unix) + IST) * 1000); return { d, s: d.toISOString() }; };
/* "Tue 03 Nov" (with the year: "Tue 03 Nov 2026") */
function dateText(unix, year) {
  const { d, s } = istParts(unix);
  return `${WEEK[d.getUTCDay()]} ${s.slice(8, 10)} ${MONTH[d.getUTCMonth()]}` + (year ? " " + s.slice(0, 4) : "");
}
/* A stored day "2026-11-03" -> "Tue 03 Nov 2026" */
const dayText = (day, year = true) => day ? dateText(Date.UTC(+day.slice(0, 4), +day.slice(5, 7) - 1, +day.slice(8, 10), 6) / 1000 - IST, year) : "–";
const hhmmOf = (unix) => istParts(unix).s.slice(11, 16);
const hhmmssOf = (unix) => istParts(unix).s.slice(11, 19);
/* The replay clock: "Tue 03 Nov 11:42" (seconds when asked); a hidden day reads "A hidden day 11:42". */
function clockText(unix, opts = {}) {
  if (!isNum(unix)) return "–";
  const t = opts.seconds ? hhmmssOf(unix) : hhmmOf(unix);
  return (opts.hidden ? "A hidden day" : dateText(unix)) + " " + t;
}
const readyText = (startUnix, hidden) => "Ready: " + clockText(startUnix, { hidden });
/* Bin start (unix seconds) of the candle holding `unix`; bins are anchored at 09:15 each day. */
function binStart(unix, tfm) {
  const rel = (((unix + IST) % 86400) + 86400) % 86400 - DAY_OPEN, w = tfm * 60;
  return unix - (((rel % w) + w) % w);
}
/* When the candle that holds `unix` closes: its bin end, or 15:30 for the day's short last bin. */
function binEnd(unix, tfm) {
  const b = binStart(unix, tfm), dayClose = unix - ((((unix + IST) % 86400) + 86400) % 86400) + DAY_OPEN + DAY_MINUTES * 60;
  return Math.min(b + tfm * 60, dayClose > b ? dayClose : b + tfm * 60);
}
/* "2:05" until the chart's candle closes, from the replay clock */
function countdownText(clockUnix, tfm) {
  if (!isNum(clockUnix)) return "";
  const s = ((((clockUnix + IST) % 86400) + 86400) % 86400) - DAY_OPEN;
  if (s < 0 || s >= DAY_MINUTES * 60) return "";
  const left = Math.max(0, binEnd(clockUnix, tfm) - clockUnix);
  return `${Math.floor(left / 60)}:${String(Math.floor(left % 60)).padStart(2, "0")}`;
}
/* Minutes from the minute after the cursor to the close of the chart's candle (Shift+Right). */
function stepMinutes(cursorUnix, tfm) {
  const next = cursorUnix + 60, o = Math.floor((((next + IST) % 86400) - DAY_OPEN) / 60);
  if (o < 0 || o >= DAY_MINUTES) return tfm;
  return Math.max(1, Math.min(tfm - (o % tfm), DAY_MINUTES - o));
}
/* The minute an event belongs to (its start, unix): a fill, and a close at a minute's open (by you, by the strategy, at the
   day's exit time) carry the start of that minute; every other event carries the close of its minute (SIM-API-2). */
function eventMinute(ev) {
  const t = unixOf(ev.ts), d = ev.data || {};
  if (ev.kind === "FILLED") return t;
  if (ev.kind === "CLOSED" && /^(MANUAL|SIGNAL_EXIT|SESSION_END)$/.test(String(d.exit_reason || ""))) return t;
  return t - 60;
}

/* ── candles ───────────────────────────────────────────────────────── */
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
/* The drawn path of a minute from its own open, high, low and close: open -> low -> high -> close when it closed up,
   open -> high -> low -> close when down. It never leaves the bar and ends on the real close. */
const minutePath = (b) => b[4] >= b[1] ? [b[1], b[3], b[2], b[4]] : [b[1], b[2], b[3], b[4]];
function partialMinute(bar, f) {
  const path = minutePath(bar), x = Math.min(1, Math.max(0, f)) * 3, seg = Math.min(2, Math.floor(x));
  const price = path[seg] + (path[seg + 1] - path[seg]) * (x - seg);
  const seen = path.slice(0, seg + 1).concat([price]);
  return [bar[0], bar[1], Math.max(...seen), Math.min(...seen), x >= 3 ? bar[4] : price, Math.round((bar[5] || 0) * Math.min(1, Math.max(0, f)))];
}

/* ── the replay clock ──────────────────────────────────────────────── */
/* Market minutes a frame of `dtMs` real milliseconds moves at `speed` (1x = one market minute a real minute). */
const playMinutes = (dtMs, speed) => Math.max(0, dtMs) * Math.max(0, speed) / 60000;
/* Real seconds one market minute takes at `speed`. */
const minuteSeconds = (speed) => 60 / Math.max(1, speed);
/* The speed in force: held to 10x while a trade is open when "Slow down in a trade" is on. */
const speedInForce = (speed, tradeOpen, slowInTrade) => (tradeOpen && slowInTrade) ? Math.min(speed, 10) : speed;
/* Minutes to ask for in one chunk: what fits into the next 1 s of play while an alert or a trade is open, else 3 s; 1 to 30. */
const chunkSize = (speed, busy) => Math.max(1, Math.min(30, Math.floor(speed * (busy ? 1 : 3) / 60)));
/* The next (dir 1) or previous (dir -1) speed of the list */
function speedStep(speeds, speed, dir) {
  const list = (speeds && speeds.length ? speeds : SPEEDS).slice().sort((a, b) => a - b);
  let i = list.indexOf(speed);
  if (i < 0) i = list.findIndex(s => s > speed) - (dir > 0 ? 1 : 0);
  return list[Math.max(0, Math.min(list.length - 1, i + (dir > 0 ? 1 : -1)))];
}
/* The chart timeframe after [ (dir -1) or ] (dir 1) */
function tfStep(tfs, tf, dir) {
  const list = tfs && tfs.length ? tfs : CHART_TFS, i = list.indexOf(tf);
  return list[Math.max(0, Math.min(list.length - 1, (i < 0 ? 0 : i) + dir))];
}

/* ── choosing ──────────────────────────────────────────────────────── */
function strategiesFor(opts, inst) {
  const kind = inst && inst.kind;
  return ((opts && opts.strategies) || []).filter(s => (s.kinds || []).includes(kind));
}
/* The start of a stored day at 09:15 IST, and the minute before it (the chart's `until` before the first Play). */
const startIsoOf = (day) => day ? day + "T09:15:00+05:30" : null;
const minuteBefore = (iso) => isoOf(unixOf(iso) - 60);
/* A start several trading days before the last stored one. */
const defaultDay = (days, back = 5) => days && days.length ? days[Math.max(0, days.length - 1 - back)] : null;
/* The stored day on or after `day` (the last one when none), so a typed date always lands on a day with bars. */
function snapDay(days, day) {
  if (!days || !days.length) return day || null;
  if (!day) return defaultDay(days);
  const d = days.find(x => x >= day);
  return d || days[days.length - 1];
}
function shiftDay(days, day, dir) {
  if (!days || !days.length) return null;
  const i = days.indexOf(snapDay(days, day));
  const j = i + dir;
  return j < 0 || j >= days.length ? null : days[j];
}
/* A random stored day, never the one shown and, when there is a choice, not one of the last two (they may be thin). */
function randomDay(days, current, rnd) {
  if (!days || !days.length) return null;
  const pool = days.length > 4 ? days.slice(0, -2) : days.slice();
  const pick = pool.filter(d => d !== current);
  if (!pick.length) return pool[0];
  return pick[Math.min(pick.length - 1, Math.floor((rnd || Math.random)() * pick.length))];
}
/* The resume offer's words: a live session cannot be replayed, it jumps to the present. */
function resumeLine(s) {
  const what = `${(s.instrument && s.instrument.label) || ""}, ${s.strategy_label || s.strategy} ${s.timeframe}`;
  return s.mode === "live"
    ? `${what}, live session started ${String(s.start || "").replace("T", " ").slice(0, 16)}: it will jump to now.`
    : `${what}, at ${s.cursor ? clockText(unixOf(s.cursor) + 60) : "–"}.`;
}
/* True while the market is open in Indian time on a weekday (holidays are not known here: the feed then says nothing). */
function marketOpenAt(nowMs) {
  const d = new Date(nowMs + IST * 1000), wd = d.getUTCDay(), s = d.getUTCHours() * 3600 + d.getUTCMinutes() * 60;
  return wd >= 1 && wd <= 5 && s >= DAY_OPEN && s < DAY_OPEN + DAY_MINUTES * 60;
}

/* Live mode: a plain sentence when the market is open but the newest stored minute is more than 5 minutes old (the feed
   is not writing minutes: a mock feed, a dropped socket), else null. */
function liveLagText(drawnUnix, nowMs) {
  if (!isNum(drawnUnix) || !marketOpenAt(nowMs) || nowMs / 1000 - drawnUnix <= 6 * 60) return null;
  return `Waiting for the feed: the newest stored minute is ${clockText(drawnUnix)}. New minutes appear here as the feed writes them.`;
}

/* ── the price scale (owner's first bug) ───────────────────────────── */
/* The range the chart shows: the visible candles' own range, widened by a level (entry, stop, target) only when it lies
   within `reach` (60%) of the candles' height beyond their range. A level farther away is not drawn into the scale: it
   is returned in `pinned`, to be shown as a tag at the pane's top or bottom edge. */
function scaleRule(range, levels, reach = 0.6) {
  if (!range || !isNum(range.minValue) || !isNum(range.maxValue)) return { range, pinned: [] };
  const lo0 = Number(range.minValue), hi0 = Number(range.maxValue), h = Math.max(hi0 - lo0, Math.abs(hi0) * 1e-4, 1e-9);
  let lo = lo0, hi = hi0;
  const pinned = [];
  for (const l of levels || []) {
    if (!l || !isNum(l.price)) continue;
    const p = Number(l.price);
    if (p < lo0 - reach * h) pinned.push({ name: l.name, label: l.label || l.name, price: p, edge: "bottom" });
    else if (p > hi0 + reach * h) pinned.push({ name: l.name, label: l.label || l.name, price: p, edge: "top" });
    else { lo = Math.min(lo, p); hi = Math.max(hi, p); }
  }
  return { range: { minValue: lo, maxValue: hi }, pinned };
}
/* "Target 24,110.00 ↑" */
const pinnedText = (p) => `${p.label} ${num(p.price)} ${p.edge === "top" ? "↑" : "↓"}`;

/* ── legend ────────────────────────────────────────────────────────── */
/* The legend's parts for a candle (and the one before it, for the change): what is shown, O H L C, change, volume. */
function legendParts(label, tf, c, prev, hasVolume) {
  if (!c) return { head: `${label} · ${tf}`, o: "–", h: "–", l: "–", c: "–", chg: "", chgTone: "", vol: "" };
  const base = prev ? prev[4] : c[1], chg = base ? (c[4] - base) / base * 100 : 0;
  return { head: `${label} · ${tf}`, o: num(c[1]), h: num(c[2]), l: num(c[3]), c: num(c[4]), chg: pctText(chg), chgTone: tone(chg), vol: hasVolume ? volText(c[5]) : "" };
}
const legendLine = (p) => `${p.head}   O ${p.o}  H ${p.h}  L ${p.l}  C ${p.c}  ${p.chg}` + (p.vol ? `  Vol ${p.vol}` : "");

/* ── keys ──────────────────────────────────────────────────────────── */
/* The key sheet: [what you press, what it does]. Number keys stay with the app's tabs. */
const KEYS = [
  ["Space", "Play or pause"], ["→", "One minute forward"], ["Shift + →", "To the close of the chart's candle"],
  ["N", "Jump to the next alert"], ["↑ ↓", "Faster, slower"], ["T", "Take the alert"], ["S", "Skip the alert"],
  ["X", "Close the trade"], ["F", "Fit the chart"], ["L", "Show or hide the bottom panel"], ["[ ]", "Chart timeframe down, up"],
  ["?", "This sheet"], ["Esc", "Close a sheet or a question"],
];
/* What a key press does on this tab (null: nothing). `e`: {key, code, shiftKey, ctrlKey, metaKey, altKey, tag, editable}. */
function keyAction(e) {
  const tag = String(e.tag || "").toUpperCase();
  if (e.ctrlKey || e.metaKey || e.altKey || e.editable || /^(INPUT|SELECT|TEXTAREA)$/.test(tag)) return null;
  const k = String(e.key || ""), code = String(e.code || "");
  if (k === "Escape") return "escape";
  if (k === "?" || (code === "Slash" && e.shiftKey)) return "help";
  if (code === "Space" || k === " ") return tag === "BUTTON" || tag === "A" ? null : "play";
  if (k === "ArrowDown" && e.shiftKey) return "play";
  if (k === "ArrowRight") return e.shiftKey ? "candle" : "step";
  if (k === "ArrowUp") return "faster";
  if (k === "ArrowDown") return "slower";
  if (k === "[") return "tf-down";
  if (k === "]") return "tf-up";
  const m = { n: "next", t: "take", s: "skip", x: "close", f: "fit", l: "panel" }[k.toLowerCase()];
  return m || null;
}

/* ── the dock's cards (texts only; the page puts them into fixed fields) ── */
const isOption = (x) => !!x && (x.unit ? x.unit === "lot" : /CALL|PUT/.test(String(x.action || "")));
const qtyText = (x) => {
  const q = x.qty ?? x.lot, unit = x.unit || (isOption(x) ? "lot" : "share");
  if (unit === "lot") return `one lot of ${q}`;
  return q === 1 ? "one share" : `${q} shares`;
};
/* Rupees (before charges) if the traded price were `price`: bought premium or long share gains when it rises. */
function rupeesAt(t, price) {
  if (!t || !isNum(price) || !isNum(t.entry_price)) return null;
  const sgn = isOption(t) || t.side !== "SHORT" ? 1 : -1;
  return sgn * (Number(price) - Number(t.entry_price)) * Number(t.qty || t.lot || 1);
}
const minutesLeft = (a, cursorIso) => isNum(a.expires_in_min) ? Number(a.expires_in_min)
  : (a.expires && cursorIso ? Math.max(0, Math.round((Date.parse(a.expires) - Date.parse(cursorIso) - 60000) / 60000)) : null);
/* The ticket for an alert: the action in large type, then for an option the PREMIUM plan first and the index triggers
   second; for a share one plan. */
function ticketModel(a, cursorIso) {
  if (!a) return null;
  const t = a.traded || {}, opt = isOption(a), left = minutesLeft(a, cursorIso);
  const m = {
    action: a.action || "", side: a.side === "SHORT" ? "short" : "long", option: opt,
    what: `${t.what || "–"} · ${qtyText(a)}`, source: sourceWord(t.source),
    buy: `${opt ? "Buy at" : (a.side === "SHORT" ? "Sell at" : "Buy at")} ₹${num(opt ? t.price : (t.price ?? a.entry))}`,
    stop: `Stop ₹${num(opt ? t.stop_price : a.stop)}`, target: `Target ₹${num(opt ? t.target_price : a.target)}`,
    risk: money(a.risk_inr), reward: money(a.reward_inr), costs: money(a.costs_inr),
    ratio: isNum(a.risk_inr) && isNum(a.reward_inr) && Number(a.risk_inr) > 0 ? `${(Number(a.reward_inr) / Number(a.risk_inr)).toFixed(1)} to 1` : "–",
    triggers: opt ? `Index triggers: entry ${num(a.entry)}, stop ${num(a.stop)}, target ${num(a.target)}` : "",
    left: left === null ? "" : (left <= 0 ? "This alert ends now." : `This alert ends in ${left} market minute${left === 1 ? "" : "s"}.`),
    implied: t.iv_source === "implied" ? "Stop and target prices are modelled on the volatility implied by the real price." : "",
    reasons: (a.reasons || []).map(String),
  };
  return m;
}
/* The position while a trade is open (or waiting for its fill). `mark`: the newest marks row [t, mark, mark_price, open_pnl]
   or null (then the trade's own figures). */
function positionModel(t, mark, exitSignal) {
  if (!t) return null;
  const opt = isOption(t);
  const head = { action: t.action || "", what: `${(t.traded && t.traded.what) || "–"} · ${qtyText(t)}`, source: sourceWord(t.source || (t.traded && t.traded.source)), option: opt };
  if (t.status === "PENDING_FILL") return { ...head, pending: true, note: "Waiting for the fill at the next minute's open." };
  const now = mark ? mark[1] : t.mark, nowPrice = mark ? mark[2] : t.mark_price, pnl = mark ? mark[3] : t.open_pnl;
  const risk = Number(t.risk_inr);
  const r = isNum(pnl) && risk > 0 ? Number(pnl) / risk : (isNum(t.open_r) ? Number(t.open_r) : null);
  const pct = isNum(pnl) && isNum(t.entry_price) && Number(t.entry_price) > 0 ? Number(pnl) / (Number(t.entry_price) * Number(t.qty || 1)) * 100 : null;
  const lo = Number(t.stop0 ?? t.stop), hi = Number(t.target), span = hi - lo;
  const along = (p) => isNum(p) && span ? Math.max(0, Math.min(100, (Number(p) - lo) / span * 100)) : null;
  const ent = Number(t.entry), way = Number(t.target0 ?? t.target) - ent;
  return {
    ...head, pending: false,
    inOut: opt ? `Premium ₹${num(t.entry_price)} → ₹${num(nowPrice)}` : `₹${num(t.entry_price ?? t.entry)} → ₹${num(nowPrice ?? now)}`,
    index: opt ? `Index ${num(t.entry)} → ${num(now)}` : "",
    pnl: signedMoney(pnl), pnlTone: tone(pnl), pct: pctText(pct), r: rText(r),
    stage: stageText(t.stage),
    stop: opt ? `Stop ₹${num(t.stop_price)} (index ${num(t.stop)})` : `Stop ₹${num(t.stop)}`,
    target: opt ? `Target ₹${num(t.target_price)} (index ${num(t.target)})` : `Target ₹${num(t.target)}`,
    minutes: isNum(t.minutes_in_trade) ? `${t.minutes_in_trade} min in the trade` : "",
    way: `${Math.round(Math.max(0, Math.min(1, Number(t.progress) || 0)) * 100)}% of the way to the target`,
    bar: { price: along(now), stop: along(t.stop), entry: along(ent), ticks: [0.25, 0.5, 0.8].map(k => along(ent + k * way)) },
    exitSignal: exitSignal ? "The strategy says close this trade." : "",
  };
}
/* The result of a closed trade, and what the rule did. */
function resultModel(t) {
  if (!t) return null;
  const opt = isOption(t), win = Number(t.net) >= 0;
  return {
    action: t.action || "", what: `${(t.traded && t.traded.what) || "–"} · ${qtyText(t)}`, won: win,
    reason: reasonText(t.exit_reason), net: signedMoney(t.net), netTone: tone(t.net), r: rText(t.r_multiple), charges: money(t.charges),
    inOut: opt ? `Premium ₹${num(t.entry_price)} → ₹${num(t.exit_price)}, index ${num(t.entry)} → ${num(t.exit)}` : `₹${num(t.entry)} → ₹${num(t.exit)}`,
    rule: ruleWords(t),
  };
}
function ruleWords(t) {
  const r = String(t.exit_reason || "").replace(/^GAP_/, "");
  if (r === "TARGET") return "The target was reached and the trade closed by itself.";
  if (r === "STOP") return "The first stop was hit before the stop could move.";
  if (r === "BREAKEVEN_STOP") return "The stop had moved to breakeven at 25% of the way; the price came back to it.";
  if (r === "LOCK_STOP") return "The stop had locked 35% of the move at 50% of the way; the price came back to it.";
  if (r === "TRAIL_STOP") return "The stop was trailing the best price after 80% of the way; the price came back to it.";
  if (r === "SIGNAL_EXIT") return "The strategy's exit signal closed it at the next minute's open.";
  if (r === "SESSION_END") return "Nothing is carried overnight: it closed at the day's exit time.";
  if (r === "MANUAL") return "You closed it; it filled at the next minute's open.";
  if (r === "DATA_END") return "The stored data ended.";
  return "";
}
/* The session's numbers. */
function numbersModel(s, capital) {
  s = s || {};
  const n = Number(s.trades) || 0, w = Number(s.winners) || 0;
  return [
    ["Trades", String(n), ""], ["Winners", n ? `${w} of ${n}` : "–", ""], ["Net", signedMoney(s.net ?? 0), tone(s.net)],
    ["Average win", money(s.avg_win), tone(s.avg_win)], ["Average loss", money(s.avg_loss), tone(s.avg_loss)],
    ["Best", money(s.best), tone(s.best)], ["Worst", money(s.worst), tone(s.worst)], ["Charges", money(s.charges), ""],
    ["Equity", money(s.equity ?? capital), ""],
  ];
}
/* A closed trade as one row of the Trades tab: premium in and out as well as the index levels (SIM-09). */
function tradeCells(t) {
  const opt = isOption(t);
  return {
    when: `${t.entry_ts ? hhmmOf(unixOf(t.entry_ts)) : "–"} → ${t.exit_ts ? hhmmOf(unixOf(t.exit_ts)) : "–"}`,
    day: t.entry_ts ? dateText(unixOf(t.entry_ts)) : "–",
    what: `${t.action || ""} ${(t.traded && t.traded.what) || ""}`.trim(),
    price: `₹${num(t.entry_price ?? t.entry)} → ₹${num(t.exit_price ?? t.exit)}`,
    index: opt ? `${num(t.entry)} → ${num(t.exit)}` : "",
    reason: reasonText(t.exit_reason), net: signedMoney(t.net), netTone: tone(t.net), r: rText(t.r_multiple),
    source: sourceWord(t.source || (t.traded && t.traded.source)),
  };
}
/* An event as a line of the log: its own minute, a plain word for its kind, its text. */
const EVENT_WORD = { ALERT: "Alert", ALERT_EXPIRED: "Alert ended", FILLED: "Filled", STAGE: "Stop moved up", STOP_MOVED: "Stop", TARGET_MOVED: "Target",
  EXIT_SIGNAL: "Exit signal", CLOSED: "Closed", CANCELLED: "Cancelled", DAY_END: "Day end", DATA_END: "Data end" };
const eventWord = (kind) => EVENT_WORD[kind] || String(kind || "").replace(/_/g, " ").toLowerCase();
function eventCells(ev) {
  const t = unixOf(ev.ts);
  return { time: isNum(t) ? hhmmOf(t) : "–", day: isNum(t) ? dateText(t) : "", kind: String(ev.kind || "").toLowerCase(), word: eventWord(ev.kind), text: String(ev.text || "") };
}

/* ── the chart's labels and markers ────────────────────────────────── */
/* Labels of the price lines for an alert (kind "alert") or an open trade: what they are and the rupees. */
function levelLabels(src, kind) {
  if (!src) return null;
  if (kind === "alert") {
    return { entry: `Alert entry ${num(src.entry)}`, stop: `Stop ${num(src.stop)} · ${rupees(-Math.abs(Number(src.risk_inr) || 0))}`, target: `Target ${num(src.target)} · ${rupees(Math.abs(Number(src.reward_inr) || 0))}` };
  }
  const opt = isOption(src);
  return {
    entry: opt ? `In ${num(src.entry)} · ₹${num(src.entry_price)}` : `In ${num(src.entry)}`,
    stop: `Stop ${num(src.stop)} · ${rupees(rupeesAt(src, src.stop_price ?? src.stop))}`,
    target: `Target ${num(src.target)} · ${rupees(rupeesAt(src, src.target_price ?? src.target))}`,
    stop0: "First stop", target0: "First target",
  };
}
/* A chart marker for an event (null when the event has none): the minute it belongs to (the chart puts it on that
   minute's candle in its own timeframe), where, which shape, which theme colour, and its words. */
function markerFor(ev, side) {
  const long = side !== "SHORT", d = ev.data || {}, t = eventMinute(ev);
  if (ev.kind === "ALERT") return { minute: t, position: long ? "belowBar" : "aboveBar", shape: long ? "arrowUp" : "arrowDown", color: "--accent2", text: "Alert" };
  if (ev.kind === "FILLED") return { minute: t, position: long ? "aboveBar" : "belowBar", shape: "circle", color: "--cyan", text: "In " + num(d.entry_price ?? d.entry) };
  if (ev.kind === "STAGE") return { minute: t, position: long ? "belowBar" : "aboveBar", shape: "square", color: "--violet", text: STAGE_SHORT[d.stage] || "Stop" };
  if (ev.kind === "CLOSED") {
    const net = Number(d.net);
    return { minute: t, position: long ? "aboveBar" : "belowBar", shape: "square", color: net >= 0 ? "--ok" : "--bad",
      text: [reasonShort(d.exit_reason), isNum(d.net) ? rupees(net) : null, isNum(d.r_multiple) ? rText(d.r_multiple).replace(" R", "R") : null].filter(Boolean).join(" · ") };
  }
  return null;
}
/* The equity line of the Numbers tab as an SVG path in a w x h box, from the closed trades' nets (oldest first). */
function equityPath(nets, capital, w, h) {
  const pts = [Number(capital) || 0];
  (nets || []).forEach(n => pts.push(pts[pts.length - 1] + (Number(n) || 0)));
  if (pts.length < 2) return "";
  const lo = Math.min(...pts), hi = Math.max(...pts), span = hi - lo || 1;
  return pts.map((v, i) => `${i ? "L" : "M"}${(i / (pts.length - 1) * w).toFixed(1)},${(h - (v - lo) / span * h).toFixed(1)}`).join(" ");
}

const api = {
  IST, DAY_OPEN, DAY_MINUTES, CHART_TFS, SPEEDS, WEEK, MONTH, KEYS, STAGE_TEXT, REASON_TEXT,
  isNum, esc, num, money, rupees, signedMoney, rText, pctText, tone, volText, sourceWord, sideWord, stageText, reasonText, reasonShort,
  tfMinutes, unixOf, isoOf, dateText, dayText, hhmmOf, hhmmssOf, clockText, readyText, binStart, binEnd, countdownText, stepMinutes, eventMinute,
  mergeBar, foldMinutes, minutePath, partialMinute, playMinutes, minuteSeconds, speedInForce, chunkSize, speedStep, tfStep,
  strategiesFor, startIsoOf, minuteBefore, defaultDay, snapDay, shiftDay, randomDay, resumeLine, marketOpenAt, liveLagText,
  scaleRule, pinnedText, legendParts, legendLine, keyAction, isOption, qtyText, rupeesAt, minutesLeft,
  ticketModel, positionModel, resultModel, ruleWords, numbersModel, tradeCells, eventWord, eventCells, levelLabels, markerFor, equityPath,
};
if (typeof module !== "undefined" && module.exports) module.exports = api;
if (root) root.SimCore = api;
})(typeof window !== "undefined" ? window : null);
