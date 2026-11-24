/* A fake of the Volume Watch engine (US-089), for looking at the tab without it and for the node tests.
   Loaded only with ?volfake=1. It serves the contract of .agile/stories/VOL-API.md from generated minutes.
   Switches in the page's address: &volbook=0 (no order book), &volclosed=1 (market closed), &volerr=1 (every
   volume call fails) or &volerr=2 (the first call works, the later ones fail). Demo data, not market data. */
(function (root) {
"use strict";
const IST = 19800, DAY_MIN = 375;
const LIVE_DAY = "2026-11-25", LAST_DAY = "2026-11-24", FIRST_DAY = "2026-09-01";
const START_MINUTE = 148;   // the live demo starts at 11:43 and moves one minute with each call
const STOCKS = [
  ["HDFCBANK", "HDFC Bank", 1712.5, 0.12], ["ICICIBANK", "ICICI Bank", 1288.3, 0.1], ["RELIANCE", "Reliance Industries", 1424.1, 0.1], ["INFY", "Infosys", 1612.9, 0.07],
  ["TCS", "TCS", 3180.4, 0.06], ["SBIN", "State Bank of India", 842.6, 0.07], ["BHARTIARTL", "Bharti Airtel", 1930.2, 0.05], ["ITC", "ITC", 412.7, 0.05],
  ["LT", "Larsen and Toubro", 3702.8, 0.04], ["AXISBANK", "Axis Bank", 1190.5, 0.05], ["KOTAKBANK", "Kotak Mahindra Bank", 2104.3, 0.04], ["HINDUNILVR", "Hindustan Unilever", 2380.0, 0.03],
  ["BAJFINANCE", "Bajaj Finance", 1022.4, 0.04], ["MARUTI", "Maruti Suzuki", 12240.0, 0.03], ["SUNPHARMA", "Sun Pharma", 1768.9, 0.03], ["M&M", "Mahindra and Mahindra", 3320.7, 0.03], ["TATASTEEL", "Tata Steel", 172.4, 0.03],
];
const INDICES = [
  { key: "NIFTY", label: "NIFTY 50", trade: true, base: 25016.2 }, { key: "BANKNIFTY", label: "NIFTY BANK", trade: true, base: 56420.5 },
  { key: "FINNIFTY", label: "NIFTY FIN SERVICE", trade: true, base: 26110.8 }, { key: "MIDCPNIFTY", label: "NIFTY MIDCAP SELECT", trade: false, base: 13244.6 },
];
let calls = 0;
const fail = (status, detail) => { const e = new Error(detail); e.status = status; return e; };
const r2 = (v) => Math.round(v * 100) / 100;
const dayOpen = (d) => Date.UTC(+d.slice(0, 4), +d.slice(5, 7) - 1, +d.slice(8, 10), 9, 15) / 1000 - IST;
const iso = (unix) => new Date((unix + IST) * 1000).toISOString().slice(0, 19) + "+05:30";
const hm = (unix) => iso(unix).slice(11, 16);
function rng(seed) { let a = seed >>> 0; return () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }
const seedOf = (d) => Number(d.replace(/-/g, "")) % 100000;
const query = (url) => { const q = {}, i = url.indexOf("?"); if (i >= 0) url.slice(i + 1).split("&").forEach(p => { const [k, v] = p.split("="); if (k) q[k] = decodeURIComponent(v || ""); }); return q; };
const flags = () => query((typeof location !== "undefined" && location.search) ? location.search : "");
/* the normal turnover of a minute of the day (rupees, whole basket): high at the open, low at lunch, up again at the close */
const expectedAt = (i) => 1.9e8 * (1 + 2.4 * Math.exp(-i / 14) + 1.5 * Math.exp(-(DAY_MIN - 1 - i) / 28));
/* A session: minutes up to `n` (exclusive), each [t, turnover, expected, buying, selling, net so far, rvol_5m, z]. A spike sits in minutes 142 to 147. */
function session(day, n, spike) {
  const rnd = rng(seedOf(day)), t0 = dayOpen(day), rows = [], turn = [], exp = [], idx = [];
  let net = 0, px = 25000 * (1 + (rnd() - 0.5) * 0.004);
  for (let i = 0; i < n; i++) {
    const hot = spike && i >= 142 && i <= 147, mul = Math.exp((rnd() - 0.5) * 0.5) * (hot ? 2.6 + (i - 142) * 0.15 : 1);
    const t = Math.round(expectedAt(i) * mul), e = Math.round(expectedAt(i));
    const u = hot ? 0.3 + (rnd() - 0.5) * 0.1 : Math.min(0.95, Math.max(0.05, 0.5 + (rnd() - 0.5) * 0.3));
    const buy = Math.round(t * u), sell = t - buy;
    net += buy - sell; turn.push(t); exp.push(e);
    const a = Math.max(0, i - 4), s5 = turn.slice(a, i + 1).reduce((x, y) => x + y, 0), e5 = exp.slice(a, i + 1).reduce((x, y) => x + y, 0);
    const rv5 = r2(s5 / e5), z = r2(Math.log(rv5) / 0.3);
    rows.push([t0 + i * 60, t, e, buy, sell, net, rv5, z]);
    px += (hot ? -1 : 0.1) * 6 + (rnd() - 0.5) * 8; idx.push(r2(px));
  }
  return { rows, idx, t0 };
}
const stateOf = (rv, z) => z >= 3 ? "spike" : (rv >= 1.5 ? "busy" : (rv < 0.6 ? "quiet" : "normal"));
const alertsFor = (day, n, rows) => {
  const out = [];
  if (n > 144) {
    out.push({ id: "v_" + day.replace(/-/g, "") + "_1139_MARKET", ts: iso(rows[144][0]), scope: "MARKET", key: "MARKET", rvol: 3.2, z: 3.4, flow_share: 0.31, direction: "SELLING",
      leaders: ["HDFCBANK", "ICICIBANK"], indices: ["BANKNIFTY", "FINNIFTY"], title: "Volume watch: 3.2 times normal, mostly selling",
      text: "Led by HDFC Bank and ICICI Bank. Likely to move: NIFTY BANK, FINNIFTY.", evidence: "After such spikes the next 15 minutes were 1.7 times as large as usual; direction was a coin flip." });
  }
  if (n > 142) out.push({ id: "v_" + day.replace(/-/g, "") + "_1137_HDFCBANK", ts: iso(rows[142][0]), scope: "STOCK", key: "HDFCBANK", rvol: 4.1, z: 4.4, flow_share: 0.27, direction: "SELLING",
    leaders: ["HDFCBANK"], indices: ["BANKNIFTY"], title: "Volume watch: HDFC Bank at 4.1 times normal, mostly selling", text: "HDFC Bank's turnover jumped. Likely to move: NIFTY BANK.",
    evidence: "After a single-stock spike the stock's next 15 minutes were 1.9 times as large as usual; direction was a coin flip." });
  return out;
};
function stockRows(rnd, rows, hot) {
  const last = rows[rows.length - 1], dayTurn = rows.reduce((s, r) => s + r[1], 0);
  return STOCKS.map(([key, label, px, w], k) => {
    const hdfc = key === "HDFCBANK" || key === "ICICIBANK", rv5 = r2((hdfc && hot ? 2.1 + rnd() : 0.8 + rnd() * 0.9)), rvd = r2(0.8 + rnd() * 0.6), z = r2(Math.log(rv5) / 0.3 + (hdfc && hot ? 1 : 0));
    const fs = hdfc && hot ? r2(0.28 + rnd() * 0.06) : r2(0.4 + rnd() * 0.25);
    return { key, label, last: r2(px * (1 + (rnd() - 0.5) * 0.01)), chg_pct: r2((rnd() - 0.55) * 2.2 - (hdfc && hot ? 0.5 : 0)), turnover_day: Math.round(dayTurn * w), share: w,
      rvol_5m: rv5, rvol_day: rvd, z, flow_share_5m: fs, flow_share_day: r2(0.42 + rnd() * 0.16), state: stateOf(rv5, z) };
  }).sort((a, b) => b.turnover_day - a.turnover_day);
}
function build(day, n, opts) {
  const spike = opts.spike, { rows, idx } = session(day, n, spike), rnd = rng(seedOf(day) + 7), last = rows[rows.length - 1];
  const dayTurn = rows.reduce((s, r) => s + r[1], 0), dayExp = rows.reduce((s, r) => s + r[2], 0);
  const buySum = rows.reduce((s, r) => s + r[3], 0), a5 = rows.slice(-5), b5 = a5.reduce((s, r) => s + r[3], 0), t5 = a5.reduce((s, r) => s + r[1], 0);
  const stocks = stockRows(rnd, rows, spike && n > 143), up = stocks.filter(s => s.chg_pct > 0.05).length, down = stocks.filter(s => s.chg_pct < -0.05).length;
  const rv5 = last[6], rv1 = r2(last[1] / last[2]), z = last[7], state = stateOf(rv5, z);
  const text = state === "spike" ? `Volume is ${rv5.toFixed(1)} times normal for this time of day, a spike. Sellers ${Math.round((1 - b5 / t5) * 100)}%.`
    : `Volume is ${rv5.toFixed(1)} times normal for this time of day. Buyers ${Math.round(b5 / t5 * 100)}%.`;
  const ix = INDICES.find(i => i.key === opts.index) || INDICES[0];
  const names = (keys) => keys.map(k => STOCKS.find(s => s[0] === k)[1]);
  const pressure = (p, top) => ({ pressure_5m: p, pressure_day: r2(p / 3.5), top });
  const indices = [
    { key: "BANKNIFTY", label: "NIFTY BANK", trade: true, ...pressure(spike ? -0.42 : 0.12, [["HDFCBANK", 0.31], ["ICICIBANK", 0.27], ["SBIN", 0.1]]), r2: 0.93,
      text: spike ? `Selling pressure, led by ${names(["HDFCBANK", "ICICIBANK"]).join(" and ")}.` : "Mild buying pressure." },
    { key: "NIFTY", label: "NIFTY 50", trade: true, ...pressure(spike ? -0.18 : 0.05, [["HDFCBANK", 0.14], ["RELIANCE", 0.12], ["ICICIBANK", 0.1]]), r2: 0.88, text: spike ? "Selling pressure, mild." : "Balanced." },
    { key: "FINNIFTY", label: "NIFTY FIN SERVICE", trade: true, ...pressure(spike ? -0.35 : 0.08, [["HDFCBANK", 0.28], ["ICICIBANK", 0.24], ["KOTAKBANK", 0.09]]), r2: 0.91, text: spike ? "Selling pressure, led by HDFC Bank and ICICI Bank." : "Balanced." },
    { key: "MIDCPNIFTY", label: "NIFTY MIDCAP SELECT", trade: false, ...pressure(-0.03, [["M&M", 0.2], ["TATASTEEL", 0.12], ["BAJFINANCE", 0.1]]), r2: 0.41, text: "Balanced. The basket explains little of this index." },
  ];
  const lastIdx = idx[idx.length - 1], shift = ix.base - 25000;
  return {
    as_of: iso(last[0] + 60), session: day, market_open: !!opts.open, source: opts.live ? "live" : "stored",
    basket: { count: 18, label: "18 large stocks", missing: ["TMPV"] },
    now: { minute: hm(last[0]), turnover_1m: last[1], expected_1m: last[2], rvol_1m: rv1, rvol_5m: rv5, rvol_day: r2(dayTurn / dayExp), z, state,
      flow_share_5m: r2(b5 / t5), flow_share_day: r2(buySum / dayTurn), turnover_day: dayTurn, expected_day: dayExp, net_flow_day: last[5], up, down, flat: STOCKS.length - up - down, text },
    minutes: rows,
    index: { key: ix.key, label: ix.label, minutes: rows.map((r, i) => [r[0], r2(idx[i] + shift)]) },
    stocks, indices, alerts: alertsFor(day, n, rows), book: opts.book ? bookOf(rnd, last, spike) : null,
    settings: { z_alert: 3.0, stock_z_alert: 4.0, cooldown_min: 15, alerts_on: true, notify: true },
    notes: ["Stock bars end at 15:14 since the closing auction began.", "Demo data: these numbers are made up."],
  };
}
function bookOf(rnd, last, spike) {
  const stocks = STOCKS.slice(0, 6).map(([key, label], i) => {
    const bid = Math.round(150000 + rnd() * 200000), ask = Math.round(bid * (spike && i < 2 ? 1.5 : 0.9 + rnd() * 0.3));
    return { key, label, bid_qty: bid, ask_qty: ask, bid_share: r2(bid / (bid + ask)), spread_bps: r2(0.8 + rnd() * 1.6) };
  });
  return { as_of: iso(last[0] + 90), bid_share: r2(stocks.reduce((s, x) => s + x.bid_share, 0) / stocks.length), stocks, note: "Resting orders in the best five price steps. Recorded since 25 Nov 2026: not yet studied." };
}
function profile() {
  const minutes = [];
  for (let i = 0; i < DAY_MIN; i++) { const t = dayOpen(LAST_DAY) + i * 60, e = expectedAt(i); minutes.push([hm(t), Math.round(e), Math.round(e * 0.78), Math.round(e * 1.27)]); }
  const total = minutes.reduce((s, m) => s + m[1], 0), half = [];
  for (let i = 0; i + 30 <= DAY_MIN; i += 30) { const sh = minutes.slice(i, i + 30).reduce((s, m) => s + m[1], 0) / total; half.push({ from: minutes[i][0], to: minutes[i + 29][0], share: r2(sh) }); }
  const sorted = half.slice().sort((a, b) => b.share - a.share);
  return { as_of: LAST_DAY, sessions: 60, minutes, half_hours: half.map((h, i) => ({ ...h, rank: sorted.indexOf(half[i]) + 1 })),
    busiest: sorted.slice(0, 2).map(({ from, to, share }) => ({ from, to, share })), quietest: sorted.slice(-2).reverse().map(({ from, to, share }) => ({ from, to, share })),
    weekdays: [["Mon", 1.04], ["Tue", 0.97], ["Wed", 0.95], ["Thu", 1.12], ["Fri", 0.92]].map(([day, rvol]) => ({ day, rvol })),
    expiry: { expiry_day_rvol: 1.12, other_days_rvol: 0.98, note: "NIFTY expiry days" },
    text: "The first half hour carries about a fifth of the day's volume; 12:15 to 13:15 is the quietest hour." };
}
const earlier = () => [["2026-11-24", "10:12", "BUYING", "MARKET", 2.4], ["2026-11-24", "14:41", "SELLING", "STOCK", 4.6], ["2026-11-23", "09:48", "SELLING", "MARKET", 3.1], ["2026-11-20", "13:05", "TWO_WAY", "MARKET", 2.9], ["2026-11-19", "11:22", "BUYING", "STOCK", 5.2]]
  .map(([day, time, dir, scope, rv], i) => ({ id: "v_" + day.replace(/-/g, "") + "_" + time.replace(":", "") + "_" + (scope === "MARKET" ? "MARKET" : "SBIN"), session: day, ts: `${day}T${time}:00+05:30`, scope, key: scope === "MARKET" ? "MARKET" : "SBIN", rvol: rv, z: 3.2 + i * 0.2,
    flow_share: dir === "BUYING" ? 0.66 : (dir === "SELLING" ? 0.33 : 0.5), direction: dir, leaders: ["SBIN"], indices: ["BANKNIFTY"],
    title: `Volume watch: ${rv} times normal, ${dir === "BUYING" ? "mostly buying" : (dir === "SELLING" ? "mostly selling" : "two-way trade")}`, text: "Led by State Bank of India. Likely to move: NIFTY BANK.", evidence: "" }));
const STUDY = { as_of: LAST_DAY, note: "docs/research/VOLUME_STUDY_2026-11-24.md",
  facts: [{ id: "size_after_market_spike", text: "After a market-wide spike the index's next 15 minutes were 1.7 times as large as usual (1.6 in the last year).", value: 1.7 },
    { id: "direction_after_market_spike", text: "The direction followed the flow 51% of the time: no edge.", value: 0.51 }], alerts_per_day: { market: 1.8, stock: 4.5 } };

async function handle(url) {
  const path = url.split("?")[0], q = query(url), f = flags();
  if (path === "/api/volume/profile") return profile();
  if (path === "/api/volume/study") return STUDY;
  if (path === "/api/volume/alerts") return { alerts: earlier().slice(0, Math.max(1, Number(q.limit) || 100)) };
  if (path !== "/api/volume") throw fail(404, "404");
  calls += 1;
  if (f.volerr === "1" || (f.volerr === "2" && calls > 1)) throw fail(503, "the demo engine is set to fail (volerr)");
  if (q.date) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(q.date) || q.date < FIRST_DAY || q.date > LIVE_DAY) throw fail(400, `No stored session for ${q.date}: the stored sessions run from ${FIRST_DAY} to ${LAST_DAY}.`);
    return build(q.date, DAY_MIN, { spike: true, open: false, live: false, book: false, index: q.index });
  }
  if (f.volclosed === "1") return build(LAST_DAY, DAY_MIN, { spike: true, open: false, live: false, book: false, index: q.index });
  return build(LIVE_DAY, Math.min(DAY_MIN, START_MINUTE + calls - 1), { spike: true, open: true, live: true, book: f.volbook !== "0", index: q.index });
}
const api = { handle, build, profile, STOCKS, INDICES, session };
if (typeof module !== "undefined" && module.exports) module.exports = api; else root.VolumeFake = api;
})(typeof window !== "undefined" ? window : globalThis);
