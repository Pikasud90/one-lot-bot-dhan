/* charts.js — thin wrapper over TradingView Lightweight Charts v5 (vendored; works offline).
   Every chart reads its colours from the CSS variables, so the Day/Night switch re-themes
   them in place. Intraday timestamps are shifted so the axis reads IST. */
(() => {
"use strict";
const IST_OFFSET = 19800;
const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const LW = () => window.LightweightCharts;
const live = new Map();   // el -> { chart, series: [], markers: [], priceLines: [] }

const themeOpts = () => ({
  layout: { background: { type: "solid", color: "transparent" }, textColor: cssVar("--plot-font"),
            fontFamily: "JetBrains Mono, Menlo, monospace", fontSize: 12, attributionLogo: false },
  grid: { vertLines: { color: cssVar("--grid") }, horzLines: { color: cssVar("--grid") } },
  crosshair: { mode: 0 },
  rightPriceScale: { borderColor: cssVar("--line2") },
  timeScale: { borderColor: cssVar("--line2"), timeVisible: true, secondsVisible: false, rightOffset: 4 },
  localization: { locale: "en-IN" },
});
const ready = () => typeof window.LightweightCharts !== "undefined";
const toT = (unix) => unix + IST_OFFSET;
const day = (s) => s;   // 'YYYY-MM-DD' strings are business days

function dispose(el) {
  const r = live.get(el);
  if (r) { try { r.chart.remove(); } catch (e) {} live.delete(el); }
  el.innerHTML = "";
}
function make(el, extra) {
  dispose(el);
  if (!ready()) { el.innerHTML = `<div class="muted" style="padding:14px">Chart library missing (static/vendor).</div>`; return null; }
  const chart = LW().createChart(el, { ...themeOpts(), autoSize: true, ...(extra || {}) });
  const rec = { chart, series: [], markers: [], priceLines: [] };
  live.set(el, rec);
  return rec;
}
function retheme() { for (const r of live.values()) r.chart.applyOptions(themeOpts()); }

/* candles + trade markers; bars = [[unix, o, h, l, c]], trades = [{entry_ts, exit_ts, entry, exit, side, net_pnl, r, reason}] */
function candles(el, bars, trades, opts = {}) {
  const r = make(el); if (!r) return null;
  const up = cssVar("--ok"), dn = cssVar("--bad");
  const s = r.chart.addSeries(LW().CandlestickSeries, { upColor: up, downColor: dn, borderVisible: false, wickUpColor: up, wickDownColor: dn,
    priceLineVisible: false, lastValueVisible: true });
  s.setData(bars.map(b => ({ time: toT(b[0]), open: b[1], high: b[2], low: b[3], close: b[4] })));
  r.series.push(s);
  const mk = [];
  (trades || []).forEach(t => {
    const long = t.side === "LONG";
    mk.push({ time: toT(t.entry_ts), position: long ? "belowBar" : "aboveBar", shape: long ? "arrowUp" : "arrowDown",
              color: long ? cssVar("--cyan") : cssVar("--accent2"), text: (long ? "B " : "S ") + (t.entry != null ? Number(t.entry).toFixed(1) : ""), size: 1 });
    mk.push({ time: toT(t.exit_ts), position: long ? "aboveBar" : "belowBar", shape: "circle",
              color: t.net_pnl > 0 ? up : dn, text: (t.r != null ? Number(t.r).toFixed(1) + "R" : ""), size: 0.8 });
  });
  mk.sort((a, b) => a.time - b.time);
  r.markers = LW().createSeriesMarkers(s, mk);
  r.chart.timeScale().fitContent();
  if (opts.fit === "last" && bars.length > 300) r.chart.timeScale().setVisibleLogicalRange({ from: bars.length - 300, to: bars.length + 4 });
  return r;
}
/* highlight one trade: zoom to it and draw its entry/stop-ish/exit lines */
function focusTrade(el, t, pad = 40) {
  const r = live.get(el); if (!r || !r.series.length) return;
  const s = r.series[0];
  r.priceLines.forEach(pl => { try { s.removePriceLine(pl); } catch (e) {} });
  r.priceLines = [];
  const mk = (price, color, title) => r.priceLines.push(s.createPriceLine({ price, color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title }));
  if (t.entry != null) mk(Number(t.entry), cssVar("--cyan"), "entry");
  if (t.exit != null) mk(Number(t.exit), t.net_pnl > 0 ? cssVar("--ok") : cssVar("--bad"), "exit");
  const data = s.data();
  if (!data.length) return;
  const from = toT(t.entry_ts), to = toT(t.exit_ts);
  let i0 = data.findIndex(d => d.time >= from), i1 = data.findIndex(d => d.time >= to);
  if (i0 < 0) i0 = 0; if (i1 < 0) i1 = data.length - 1;
  r.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, i0 - pad), to: Math.min(data.length - 1, i1 + pad) });
}
/* equity (area) with drawdown in a second pane; eq = [[date, equity]] */
function equity(el, eq, cap) {
  const r = make(el); if (!r) return null;
  const pts = eq.map(e => ({ time: day(e[0]), value: Number(e[1]) - cap }));
  let peak = -Infinity;
  const dd = pts.map(p => { peak = Math.max(peak, p.value); return { time: p.time, value: p.value - peak }; });
  const a = r.chart.addSeries(LW().BaselineSeries, { baseValue: { type: "price", price: 0 }, topLineColor: cssVar("--ok"), topFillColor1: "rgba(20,131,71,.35)", topFillColor2: "rgba(20,131,71,.02)",
    bottomLineColor: cssVar("--bad"), bottomFillColor1: "rgba(212,31,62,.02)", bottomFillColor2: "rgba(212,31,62,.35)", lineWidth: 2, priceLineVisible: false, title: "net P&L ₹" });
  a.setData(pts);
  const d = r.chart.addSeries(LW().AreaSeries, { lineColor: cssVar("--bad"), topColor: "rgba(212,31,62,.05)", bottomColor: "rgba(212,31,62,.35)", lineWidth: 1, priceLineVisible: false, title: "drawdown ₹" }, 1);
  d.setData(dd);
  try { const panes = r.chart.panes(); if (panes[1]) panes[1].setHeight(110); } catch (e) {}
  r.series.push(a, d);
  r.chart.applyOptions({ timeScale: { timeVisible: false } });
  r.chart.timeScale().fitContent();
  return r;
}
/* histogram with sign colouring; points = [{time, value}] (time = 'YYYY-MM-DD' or label index) */
function histogram(el, points, opts = {}) {
  const r = make(el); if (!r) return null;
  const h = r.chart.addSeries(LW().HistogramSeries, { priceLineVisible: false, lastValueVisible: false, priceFormat: opts.priceFormat || { type: "price", precision: 0, minMove: 1 } });
  h.setData(points.map(p => ({ time: p.time, value: p.value, color: p.color || (p.value >= 0 ? cssVar("--ok") : cssVar("--bad")) })));
  r.series.push(h);
  r.chart.applyOptions({ timeScale: { timeVisible: !!opts.timeVisible } });
  r.chart.timeScale().fitContent();
  return r;
}
/* one or more lines; series = [{name, points:[{time,value}], color, width, scale?: 'left'|'right'}] */
function lines(el, series, opts = {}) {
  const r = make(el, opts.left ? { leftPriceScale: { visible: true, borderColor: cssVar("--line2") } } : {}); if (!r) return null;
  series.forEach(sr => {
    const ls = r.chart.addSeries(LW().LineSeries, { color: sr.color || cssVar("--cyan"), lineWidth: sr.width || 2, title: sr.name || "",
      priceLineVisible: false, priceScaleId: sr.scale === "left" ? "left" : "right", lastValueVisible: !!opts.lastValue });
    ls.setData(sr.points);
    r.series.push(ls);
  });
  r.chart.applyOptions({ timeScale: { timeVisible: !!opts.timeVisible } });
  r.chart.timeScale().fitContent();
  return r;
}
/* a category bar chart (labels on the x axis) drawn as a histogram over synthetic dates */
function bars(el, labels, values, opts = {}) {
  const base = new Date(Date.UTC(2000, 0, 1));
  const pts = values.map((v, i) => ({ time: new Date(base.getTime() + i * 86400000).toISOString().slice(0, 10), value: v, color: opts.color ? opts.color(v, i) : undefined }));
  const r = histogram(el, pts, opts);
  if (!r) return null;
  r.chart.applyOptions({ timeScale: { tickMarkFormatter: (t) => { const i = Math.round((new Date(t.year, t.month - 1, t.day) - new Date(2000, 0, 1)) / 86400000); return labels[i] || ""; }, timeVisible: false } });
  r.chart.timeScale().fitContent();
  return r;
}
window.Charts = { ready, candles, focusTrade, equity, histogram, lines, bars, dispose, retheme, cssVar, toT };
})();
