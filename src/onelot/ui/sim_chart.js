/* The Simulator's chart (US-094): one TradingView Lightweight Charts v5 chart and its series, built once for the life of
   the page. A switch swaps the data; reset() is the ONE place that clears everything a session drew (candles, lines,
   levels in the price scale, zones, markers, the strategy's lines), so nothing of a session survives the next one
   (SIM-API-2 rule 7, the owner's first bug). Colours come only from the theme variables. Used by sim.js (window.SimChart)
   and by the node tests with a stub of the library (require). */
(function (root) {
"use strict";
const C = root && root.SimCore ? root.SimCore : require("./sim_core.js");
const OVERLAY_COLOURS = ["--violet", "--accent2", "--cyan", "--dim"];
const LEVELS = ["entry", "stop", "target", "stop0", "target0"];
const SHOW = 120;      // candles shown at the start
const RIGHT = 6;       // empty bars right of the newest

/* The shaded reward (entry to target) and risk (entry to stop) zones, from the entry's candle to the right edge. */
function zonesPrimitive(cssv) {
  let chart = null, series = null, requestUpdate = null, z = null;
  const renderer = {
    draw(target) {
      if (!z || !chart || !series || !Number.isFinite(z.t0)) return;
      const ts = chart.timeScale();
      let x0 = null;
      try { x0 = ts.timeToCoordinate(z.t0); } catch (e) { return; }
      if (x0 === null) x0 = 0;
      const yE = series.priceToCoordinate(z.entry), yS = series.priceToCoordinate(z.stop), yT = series.priceToCoordinate(z.target);
      if (yE === null) return;
      target.useBitmapCoordinateSpace((s) => {
        const ctx = s.context, hr = s.horizontalPixelRatio, vr = s.verticalPixelRatio, x = Math.round(Math.max(0, x0) * hr), w = s.bitmapSize.width - x;
        if (w <= 0) return;
        const rect = (ya, yb, colour) => {
          if (ya === null || yb === null) return;
          const top = Math.round(Math.min(ya, yb) * vr), h = Math.round(Math.abs(yb - ya) * vr);
          ctx.fillStyle = colour; ctx.fillRect(x, top, w, Math.max(1, h));
        };
        // the stop on the losing side: risk (entry to stop) and reward (entry to target); once it has passed the entry the
        // band from the entry to the stop is profit locked in, drawn as reward twice over
        const losing = (z.target - z.entry) * (z.stop - z.entry) < 0;
        if (losing) { rect(yE, yT, cssv("--ok-bg")); rect(yE, yS, cssv("--bad-bg")); }
        else { rect(yE, yT, cssv("--ok-bg")); rect(yE, yS, cssv("--ok-bg")); }
      });
    },
  };
  const view = { renderer: () => renderer, zOrder: () => "bottom" };
  return {
    attached(p) { chart = p.chart; series = p.series; requestUpdate = p.requestUpdate; },
    detached() { chart = null; series = null; requestUpdate = null; },
    updateAllViews() {},
    paneViews() { return z ? [view] : []; },
    set(v) { z = v; if (requestUpdate) requestUpdate(); },
    get() { return z; },
  };
}

function create(el, LW, opts = {}) {
  const cssv = opts.cssv || ((n) => n);
  const pinBox = opts.pinBox || null;
  const state = { bars: [], tfm: 3, tf: "3m", meta: null, minute: null, levels: null, levelKey: null, pinned: [], pinKey: "",
    markers: [], overlays: [], overlayLast: [], overlayOn: false, hideDates: false, zones: null, view: null };
  const fmtTime = (t) => state.hideDates ? C.hhmmOf(t) : `${C.dateText(t)} ${C.hhmmOf(t)}`;
  const tickMark = (t, type) => {
    if (type === 3 || type === 4) return C.hhmmOf(t);
    if (state.hideDates) return "·";
    if (type === 2) return C.dateText(t).slice(4);          // "03 Nov": a day mark reads as a day
    return C.dateText(t, true).slice(7);                   // "Nov 2026"
  };
  const chartOptions = () => ({
    layout: { background: { type: "solid", color: cssv("--panel") }, textColor: cssv("--plot-font"), fontFamily: "JetBrains Mono, Menlo, monospace", fontSize: 14, attributionLogo: false,
      panes: { separatorColor: cssv("--line"), separatorHoverColor: cssv("--line2") } },
    grid: { vertLines: { color: cssv("--grid") }, horzLines: { color: cssv("--grid") } },
    crosshair: { mode: 0, vertLine: { color: cssv("--dim"), labelBackgroundColor: cssv("--text") }, horzLine: { color: cssv("--dim"), labelBackgroundColor: cssv("--text") } },
    rightPriceScale: { borderColor: cssv("--line2"), scaleMargins: { top: 0.1, bottom: 0.1 } },
    timeScale: { borderColor: cssv("--line2"), timeVisible: true, secondsVisible: false, rightOffset: RIGHT, barSpacing: 8, minBarSpacing: 1.5,
      lockVisibleTimeRangeOnResize: true, shiftVisibleRangeOnNewBar: true, tickMarkFormatter: tickMark },
    localization: { locale: "en-IN", timeFormatter: fmtTime, priceFormatter: (p) => C.num(p) },
  });
  const chart = LW.createChart(el, { ...chartOptions(), autoSize: true });
  const candleColours = () => { const up = cssv("--ok"), dn = cssv("--bad"); return { upColor: up, downColor: dn, wickUpColor: up, wickDownColor: dn, borderVisible: false }; };
  const candles = chart.addSeries(LW.CandlestickSeries, {
    ...candleColours(), priceLineVisible: true, priceLineStyle: 2, lastValueVisible: true,
    priceFormat: { type: "price", precision: 2, minMove: 0.05 },
    autoscaleInfoProvider: (orig) => autoscale(orig()),
  });
  const volume = chart.addSeries(LW.HistogramSeries, { priceScaleId: "vol", priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false, visible: false });
  try { chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 }, visible: false }); } catch (e) {}
  const overlays = OVERLAY_COLOURS.map((c) => chart.addSeries(LW.LineSeries, { color: cssv(c), lineWidth: 2, priceLineVisible: false, lastValueVisible: false,
    crosshairMarkerVisible: false, visible: false, autoscaleInfoProvider: () => null }));
  const lines = {};
  const lineStyle = (name, kind) => {
    const faint = name === "stop0" || name === "target0";
    const colour = cssv(name.startsWith("stop") ? "--bad" : (name.startsWith("target") ? "--ok" : "--cyan"));
    return { color: colour, lineWidth: faint ? 1 : 2, lineStyle: faint ? 2 : (kind === "alert" ? 1 : 0), axisLabelVisible: !faint, axisLabelColor: colour, axisLabelTextColor: cssv("--panel") };
  };
  LEVELS.forEach((name) => { lines[name] = candles.createPriceLine({ price: 0, ...lineStyle(name, "trade"), lineVisible: false, axisLabelVisible: false, title: "" }); });
  const zones = zonesPrimitive(cssv);
  candles.attachPrimitive(zones);
  const marks = LW.createSeriesMarkers(candles, []);
  const pinEls = [];

  /* THE SCALE RULE: the visible candles' range, widened only by levels within 60% of their height; the others pinned. */
  function autoscale(o) {
    if (!o || !o.priceRange || !state.levels) { setPinned([]); return o; }
    const r = C.scaleRule(o.priceRange, state.levels);
    setPinned(r.pinned);
    return { ...o, priceRange: r.range };
  }
  function setPinned(list) {
    const key = list.map(p => p.name + p.price + p.edge).join("|");
    if (key === state.pinKey) return;
    state.pinKey = key; state.pinned = list;
    if (!pinBox) return;
    const draw = () => {
      while (pinEls.length < state.pinned.length) { const d = document.createElement("div"); d.className = "sim-pin"; pinBox.appendChild(d); pinEls.push(d); }
      pinEls.forEach((d, i) => {
        const p = state.pinned[i];
        d.hidden = !p;
        if (!p) { d.textContent = ""; d.className = "sim-pin"; return; }   // nothing of an old session stays, even hidden (R12)
        d.textContent = C.pinnedText(p);
        d.className = `sim-pin ${p.edge} ${p.name.startsWith("stop") ? "stop" : (p.name.startsWith("target") ? "target" : "entry")}`;
        d.style.setProperty("--slot", String(state.pinned.slice(0, i).filter(x => x.edge === p.edge).length));
      });
      if (ctl) ctl.margins();
    };
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(draw); else draw();
  }
  const barOf = (c) => ({ time: c[0], open: c[1], high: c[2], low: c[3], close: c[4] });
  const volOf = (c) => ({ time: c[0], value: c[5] || 0, color: cssv(c[4] >= c[1] ? "--vol-up" : "--vol-dn") });
  const hasVolume = () => !!(state.meta && state.meta.hasVolume);

  const ctl = {
    chart, candles, volume, lines, zones, marks,
    get bars() { return state.bars; },
    get levels() { return state.levels; },
    get pinned() { return state.pinned; },
    get tfm() { return state.tfm; },
    /* THE reset: nothing of the previous session survives. */
    reset() {
      state.bars = []; state.minute = null; state.levels = null; state.levelKey = null; state.markers = []; state.zones = null;
      candles.setData([]); volume.setData([]);
      overlays.forEach((s) => { s.setData([]); s.applyOptions({ visible: false }); });
      state.overlays = []; state.overlayLast = []; state.overlayOn = false;
      LEVELS.forEach((n) => lines[n].applyOptions({ lineVisible: false, axisLabelVisible: false, title: "" }));
      zones.set(null);
      marks.setMarkers([]);
      setPinned([]);
      try { chart.priceScale("right").applyOptions({ autoScale: true }); } catch (e) {}
    },
    /* A new picture: reset, the instrument's price format and volume, the candles, the start view (about 120 candles). */
    load(meta, tf, tfm, list, forming) {
      ctl.reset();
      state.meta = meta;
      const tick = C.isNum(meta.tick) && meta.tick > 0 ? Number(meta.tick) : 0.05;
      candles.applyOptions({ priceFormat: { type: "price", precision: 2, minMove: tick } });
      const vol = hasVolume();
      volume.applyOptions({ visible: vol });
      ctl.margins();
      ctl.setCandles(tf, tfm, list, forming);
      ctl.startView();
    },
    /* The candles of a (new) chart timeframe; the session's lines and zones stay. */
    setCandles(tf, tfm, list, forming) {
      state.tf = tf; state.tfm = tfm; state.minute = null;
      state.bars = (list || []).slice();
      if (forming) state.bars.push(forming);
      candles.setData(state.bars.map(barOf));
      volume.setData(hasVolume() ? state.bars.map(volOf) : []);
      ctl.redrawMarkers();
      if (state.zones) ctl.setZones(state.zones);
    },
    startView() {
      const n = state.bars.length;
      try { chart.priceScale("right").applyOptions({ autoScale: true }); } catch (e) {}
      if (!n) return;
      // Anchored to the NEWEST candle (bar spacing + right offset), never to absolute indices: the library applies a
      // logical range only at its next frame, so minutes drawn before then (a Next alert, a resume) left the view behind
      // on the old candles with the newest off screen (found on real bars, 25 Nov). An anchored view follows every new bar.
      const ts = chart.timeScale(), w = typeof ts.width === "function" ? ts.width() : 0;
      if (w > 0) ts.applyOptions({ barSpacing: Math.max(1.5, w / (SHOW + RIGHT)) });
      ts.scrollToPosition(RIGHT, false);
    },
    fit() { ctl.startView(); },
    /* The price scale's margins. The top one keeps the newest candles clear of the chart's toolbar (about 44 px) on a
       short chart (1024x768: 299 px), where 10% was 30 px and the highs ran under it (found on real bars, 25 Nov). */
    margins() {
      // On a phone the legend spans the chart and the pinned tags sit under it: the band holds them too.
      const bottomOf = (b) => (b && b.offsetHeight ? b.offsetTop + b.offsetHeight : 0), narrow = !!(el && el.clientWidth && el.clientWidth < 700);
      let px = Math.max(bottomOf(opts.topBox), narrow ? bottomOf(opts.legendBox) : 0) + 8;
      // the pinned tags sit at the LEFT, one under another below the legend, away from the price scale's own labels and
      // the newest candles at the right (review R3)
      if (pinBox && pinBox.style) pinBox.style.setProperty("--pin-top", (bottomOf(opts.legendBox) + 6 || 64) + "px");
      if (narrow) px += state.pinned.filter((p) => p.edge === "top").length * 30;
      if (px <= 8) px = 44;
      // A phone keeps the candles at 55% or more of the pane: a lower cap on the band and a slimmer volume strip.
      const h = (el && el.clientHeight) || 0, top = Math.min(narrow ? 0.2 : 0.25, Math.max(0.1, h ? px / h : 0.1)), vb = narrow ? 0.16 : 0.22;
      try { chart.priceScale("right").applyOptions({ autoScale: true, scaleMargins: hasVolume() ? { top: Math.max(0.08, top), bottom: vb } : { top, bottom: 0.1 } }); } catch (e) {}
      try { chart.priceScale("vol").applyOptions({ scaleMargins: { top: 1 - vb + 0.04, bottom: 0 } }); } catch (e) {}
    },
    toNow() { chart.timeScale().scrollToRealTime(); },
    /* Draw minute `bar` up to fraction f (1: the whole minute; it is then part of its candle). */
    drawMinute(bar, f) {
      if (!state.minute || state.minute.t !== bar[0]) {
        const last = state.bars[state.bars.length - 1], bin = C.binStart(bar[0], state.tfm);
        state.minute = { t: bar[0], base: last && last[0] === bin ? last : null };
      }
      const c = C.mergeBar(state.minute.base, f >= 1 ? bar : C.partialMinute(bar, f), state.tfm);
      const n = state.bars.length;
      if (n && state.bars[n - 1][0] === c[0]) state.bars[n - 1] = c; else state.bars.push(c);
      candles.update(barOf(c));
      if (hasVolume()) volume.update(volOf(c));
      if (f >= 1) state.minute = null;
      return c;
    },
    last() { return state.bars[state.bars.length - 1] || null; },
    candleAt(i) { return state.bars[i] || null; },
    /* The present levels: an alert's plan (kind "alert") or the open trade's, with their labels; null hides them. */
    setLevels(src, kind, labels) {
      const key = src ? [kind, src.entry, src.stop, src.target, src.stop0, src.target0, labels && labels.stop, labels && labels.target].join("|") : "";
      if (key === state.levelKey) return;
      state.levelKey = key;
      if (!src) {
        state.levels = null;
        LEVELS.forEach((n) => lines[n].applyOptions({ lineVisible: false, axisLabelVisible: false, title: "" }));
      } else {
        const lv = [];
        LEVELS.forEach((n) => {
          const p = src[n], faintSame = (n === "stop0" && Number(src.stop0) === Number(src.stop)) || (n === "target0" && Number(src.target0) === Number(src.target));
          if (!C.isNum(p) || faintSame || (kind === "alert" && n.endsWith("0"))) { lines[n].applyOptions({ lineVisible: false, axisLabelVisible: false, title: "" }); return; }
          lines[n].applyOptions({ ...lineStyle(n, kind), price: Number(p), lineVisible: true, title: (labels && labels[n]) || n });
          if (!n.endsWith("0")) lv.push({ name: n, label: n === "entry" ? (kind === "alert" ? "Alert entry" : "Entry") : (n === "stop" ? "Stop" : "Target"), price: Number(p) });
        });
        state.levels = lv;
      }
      try { chart.priceScale("right").applyOptions({ autoScale: true }); } catch (e) {}
    },
    /* z: {minute (the entry's minute, unix), entry, stop, target} or null; drawn from the entry's candle in this timeframe */
    setZones(z) { state.zones = z; zones.set(z ? { ...z, t0: C.binStart(z.minute, state.tfm) } : null); },
    /* Markers: records {t (unix of the minute's candle in the chart timeframe is computed here), ...} from SimCore.markerFor */
    setMarkers(list) { state.markers = (list || []).slice(); ctl.redrawMarkers(); },
    redrawMarkers() {
      const first = state.bars.length ? state.bars[0][0] : Infinity;
      // markers that fall on the same candle and side are merged into one ("Alert · In 413.20"): no stacked labels
      const groups = new Map();
      state.markers.forEach((m) => {
        const time = C.binStart(m.minute, state.tfm);
        if (time < first) return;
        const k = time + "|" + m.position, g = groups.get(k);
        if (!g) groups.set(k, { time, position: m.position, shape: m.shape, color: m.color, text: m.text });
        else { g.text = g.text && m.text ? g.text + " · " + m.text : (g.text || m.text); g.shape = m.shape; g.color = m.color; }
      });
      const out = [...groups.values()].map((m) => ({ time: m.time, position: m.position, shape: m.shape, color: cssv(m.color), text: m.text, size: 1 }))
        .sort((a, b) => a.time - b.time);
      marks.setMarkers(out);
    },
    /* The strategy's lines: defs [{id, label}] for the session; shown only while `on` (chart timeframe = strategy timeframe). */
    setOverlayDefs(defs) {
      state.overlays = (defs || []).slice(0, overlays.length);
      state.overlayLast = [];
      overlays.forEach((s, i) => { s.setData([]); s.applyOptions({ visible: false, color: cssv(OVERLAY_COLOURS[i]) }); });
    },
    overlayIndex(id) { return state.overlays.findIndex((d) => d.id === id); },
    overlayColour(i) { return OVERLAY_COLOURS[i]; },
    setOverlay(id, points) {
      const i = ctl.overlayIndex(id);
      if (i < 0) return;
      const d = C.overlayData(points, state.tfm, null);
      overlays[i].setData(d);
      state.overlayLast[i] = d.length ? [d[d.length - 1].time, d[d.length - 1].value] : null;
    },
    addOverlay(id, points) {
      const i = ctl.overlayIndex(id);
      if (i < 0) return;
      C.overlayData(points, state.tfm, state.overlayLast[i]).forEach((p) => { try { overlays[i].update(p); state.overlayLast[i] = [p.time, p.value]; } catch (e) {} });
    },
    showOverlays(on) { state.overlayOn = !!on; overlays.forEach((s, i) => s.applyOptions({ visible: !!on && i < state.overlays.length })); },
    overlayValue(id, t) {
      const i = ctl.overlayIndex(id);
      if (i < 0) return null;
      const d = overlays[i].data ? overlays[i].data() : [];
      let lo = 0, hi = d.length - 1, ans = null;
      while (lo <= hi) { const m = (lo + hi) >> 1; if (d[m].time <= t) { ans = d[m].color === "transparent" && d[m].time < t ? null : (d[m].value ?? null); lo = m + 1; } else hi = m - 1; }   // none inside a gap
      return ans;
    },
    hideDates(on) { state.hideDates = !!on; chart.applyOptions({ timeScale: { tickMarkFormatter: tickMark }, localization: { timeFormatter: fmtTime } }); },
    theme() {
      // colours only: the bar spacing and right offset of chartOptions() would undo the reader's zoom (and the start view)
      const o = chartOptions(); delete o.timeScale.barSpacing; delete o.timeScale.rightOffset; delete o.rightPriceScale.scaleMargins;
      chart.applyOptions(o);
      candles.applyOptions(candleColours());
      overlays.forEach((s, i) => s.applyOptions({ color: cssv(OVERLAY_COLOURS[i]) }));
      if (hasVolume()) volume.setData(state.bars.map(volOf));
      const k = state.levelKey; state.levelKey = null;
      if (k) LEVELS.forEach((n) => { const o = lines[n].options(); if (o.lineVisible) lines[n].applyOptions(lineStyle(n, k.startsWith("alert") ? "alert" : "trade")); });
      state.levelKey = k;
      ctl.redrawMarkers();
      if (state.zones) ctl.setZones(state.zones);
    },
    /* Scroll the chart so the candle of unix time t is in the middle. */
    scrollTo(t) {
      const bin = C.binStart(t, state.tfm), i = state.bars.findIndex((c) => c[0] >= bin);
      if (i < 0) return;
      const r = chart.timeScale().getVisibleLogicalRange(), half = r ? Math.max(20, (r.to - r.from) / 2) : 60;
      chart.timeScale().setVisibleLogicalRange({ from: i - half, to: i + half });
    },
    /* For the browser checks: the share of the price pane's height the visible candles fill (0 to 1). */
    fillRatio() {
      const r = chart.timeScale().getVisibleLogicalRange();
      if (!r || !state.bars.length) return null;
      const vis = state.bars.slice(Math.max(0, Math.floor(r.from)), Math.min(state.bars.length, Math.ceil(r.to) + 1));
      if (!vis.length) return null;
      const hi = Math.max(...vis.map((c) => c[2])), lo = Math.min(...vis.map((c) => c[3]));
      const y1 = candles.priceToCoordinate(hi), y2 = candles.priceToCoordinate(lo), h = chart.paneSize ? chart.paneSize(0).height : el.clientHeight;
      return y1 === null || y2 === null || !h ? null : Math.abs(y2 - y1) / h;
    },
  };
  return ctl;
}
const api = { create, zonesPrimitive, SHOW, RIGHT };
if (typeof module !== "undefined" && module.exports) module.exports = api;
if (root) root.SimChart = api;
})(typeof window !== "undefined" ? window : null);
