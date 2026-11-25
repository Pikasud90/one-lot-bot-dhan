/* The Volume tab (US-090): the Indian market's volume, read from the large stocks, as a watchdog page.
   Calls the engine's JSON (.agile/stories/VOL-API.md). Index bars carry no volume, so everything here is the
   stocks' turnover (volume x price, in rupees). With ?volfake=1 the calls go to src/onelot/ui/volume_fake.js
   instead (demo data, not market data). Helpers stand at column 0 inside the IIFE so the node tests can lift them. */
(() => {
"use strict";
const OL = window.OneLot;
if (!OL) return;
const { $, esc, store, tabShown } = OL;
const IST = 19800;                 // seconds: the chart reads Indian time
const POLL_MS = 20000;
const REASK_MS = 2000;             // while the engine prepares a session (`building`), live or a past date
const NO_BOOK = "The order book is not shown now: it is switched off, the market is closed, or the broker sent no quotes.";
const CLOSED_TEXT = "The market is closed. This is the last session. The watch starts again at 09:15.";
const WATCH_NOT_ALERT = "A watch alert is not a trade alert: it tells you where unusual volume is, not what to buy.";
const MONTH = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const STATE_TEXT = { quiet: "Quiet", normal: "Normal", busy: "Busy", spike: "Spike" };
const STATE_HELP = {
  quiet: "Well below the usual volume for this time of day (under the quiet setting, 0.7 unless changed).",
  normal: "About the usual volume for this time of day.",
  busy: "At least 1.5 times the usual volume for this time of day.",
  spike: "An unusual jump: far above what the last 60 sessions had at this time of day.",
};

/* ── pure helpers (unit-tested in node) ─────────────────────────────── */
const num = (v) => (v === null || v === undefined || v === "" || !Number.isFinite(Number(v))) ? null : Number(v);
const crore = (v) => { const n = num(v); if (n === null) return "–"; const c = Math.abs(n) / 1e7; return (n < 0 ? "−" : "") + "₹" + (c >= 100 ? Math.round(c).toLocaleString("en-IN") : (c >= 1 ? c.toFixed(1) : c.toFixed(2))) + " crore"; };
const times = (v) => num(v) === null ? "–" : num(v).toFixed(1) + " times";
const timesNormal = (v) => num(v) === null ? "–" : num(v).toFixed(1) + " times normal";
const buyersPct = (s) => num(s) === null ? "–" : Math.round(num(s) * 100) + "%";
const buyersText = (s) => num(s) === null ? "Buyers' share not known yet" : "Buyers " + Math.round(num(s) * 100) + "%";
const stateWord = (s) => STATE_TEXT[s] || "–";
const unixOf = (iso) => Math.floor(Date.parse(iso) / 1000);
const clockOf = (iso) => iso ? String(iso).slice(11, 16) : "–";
const dayWord = (ymd) => { const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(ymd || "")); return m ? `${Number(m[3])} ${MONTH[Number(m[2]) - 1]}` : "–"; };
const signed = (v, d = 2) => num(v) === null ? "–" : (num(v) > 0 ? "+" : (num(v) < 0 ? "−" : "")) + Math.abs(num(v)).toFixed(d);
const clampHalf = (p) => Math.round(Math.max(-1, Math.min(1, num(p) || 0)) * 50);   // percent of the bar's width, either side of the middle
const sharePct = (s) => Math.max(0, Math.min(100, Math.round((num(s) === null ? 0.5 : num(s)) * 100)));
const closedBanner = (d, date) => (d && d.market_open === false && !date) ? CLOSED_TEXT : "";
const statusLine = (d) => {
  if (!d) return "";
  const when = d.source === "stored" ? `Stored session of ${dayWord(d.session)}` : "Live now";
  const last = d.now && d.now.minute ? `last closed minute ${d.now.minute}` : "no closed minute yet";
  return `${when}, ${last}`;
};
const reaskAfter = (d) => (d && d.building === true) ? REASK_MS : 0;
const sideOfMinute = (row) => num(row[3]) !== null && num(row[4]) !== null && row[3] >= row[4] ? "buy" : "sell";
function sortStocks(list, key, dir) {
  const sign = dir === "asc" ? 1 : -1, rows = (list || []).slice();
  const val = (r) => { const v = r[key]; return typeof v === "string" ? v.toLowerCase() : num(v); };
  rows.sort((a, b) => {
    const x = val(a), y = val(b);
    if (x === null && y === null) return 0;
    if (x === null) return 1;
    if (y === null) return -1;
    if (typeof x === "string" || typeof y === "string") return sign * String(x).localeCompare(String(y));
    return sign * (x - y);
  });
  return rows;
}
/* The contract's minute rows [t, turnover, expected, buying, selling, net flow, rvol_5m, z] as chart points (time already in IST). */
function chartPoints(minutes, indexMinutes) {
  const rows = (minutes || []).filter(r => Array.isArray(r) && num(r[0]) !== null).slice().sort((a, b) => a[0] - b[0]);
  const keep = (key) => rows.filter(r => num(r[key]) !== null).map(r => ({ time: r[0] + IST, value: Number(r[key]) }));
  const index = (indexMinutes || []).filter(r => Array.isArray(r) && num(r[0]) !== null && num(r[1]) !== null).slice().sort((a, b) => a[0] - b[0]).map(r => ({ time: r[0] + IST, value: Number(r[1]) }));
  return {
    turnover: rows.filter(r => num(r[1]) !== null).map(r => ({ time: r[0] + IST, value: Number(r[1]), side: sideOfMinute(r) })),
    expected: keep(2), net: keep(5), index,
  };
}
/* The chart time of the minute an alert belongs to: the last drawn minute at or before the alert's time. */
function alertTime(ts, times) {
  const u = unixOf(ts) + IST;
  if (!Number.isFinite(u)) return null;
  let best = null;
  for (const t of times) if (t <= u && (best === null || t > best)) best = t;
  return best;
}
const minuteReadout = (row, idx) => !row ? "" :
  `${clockOf(new Date((row[0] + IST) * 1000).toISOString())}: turnover ${crore(row[1])} (${timesNormal(num(row[2]) ? row[1] / row[2] : null)}), `
  + `${buyersText(num(row[3]) !== null && num(row[4]) !== null && row[3] + row[4] > 0 ? row[3] / (row[3] + row[4]) : null)}, `
  + `net flow so far ${crore(row[5])}` + (idx === null || idx === undefined ? "" : `, index ${Number(idx).toLocaleString("en-IN", { maximumFractionDigits: 1 })}`);
const bookIsEmpty = (b) => !b || !Array.isArray(b.stocks) || !b.stocks.length;
const stateChip = (s) => `<span class="vol-chip ${esc(STATE_TEXT[s] ? s : "normal")}" title="${esc(STATE_HELP[s] || "")}">${esc(stateWord(s))}</span>`;
const shareBar = (s) => `<span class="vol-bs" title="${esc(buyersText(s))}"><i style="width:${sharePct(s)}%"></i><u></u></span>`;
const PRESSURE_FLOOR = 0.05;   // only the fallback for a demo answer without settings: the engine's own floor comes in settings.pressure_floor
const pressureWord = (p, floor = PRESSURE_FLOOR) => num(p) === null ? "–" : (p > floor ? "Buying pressure" : (p < -floor ? "Selling pressure" : "Balanced"));
const pressBar = (p5, pd) => `<div class="vol-press" aria-hidden="true"><i class="${num(p5) < 0 ? "sell" : "buy"}" style="${num(p5) < 0 ? "right" : "left"}:50%;width:${Math.abs(clampHalf(p5))}%"></i><b class="${num(pd) < 0 ? "sell" : "buy"}" style="${num(pd) < 0 ? "right" : "left"}:50%;width:${Math.abs(clampHalf(pd))}%"></b><u></u></div>`;
const nameOf = (stocks, key) => { const s = (stocks || []).find(x => x.key === key); return s ? s.label : key; };
const directionWord = (d) => d === "BUYING" ? "mostly buying" : (d === "SELLING" ? "mostly selling" : "two-way trade");
const tile = (label, value, sub, extra = "") => `<div class="tile"><small>${esc(label)}</small><b>${value}</b>${sub ? `<i>${esc(sub)}</i>` : ""}${extra}</div>`;

function nowTilesHtml(now) {
  if (!now) return `<div class="card muted">No closed minute yet: the numbers appear after the first minute of the session.</div>`;
  const flow = (label, s) => tile(label, esc(buyersPct(s)), buyersText(s), `<div class="vol-split" aria-hidden="true"><i style="width:${sharePct(s)}%"></i><u></u></div>`);
  return tile("Volume now (1 minute)", esc(times(now.rvol_1m)), "of the normal for this minute")
    + tile("Last 5 minutes", esc(times(now.rvol_5m)), "of the normal for these minutes")
    + tile("Day so far", esc(times(now.rvol_day)), "of the normal for the day so far")
    + flow("Buyers, last 5 minutes", now.flow_share_5m)
    + flow("Buyers, day so far", now.flow_share_day)
    + tile("Stocks up and down", `<span class="pos">${esc(now.up ?? "–")}</span> / <span class="neg">${esc(now.down ?? "–")}</span>`, `${now.flat ?? 0} unchanged`)
    + tile("Turnover today", esc(crore(now.turnover_day)), `normal by now: ${crore(now.expected_day)}`);
}
function headlineHtml(d) {
  const now = d.now;
  if (!now) return `<div class="vol-head"><span class="vol-chip normal">No data</span> <span class="vol-headtext">No closed minute yet.</span></div>`;
  const basket = d.basket ? ` <span class="muted">Watching ${esc(d.basket.label || "")}.</span>` : "";
  return `<div class="vol-head">${stateChip(now.state)} <span class="vol-headtext">${esc(now.text || "")}</span>${basket}</div>`
    + `<div class="muted vol-help">${esc(STATE_HELP[now.state] || "")}</div>`;
}
const SORT_COLS = [
  ["label", "Stock", "l"], ["last", "Last", ""], ["chg_pct", "Change", ""], ["turnover_day", "Turnover today", ""], ["share", "Share of basket", ""],
  ["rvol_5m", "Volume, 5 min", ""], ["rvol_day", "Volume, day", ""], ["z", "Spike score", ""], ["flow_share_5m", "Buyers, 5 min", "b"], ["state", "State", "l"],
];
function stocksTableHtml(stocks, sort) {
  const rows = sortStocks(stocks, sort.key, sort.dir);
  if (!rows.length) return `<div class="muted">No stock has bars yet.</div>`;
  const head = SORT_COLS.map(([k, label, c]) => {
    const on = k === sort.key, arrow = on ? (sort.dir === "asc" ? " ▲" : " ▼") : "";
    return `<th class="${c}" data-sort="${esc(k)}" tabindex="0" role="button" aria-sort="${on ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}" title="Sort by ${esc(label)}">${esc(label)}${arrow}</th>`;
  }).join("");
  const body = rows.map(s => `<tr><td class="l"><b>${esc(s.label || s.key)}</b></td><td>${num(s.last) === null ? "–" : Number(s.last).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>`
    + `<td class="${num(s.chg_pct) > 0 ? "pos" : (num(s.chg_pct) < 0 ? "neg" : "")}">${signed(s.chg_pct)}%</td><td>${esc(crore(s.turnover_day))}</td><td>${num(s.share) === null ? "–" : Math.round(s.share * 100) + "%"}</td>`
    + `<td>${esc(times(s.rvol_5m))}</td><td>${esc(times(s.rvol_day))}</td><td>${signed(s.z, 1)}</td>`
    + `<td class="b">${shareBar(s.flow_share_5m)} <span class="vol-bp">${esc(buyersPct(s.flow_share_5m))}</span></td><td class="l">${stateChip(s.state)}</td></tr>`).join("");
  return `<div class="scroll"><table class="vol-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}
function indexCardHtml(ix, stocks, floor) {
  const top = (ix.top || []).map(t => `${esc(nameOf(stocks, t[0]))} ${Math.round(Number(t[1]) * 100)}%`).join(", ");
  const train = ix.trade === false ? ` <span class="vol-train">Training only</span>` : "";
  return `<div class="card vol-index"><h3>${esc(ix.label || ix.key)}${train}</h3>`
    + `<div class="vol-pword ${num(ix.pressure_5m) < 0 ? "neg" : (num(ix.pressure_5m) > 0 ? "pos" : "")}">${esc(pressureWord(ix.pressure_5m, floor))}</div>`
    + `<div class="vol-presslabels"><span>Selling</span><span>Buying</span></div>${pressBar(ix.pressure_5m, ix.pressure_day)}`
    + `<div class="muted vol-pnums">Last 5 minutes (thick bar): ${esc(signed(ix.pressure_5m))}. Day so far (thin bar): ${esc(signed(ix.pressure_day))}. Net buying or selling in units of normal volume.</div>`
    + (num(ix.r2) === null ? "" : `<div>The basket explains ${Math.round(ix.r2 * 100)}% of its minute moves.</div>`)
    + (top ? `<div class="muted">Biggest influence: ${top}.</div>` : "") + (ix.text ? `<div class="vol-ptext">${esc(ix.text)}</div>` : "") + `</div>`;
}
function indicesHtml(d) {
  const list = d.indices || [];
  return list.length ? `<div class="vol-grid">${list.map(ix => indexCardHtml(ix, d.stocks, d.settings && d.settings.pressure_floor)).join("")}</div>` : `<div class="card muted">No index pressure yet.</div>`;
}
function bookHtml(book) {
  if (bookIsEmpty(book)) return `<p class="vol-nobook">${esc(NO_BOOK)}</p>`;
  const rows = book.stocks.map(s => `<tr><td class="l"><b>${esc(s.label || s.key)}</b></td><td>${num(s.bid_qty) === null ? "–" : Number(s.bid_qty).toLocaleString("en-IN")}</td><td>${num(s.ask_qty) === null ? "–" : Number(s.ask_qty).toLocaleString("en-IN")}</td>`
    + `<td class="b">${shareBar(s.bid_share)} <span class="vol-bp">${esc(buyersPct(s.bid_share))}</span></td><td>${num(s.spread_bps) === null ? "–" : Number(s.spread_bps).toFixed(1) + " bps"}</td></tr>`).join("");
  return `<div class="vol-bookhead"><b>Buyers' share of resting orders: ${esc(buyersPct(book.bid_share))}</b> <span class="muted">at ${esc(clockOf(book.as_of))}</span></div>`
    + `<div class="vol-split big" aria-hidden="true"><i style="width:${sharePct(book.bid_share)}%"></i><u></u></div>`
    + `<div class="scroll"><table class="vol-table"><thead><tr><th class="l">Stock</th><th>Waiting to buy</th><th>Waiting to sell</th><th class="b">Buyers' share</th><th>Gap between prices</th></tr></thead><tbody>${rows}</tbody></table></div>`
    + (book.note ? `<div class="muted vol-note">${esc(book.note)}</div>` : "");
}
function alertCardHtml(a) {
  const dir = a.direction === "BUYING" ? "buy" : (a.direction === "SELLING" ? "sell" : "two");
  return `<div class="card vol-alert ${dir}"><div class="vol-alert-top"><span class="vol-when">${esc(a.session ? dayWord(a.session) + " " : "")}${esc(clockOf(a.ts))}</span> <b>${esc(a.title || "")}</b></div>`
    + (a.text ? `<div>${esc(a.text)}</div>` : "") + (a.evidence ? `<div class="muted vol-evidence">${esc(a.evidence)}</div>` : "") + `</div>`;
}
const newestFirst = (alerts) => (alerts || []).slice().sort((a, b) => String(b.ts).localeCompare(String(a.ts)));
function alertsHtml(alerts) {
  const list = newestFirst(alerts);
  return list.length ? list.map(a => alertCardHtml(a)).join("") : `<div class="muted">No watch alerts in this session.</div>`;
}
function earlierHtml(res) {
  if (res === null || res === undefined) return "";
  const list = newestFirst(res.alerts);
  return list.length ? list.map(a => alertCardHtml(a)).join("") : `<div class="muted">No earlier alerts are stored yet.</div>`;
}
function rangeText(b) {
  return `${esc(b.from)} to ${esc(b.to)} (${Math.round(Number(b.share) * 100)}% of a normal day)`;
}
function profileHtml(p) {
  if (!p) return `<div class="muted">The day's shape is not available yet.</div>`;
  const list = (items) => (items || []).length ? `<ul>${items.map(b => `<li>${rangeText(b)}</li>`).join("")}</ul>` : `<div class="muted">–</div>`;
  const days = (p.weekdays || []).map(w => `<tr><td class="l">${esc(w.day)}</td><td>${esc(times(w.rvol))}</td><td class="l muted">${num(w.rvol) === null ? "" : (w.rvol >= 1.05 ? "busier than average" : (w.rvol <= 0.95 ? "quieter than average" : "about average"))}</td></tr>`).join("");
  const ex = p.expiry ? `<p>${esc(p.expiry.note || "Expiry days")}: ${esc(times(p.expiry.expiry_day_rvol))} the usual volume; other days ${esc(times(p.expiry.other_days_rvol))}.</p>` : "";
  return (p.text ? `<p class="vol-ptext">${esc(p.text)}</p>` : "")
    + `<div class="vol-two"><div><h4>Busiest half hours</h4>${list(p.busiest)}</div><div><h4>Quietest half hours</h4>${list(p.quietest)}</div></div>`
    + (days ? `<h4>Weekdays (volume against the average day)</h4><div class="scroll"><table class="vol-table small"><tbody>${days}</tbody></table></div>` : "") + ex
    + `<div class="muted">Based on the last ${esc(p.sessions)} full sessions.</div>`;
}
function studyHtml(s) {
  if (!s) return `<div class="muted">What history says is not available yet.</div>`;
  const facts = (s.facts || []).map(f => `<li>${esc(f.text)}</li>`).join("");
  const apd = s.alerts_per_day ? `<p>On a normal day the watch raises about ${esc(s.alerts_per_day.market)} market-wide alerts and ${esc(s.alerts_per_day.stock)} single-stock alerts.</p>` : "";
  return (facts ? `<ul class="vol-facts">${facts}</ul>` : "") + apd + (s.note ? `<div class="muted">The study: ${esc(s.note)}</div>` : "");
}
function missingHtml(basket) {
  const m = basket && basket.missing || [];
  return m.length ? `<div class="muted vol-note">Not in the picture (no bars yet): ${m.map(esc).join(", ")}.</div>` : "";
}
function notesHtml(notes) {
  return (notes || []).length ? `<ul class="muted vol-notes">${notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul>` : "";
}
const LEGEND = [
  ["Turnover", "Rupees traded: the number of shares times their price. Rupees can be added across stocks, shares cannot."],
  ["Volume against normal (relative volume)", "How much was traded compared with the usual for this same minute of the day, taken from the last 20 full sessions. 1.0 times is usual, 2.0 times is double."],
  ["Buyers' share", ""],
  ["Spike", "The last 5 minutes are far above what the last 60 sessions had at this time of day. It says unusual, not up or down."],
  ["Pressure on an index", "The net buying or selling of the stocks that move the index, weighted by how much each moves it, in units of normal volume. Above zero is net buying, below zero net selling."],
  ["Order book", "Orders waiting at the best five price steps. It shows intentions, which can be pulled at any moment, so treat it as a hint only."],
];
const SPLIT_TEXT = {
  bvc: "Each one-minute bar's turnover is split by the minute's price move against the usual size of recent minute moves: a move well above the usual counts mostly as buying, a fall mostly as selling. It is an estimate from one-minute bars, not a count of buyers.",
  clv: "Each one-minute bar's turnover is split by where it closed between its low and high: a close near the high counts as buying. It is an estimate from one-minute bars, not a count of buyers.",
  tick: "Each one-minute bar's turnover counts as buying when the minute closed above where it opened, as selling when it closed below, and half and half when unchanged. It is an estimate from one-minute bars, not a count of buyers.",
};
const splitText = (rule) => SPLIT_TEXT[rule] || "Each one-minute bar's turnover is split into buying and selling by its price move. It is an estimate from one-minute bars, not a count of buyers.";
const legendHtml = (rule) => `<dl class="vol-legend">${LEGEND.map(([t, x]) => `<dt>${esc(t)}</dt><dd>${esc(t === "Buyers' share" ? splitText(rule) : x)}</dd>`).join("")}</dl><p class="vol-warn">${esc(WATCH_NOT_ALERT)}</p>`;
const demoHtml = () => `<div class="card vol-demo"><b>Demo data, not market data.</b> This tab is running on a built-in fake of the engine (?volfake=1).</div>`;
const errorText = (e, have) => `${have ? "Could not refresh: " : "The volume watch is not answering: "}${String((e && e.message) || e).replace(/\.+$/, "")}${have ? ". Showing the last picture." : "."}`;
const indexOptions = (d, sel) => {
  const seen = new Set(), items = [];
  const add = (k, l) => { if (k && !seen.has(k)) { seen.add(k); items.push([k, l || k]); } };
  add("NIFTY", "NIFTY 50"); (d && d.indices || []).forEach(i => add(i.key, i.label)); if (d && d.index) add(d.index.key, d.index.label);
  return items.map(([k, l]) => `<option value="${esc(k)}"${k === sel ? " selected" : ""}>${esc(l)}</option>`).join("");
};
/* Every section of the page as html, from one answer of /api/volume. */
function sectionsHtml(d, sort, date) {
  return {
    banner: closedBanner(d, date) ? `<div class="card vol-closed">${esc(closedBanner(d, date))}</div>` : "",
    status: esc(statusLine(d)),
    headline: headlineHtml(d), tiles: nowTilesHtml(d.now), stocks: stocksTableHtml(d.stocks, sort) + missingHtml(d.basket), indices: indicesHtml(d),
    book: bookHtml(d.book), alerts: alertsHtml(d.alerts), notes: notesHtml(d.notes),
  };
}

/* ── state ──────────────────────────────────────────────────────────── */
const S = { reask: 0, data: null, profile: null, study: null, earlier: null, fake: null, busy: false, err: "", date: "", index: store.get("volume.index", "NIFTY") || "NIFTY",
  sort: { key: "turnover_day", dir: "desc" }, chart: null, profToday: null, series: null, prof: null, rows: new Map(), idx: new Map(), profAt: 0, ready: false };
const cssv = (n) => (window.Charts && window.Charts.cssVar ? window.Charts.cssVar(n) : getComputedStyle(document.documentElement).getPropertyValue(n).trim());
const today = () => new Date(Date.now() + IST * 1000).toISOString().slice(0, 10);

/* ── calls ──────────────────────────────────────────────────────────── */
async function getJson(url) {
  if (S.fake) return S.fake.handle(url);
  const r = await fetch(url);
  if (!r.ok) {
    let m = r.status === 404 ? "the volume watch is not running in this build" : `the desk answered ${r.status}`;
    try { const b = await r.json(); if (b && b.detail) m = String(b.detail); } catch (e) {}
    throw new Error(m);
  }
  return r.json();
}
const volumeUrl = () => "/api/volume?index=" + encodeURIComponent(S.index) + (S.date ? "&date=" + encodeURIComponent(S.date) : "");

/* ── charts ─────────────────────────────────────────────────────────── */
function chartOpts() {
  return {
    layout: { background: { type: "solid", color: cssv("--panel") }, textColor: cssv("--plot-font"), fontFamily: "JetBrains Mono, Menlo, monospace", fontSize: 14, attributionLogo: false },
    grid: { vertLines: { color: cssv("--grid") }, horzLines: { color: cssv("--grid") } },
    rightPriceScale: { borderColor: cssv("--line2") },
    leftPriceScale: { visible: true, borderColor: cssv("--line2") },
    timeScale: { borderColor: cssv("--line2"), timeVisible: true, secondsVisible: false, rightOffset: 3 },
    localization: { locale: "en-IN" },
  };
}
function ensureMain() {
  const el = $("vol-chart");
  if (!el) return false;
  if (S.chart) return true;
  const LW = window.LightweightCharts;
  if (!LW) { el.innerHTML = `<div class="muted vol-pad">The chart library is missing (static/vendor).</div>`; return false; }
  const chart = S.chart = LW.createChart(el, { ...chartOpts(), autoSize: true });
  const sr = S.series = {};
  sr.bars = chart.addSeries(LW.HistogramSeries, { priceLineVisible: false, lastValueVisible: false, priceFormat: { type: "custom", formatter: (v) => crore(v), minMove: 1 } }, 0);
  sr.exp = chart.addSeries(LW.LineSeries, { color: cssv("--accent2"), lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, priceFormat: { type: "custom", formatter: (v) => crore(v), minMove: 1 } }, 0);
  sr.net = chart.addSeries(LW.BaselineSeries, { baseValue: { type: "price", price: 0 }, lineWidth: 2, priceLineVisible: false, lastValueVisible: false, priceFormat: { type: "custom", formatter: (v) => crore(v), minMove: 1 },
    topLineColor: cssv("--ok"), topFillColor1: cssv("--ok-bg"), topFillColor2: cssv("--ok-bg"), bottomLineColor: cssv("--bad"), bottomFillColor1: cssv("--bad-bg"), bottomFillColor2: cssv("--bad-bg") }, 1);
  sr.idx = chart.addSeries(LW.LineSeries, { color: cssv("--violet"), lineWidth: 2, priceScaleId: "left", priceLineVisible: false, lastValueVisible: true }, 1);
  sr.marks = LW.createSeriesMarkers(sr.bars, []);
  try { const panes = chart.panes(); if (panes[1]) panes[1].setHeight(170); } catch (e) {}
  chart.subscribeCrosshairMove((p) => {
    const row = p && p.time !== undefined ? S.rows.get(p.time) : null, out = $("vol-readout");
    if (out) out.textContent = row ? minuteReadout(row, S.idx.get(p.time)) : lastReadout();
  });
  return true;
}
function lastReadout() {
  const rows = S.data && S.data.minutes || [];
  if (!rows.length) return "Move the pointer over the chart to read a minute.";
  const r = rows[rows.length - 1];
  return "Latest minute " + minuteReadout(r, S.idx.get(r[0] + IST));
}
function drawMain() {
  const d = S.data;
  if (!d || !ensureMain()) return;
  const p = chartPoints(d.minutes, d.index && d.index.minutes), sr = S.series;
  const up = cssv("--ok"), dn = cssv("--bad");
  sr.bars.setData(p.turnover.map(x => ({ time: x.time, value: x.value, color: x.side === "buy" ? up : dn })));
  sr.exp.setData(p.expected);
  sr.net.setData(p.net);
  sr.idx.setData(p.index);
  sr.idx.applyOptions({ title: d.index ? d.index.label || d.index.key : "" });
  const times = p.turnover.map(x => x.time);
  sr.marks.setMarkers((d.alerts || []).map(a => ({ a, t: alertTime(a.ts, times) })).filter(x => x.t !== null).sort((x, y) => x.t - y.t)
    .map(x => ({ time: x.t, position: "aboveBar", shape: x.a.scope === "MARKET" ? "arrowDown" : "circle", color: cssv("--accent"), text: x.a.scope === "MARKET" ? "watch" : "", size: 1 })));
  S.rows = new Map((d.minutes || []).map(r => [r[0] + IST, r]));
  S.idx = new Map(p.index.map(x => [x.time, x.value]));
  S.chart.applyOptions(chartOpts());
  S.chart.timeScale().fitContent();
  const out = $("vol-readout"); if (out) out.textContent = lastReadout();
}
const dayBase = 946684800;   // 2000-01-01 as the fake date of the shape chart: only the clock reads
const clockSeconds = (hhmm) => { const m = /^(\d{1,2}):(\d{2})/.exec(String(hhmm || "")); return m ? Number(m[1]) * 3600 + Number(m[2]) * 60 : null; };
const todayPoints = () => ((S.data && S.data.minutes) || []).filter(r => num(r[1]) !== null).map(r => ({ time: dayBase + ((r[0] + IST) % 86400), value: Number(r[1]) }));
function drawProfile() {
  const el = $("vol-shape");
  const p = S.profile;
  if (!el) return;
  if (!p || !(p.minutes || []).length) { el.innerHTML = `<div class="muted vol-pad">No normal-day curve yet.</div>`; S.prof = null; return; }
  const LW = window.LightweightCharts;
  if (!LW) { el.innerHTML = `<div class="muted vol-pad">The chart library is missing (static/vendor).</div>`; return; }
  if (S.prof) { try { S.prof.remove(); } catch (e) {} S.prof = null; el.innerHTML = ""; }
  const chart = S.prof = LW.createChart(el, { ...chartOpts(), leftPriceScale: { visible: false }, autoSize: true, timeScale: { ...chartOpts().timeScale, fixLeftEdge: true, fixRightEdge: true, rightOffset: 0 } });
  const fmtV = { type: "custom", formatter: (v) => crore(v), minMove: 1 };
  const pts = (i) => p.minutes.filter(m => clockSeconds(m[0]) !== null && num(m[i]) !== null).map(m => ({ time: dayBase + clockSeconds(m[0]), value: Number(m[i]) }));
  const line = (data, color, width, style, title) => { const s = chart.addSeries(LW.LineSeries, { color, lineWidth: width, lineStyle: style, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, title, priceFormat: fmtV }); s.setData(data); return s; };
  line(pts(3), cssv("--dim"), 1, 2, "75th");
  line(pts(2), cssv("--dim"), 1, 2, "25th");
  line(pts(1), cssv("--cyan"), 2, 0, "normal day");
  S.profToday = line(todayPoints(), cssv("--accent"), 2, 0, S.date ? "this day" : "today");
  chart.timeScale().fitContent();
}
function retheme() {
  if (S.chart) drawMain();
  if (S.prof) drawProfile();
}

/* ── render ─────────────────────────────────────────────────────────── */
const set = (id, html) => { const el = $(id); if (el) el.innerHTML = html; };
function renderShell() {
  const root = $("volume-root");
  root.innerHTML =
    `<div id="vol-demo"></div>`
    + `<div class="card vol-bar"><div class="vol-bar-row"><div class="vol-status" id="vol-status" aria-live="polite">Loading the volume watch…</div>`
    + `<div class="vol-pick"><label for="vol-date">Session</label><input id="vol-date" type="date" aria-label="Session date, empty for today"></div>`
    + `<div class="vol-pick"><label for="vol-index">Index price line</label><select id="vol-index"></select></div>`
    + `<div class="vol-pick vol-go"><button class="btn small" id="vol-today" type="button">Today</button></div></div>`
    + `<div class="muted vol-err" id="vol-err" role="status"></div></div>`
    + `<div id="vol-banner"></div>`
    + `<div class="card" id="vol-headline"></div><div class="tiles vol-tiles" id="vol-tiles"></div>`
    + `<div class="card vol-chart-card"><h2>Turnover by minute</h2><div class="muted vol-sub">Each bar is one minute of the large stocks' turnover: green when buying led, red when selling led. The orange line is the normal for that minute. Below: the net flow so far today (buying minus selling) and the index price. An arrow marks a market-wide watch alert, a dot a single-stock one.</div>`
    + `<div class="vol-readout" id="vol-readout"></div><div id="vol-chart" class="vol-chart"></div></div>`
    + `<div class="card"><h2>Where the volume is</h2><div class="muted vol-sub">Click a heading to sort.</div><div id="vol-stocks"></div></div>`
    + `<h2 class="vol-h">Pressure on the indices</h2><div id="vol-indices"></div>`
    + `<div class="card"><h2>Order book</h2><div id="vol-book"></div></div>`
    + `<div class="card"><h2>Watch alerts</h2><div class="muted vol-sub">${esc(WATCH_NOT_ALERT)}</div><div id="vol-alerts"></div>`
    + `<div class="actions"><button class="btn small" id="vol-earlier-btn" type="button">Earlier alerts</button></div><div id="vol-earlier"></div></div>`
    + `<div class="card"><h2>The day's shape</h2><div id="vol-profile"></div><div id="vol-shape" class="vol-chart small"></div></div>`
    + `<div class="card"><h2>What history says</h2><div id="vol-study"></div></div>`
    + `<div class="card"><h2>How to read this page</h2><div id="vol-legend">${legendHtml()}</div></div>`
    + `<div id="vol-notes"></div>`;
  S.ready = true;
}
function renderBar() {
  const sel = $("vol-index");
  if (sel) sel.innerHTML = indexOptions(S.data, S.index);
  const dt = $("vol-date"); if (dt) { dt.max = today(); dt.value = S.date; }
  set("vol-demo", S.fake ? demoHtml() : "");
  const er = $("vol-err"); if (er) er.textContent = S.err;
}
function render() {
  const d = S.data;
  renderBar();
  if (!d) return;
  const h = sectionsHtml(d, S.sort, S.date);
  set("vol-banner", h.banner); $("vol-status").innerHTML = h.status; set("vol-headline", h.headline); set("vol-tiles", h.tiles);
  set("vol-stocks", h.stocks); set("vol-indices", h.indices); set("vol-book", h.book); set("vol-alerts", h.alerts); set("vol-notes", h.notes);
  set("vol-legend", legendHtml(d.settings && d.settings.rule));
  drawMain();
  if (S.prof && S.profToday) { S.profToday.setData(todayPoints()); S.profToday.applyOptions({ title: S.date ? "this day" : "today" }); } else if (S.profile) drawProfile();
}
function renderExtras() {
  set("vol-profile", profileHtml(S.profile)); set("vol-study", studyHtml(S.study)); set("vol-earlier", earlierHtml(S.earlier));
  drawProfile();
}

/* ── loading ────────────────────────────────────────────────────────── */
async function load() {
  if (S.busy) return;
  S.busy = true;
  try {
    const d = await getJson(volumeUrl());
    if (!d || typeof d !== "object") throw new Error("an empty answer");
    S.data = d; S.err = "";
    render();
    clearTimeout(S.reask);
    const wait = reaskAfter(d);
    if (wait) S.reask = setTimeout(() => { if (tabShown("volume") && !document.hidden) load(); }, wait);
  } catch (e) {
    S.err = errorText(e, !!S.data);
    if (!S.data) set("vol-headline", `<div class="neg">${esc(S.err)}</div>`);
    const er = $("vol-err"); if (er) er.textContent = S.data ? S.err : "";
  } finally { S.busy = false; }
}
async function loadExtras(force) {
  if (!force && S.profile && Date.now() - S.profAt < 600000) return;
  const [p, s] = await Promise.all([getJson("/api/volume/profile").catch(() => null), getJson("/api/volume/study").catch(() => null)]);
  if (p) { S.profile = p; S.profAt = Date.now(); }
  if (s) S.study = s;
  renderExtras();
}
async function loadEarlier() {
  const b = $("vol-earlier-btn"); if (b) b.disabled = true;
  try { S.earlier = await getJson("/api/volume/alerts?limit=100"); } catch (e) { S.earlier = { alerts: [] }; set("vol-earlier", `<div class="neg">${esc(errorText(e, false))}</div>`); if (b) b.disabled = false; return; }
  set("vol-earlier", earlierHtml(S.earlier)); if (b) b.disabled = false;
}
async function loadTab() {
  if (!S.ready) renderShell();
  if (S.chart) S.chart.timeScale().fitContent();
  await load();
  loadExtras(false).catch(() => {});
}
function wire() {
  const root = $("volume-root");
  root.addEventListener("change", (e) => {
    if (e.target.id === "vol-date") { S.date = e.target.value || ""; load(); }
    else if (e.target.id === "vol-index") { S.index = e.target.value; store.set("volume.index", S.index); load(); }
  });
  const sortBy = (th) => {
    const k = th.dataset.sort;
    S.sort = { key: k, dir: S.sort.key === k ? (S.sort.dir === "desc" ? "asc" : "desc") : (k === "label" || k === "state" ? "asc" : "desc") };
    if (S.data) set("vol-stocks", stocksTableHtml(S.data.stocks, S.sort) + missingHtml(S.data.basket));
  };
  root.addEventListener("click", (e) => {
    const th = e.target.closest && e.target.closest("th[data-sort]");
    if (th) { sortBy(th); return; }
    const b = e.target.closest && e.target.closest("button");
    if (!b) return;
    if (b.id === "vol-today") { S.date = ""; load(); }
    else if (b.id === "vol-earlier-btn") loadEarlier();
  });
  root.addEventListener("keydown", (e) => {
    const th = e.target.closest && e.target.closest("th[data-sort]");
    if (th && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); sortBy(th); const again = root.querySelector(`th[data-sort="${th.dataset.sort}"]`); if (again) again.focus(); }
  });
  setInterval(() => { if (tabShown("volume") && !document.hidden && !S.date && S.ready) load(); }, POLL_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && tabShown("volume") && !S.date && S.ready) load(); });
  let rt = null;
  window.addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { if (S.chart) S.chart.timeScale().fitContent(); if (S.prof) S.prof.timeScale().fitContent(); }, 150); });
  new MutationObserver(retheme).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
}
function boot() {
  if (!$("volume-root")) return;
  renderShell();
  wire();
  OL.tabLoaders.volume = () => { loadTab(); };
  if (tabShown("volume")) loadTab();
}
if (/[?&]volfake=1(&|$)/.test(location.search)) {
  const s = document.createElement("script");
  s.src = "/static/volume_fake.js";
  s.onload = () => { S.fake = window.VolumeFake; boot(); };
  s.onerror = () => { boot(); };
  document.head.appendChild(s);
} else boot();
})();
