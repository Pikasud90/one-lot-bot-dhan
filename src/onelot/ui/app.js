/* One Lot Desk — thin client over the engine API. No framework, no build step.
   Tabs: home (account, cached), live (SSE), lab (backtests), data, news, settings. */
(() => {
"use strict";
const TOKEN = window.ONELOT_TOKEN || "";
const $ = (id) => document.getElementById(id);
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(Number(v))) ? "–" : Number(v).toFixed(d);
const rup = (v) => (v === null || v === undefined || Number.isNaN(Number(v))) ? "–"
  : (Number(v) < 0 ? "−" : "") + "₹" + Math.abs(Number(v)).toLocaleString("en-IN", { maximumFractionDigits: 0 });
const cls = (v) => v > 0 ? "pos" : (v < 0 ? "neg" : "");
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[m]));
const kv = (k, v, c = "") => `<div class="kv"><span>${esc(k)}</span><span class="${c}">${v}</span></div>`;
const pct = (v, d = 1) => (v === null || v === undefined || Number.isNaN(Number(v))) ? "–" : Number(v).toFixed(d) + "%";
const tsf = (s) => s ? String(s).replace("T", " ").slice(0, 16) : "–";
const store = { get(k, d) { try { return JSON.parse(localStorage.getItem("onelot." + k)) ?? d; } catch (e) { return d; } },
                set(k, v) { try { localStorage.setItem("onelot." + k, JSON.stringify(v)); } catch (e) {} } };

function toast(msg, kind = "") {
  const d = document.createElement("div"); d.className = "toast " + kind; d.textContent = msg;
  $("toasts").appendChild(d); setTimeout(() => d.remove(), 5000);
}
async function api(url, body, method) {
  const opt = { method: method || (body ? "POST" : "GET"), headers: { "x-onelot-token": TOKEN } };
  if (body) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(url, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { const m = (j && j.detail) ? (typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail)) : r.status; toast("Request failed: " + m, "bad"); throw new Error(m); }
  return j;
}
function table(cols, rows, opts = {}) {
  if (!rows || !rows.length) return `<div class="muted">${esc(opts.empty || "Nothing here.")}</div>`;
  const head = cols.map(c => `<th class="${c.n ? "n" : ""}">${esc(c.l)}</th>`).join("");
  const body = rows.map((r, i) => `<tr class="${opts.rowClass ? opts.rowClass(r) : ""}" data-i="${i}">` +
    cols.map(c => `<td class="${c.n ? "n" : ""} ${c.c ? c.c(r) : ""}">${c.f ? c.f(r) : esc(r[c.k])}</td>`).join("") + "</tr>").join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}
function plotReady() { return typeof window.Plotly !== "undefined"; }
const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const PLOT_LAYOUT = () => ({
  paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)", font: { color: cssVar("--plot-font"), family: "JetBrains Mono, monospace", size: 12 },
  margin: { l: 62, r: 18, t: 34, b: 40 }, xaxis: { gridcolor: cssVar("--grid") }, yaxis: { gridcolor: cssVar("--grid") }, showlegend: false,
});

/* ── theme (day by default; the choice is per browser) ───────────────── */
function applyTheme(t) {
  document.documentElement.dataset.theme = t; store.set("theme", t);
  $("theme-toggle").textContent = t === "night" ? "☀ Day" : "☾ Night";
  if (!plotReady()) return;
  document.querySelectorAll(".plot").forEach(el => {
    if (!el.data) return;
    const patch = { "font.color": cssVar("--plot-font"), "xaxis.gridcolor": cssVar("--grid"), "yaxis.gridcolor": cssVar("--grid") };
    if (el.layout && el.layout.yaxis2) patch["yaxis2.gridcolor"] = cssVar("--grid");
    Plotly.relayout(el, patch);
  });
}
$("theme-toggle").onclick = () => applyTheme(document.documentElement.dataset.theme === "night" ? "day" : "night");
applyTheme(store.get("theme", "day") === "night" ? "night" : "day");

/* ── tabs ─────────────────────────────────────────────────────────────── */
const tabLoaders = {};
function showTab(name) {
  document.querySelectorAll(".tab").forEach(s => s.classList.toggle("active", s.dataset.tab === name));
  document.querySelectorAll("#tabs a").forEach(a => a.classList.toggle("active", a.dataset.tab === name));
  store.set("tab", name);
  if (tabLoaders[name]) tabLoaders[name]();
}
window.addEventListener("hashchange", () => showTab(location.hash.slice(1) || "home"));

/* ── header badges + warnings (from the live stream) ─────────────────── */
let latest = null;
function renderBadges(s) {
  const st = s.status || {}, h = s.health || {}, risk = s.risk || {};
  const b = [];
  const bd = (t, c = "") => `<span class="badge ${c}"><span>${esc(t)}</span></span>`;
  b.push(bd(st.mode || "?", st.mode === "LIVE" ? "bad" : "ok"));
  b.push(bd(st.broker || "?"));
  const hs = h.state || "?";
  b.push(bd(hs, hs === "HEALTHY" ? "ok" : (hs === "DEAD" ? "bad" : "warn")));
  b.push(bd(risk.state || "?", risk.state === "HALTED" ? "bad" : "ok"));
  if (st.session && !st.session.entries_allowed) b.push(bd("entries closed", "warn"));
  $("badges").innerHTML = b.join("");
}
function renderWarnings(s) {
  const w = (s.status && s.status.warnings) || [];
  $("warnings").innerHTML = w.length ? `<div class="warnbar"><b>${w.length} startup warning${w.length > 1 ? "s" : ""}</b><ul>` +
    w.map(x => `<li>${esc(x)}</li>`).join("") + `</ul></div>` : "";
}

/* ═══════════════════════════════ HOME ═══════════════════════════════ */
let acctLoaded = false;
async function loadAccount(refresh) {
  const snap = refresh ? await api("/api/account/refresh", {}) .then(r => r.snapshot) : await api("/api/account");
  renderAccount(snap); acctLoaded = true;
}
function renderAccount(a) {
  const s = a.summary || {}, p = a.panels || {};
  $("acct-age").textContent = a.fetched_at ? ("cached " + tsf(a.fetched_at) + (a.stale ? " · stale" : "")) : "never fetched";
  if (!a.configured) {
    $("acct-notice").innerHTML = `<div class="notice">No Dhan credentials yet. Add your client ID and a free-tier access token under
      <a href="#settings">Settings</a>; the ledger, funds, holdings and positions work without the paid Data API.</div>`;
  } else {
    const errs = Object.entries(p).filter(([, v]) => v && v.error).map(([k, v]) => `${k}: ${v.error}`);
    $("acct-notice").innerHTML = errs.length ? `<div class="warnbar"><b>Some panels could not be fetched</b><ul>${errs.map(e => `<li>${esc(e)}</li>`).join("")}</ul></div>` : "";
  }
  const tile = (l, v, i = "", c = "") => `<div class="tile"><small>${l}</small><b class="${c}">${v}</b><i>${i}</i></div>`;
  $("acct-tiles").innerHTML =
    tile("Available balance", rup(s.available_balance), "fund limit") +
    tile("Withdrawable", rup(s.withdrawable_balance)) +
    tile("Utilised", rup(s.utilized_amount), "margin blocked") +
    tile("Collateral", rup(s.collateral)) +
    tile("Holdings", s.holdings_count ?? "–", "invested " + rup(s.holdings_invested)) +
    tile("Holdings value", rup(s.holdings_value), s.holdings_pnl === null || s.holdings_pnl === undefined ? "no LTP in payload" :
      `<span class="${cls(s.holdings_pnl)}">P&L ${rup(s.holdings_pnl)} (${pct(s.holdings_invested ? s.holdings_pnl / s.holdings_invested * 100 : null)})</span>`) +
    tile("Open positions", s.open_positions ?? "–", "broker net qty ≠ 0") +
    tile("Realised today", rup(s.positions_realized), "", cls(s.positions_realized)) +
    tile("Unrealised", rup(s.positions_unrealized), "open positions", cls(s.positions_unrealized)) +
    tile("Ledger balance", rup(s.ledger_running_balance), s.ledger_last_date ? "as of " + esc(s.ledger_last_date) : "latest voucher") +
    tile("30-day flows", `<span class="pos" style="font-size:.8em">+${rup(s.ledger_credits).replace("₹", "")}</span><span class="neg" style="font-size:.8em"> / −${rup(s.ledger_debits).replace("₹", "")}</span>`, "ledger credits / debits, ₹");
  const prof = (p.profile && p.profile.data) || {};
  $("acct-profile").innerHTML = kv("Client", s.client_id_suffix ? "…" + s.client_id_suffix : "–") +
    kv("Token valid till", esc(prof.tokenValidity || "–")) +
    kv("Data API plan", `<span class="${String(prof.dataPlan || "").toLowerCase() === "active" ? "pos" : "warn"}">${esc(prof.dataPlan || "not active / unknown")}</span>`) +
    kv("Data API valid till", esc(prof.dataValidity || "–")) +
    kv("Active segments", esc(prof.activeSegment || "–")) +
    kv("DDPI / MTF", esc((prof.ddpi || "–") + " / " + (prof.mtf || "–")));
  const pos = (p.positions && p.positions.data) || [];
  $("acct-positions").innerHTML = table([
    { l: "Symbol", k: "tradingSymbol" }, { l: "Seg", k: "exchangeSegment" }, { l: "Product", k: "productType" },
    { l: "Net qty", n: 1, f: r => fmt(r.netQty, 0) }, { l: "Buy avg", n: 1, f: r => fmt(r.buyAvg) }, { l: "Sell avg", n: 1, f: r => fmt(r.sellAvg) },
    { l: "Realised", n: 1, f: r => rup(r.realizedProfit), c: r => cls(r.realizedProfit) },
    { l: "Unrealised", n: 1, f: r => rup(r.unrealizedProfit), c: r => cls(r.unrealizedProfit) },
  ], pos, { empty: "No positions at the broker." });
  const hold = (p.holdings && p.holdings.data) || [];
  $("acct-holdings").innerHTML = table([
    { l: "Symbol", k: "tradingSymbol" }, { l: "Qty", n: 1, f: r => fmt(r.totalQty, 0) },
    { l: "Available", n: 1, f: r => fmt(r.availableQty, 0) }, { l: "Avg cost", n: 1, f: r => fmt(r.avgCostPrice) },
    { l: "LTP", n: 1, f: r => +r.lastTradedPrice > 0 ? fmt(r.lastTradedPrice) : "–" },
    { l: "Invested", n: 1, f: r => rup((+r.avgCostPrice || 0) * (+r.totalQty || 0)) },
    { l: "Value", n: 1, f: r => +r.lastTradedPrice > 0 ? rup(+r.lastTradedPrice * (+r.totalQty || 0)) : "–" },
    { l: "P&L", n: 1, f: r => +r.lastTradedPrice > 0 ? rup((+r.lastTradedPrice - +r.avgCostPrice) * (+r.totalQty || 0)) : "–",
      c: r => +r.lastTradedPrice > 0 ? cls((+r.lastTradedPrice - +r.avgCostPrice) * (+r.totalQty || 0)) : "" },
    { l: "P&L %", n: 1, f: r => +r.lastTradedPrice > 0 && +r.avgCostPrice > 0 ? pct((+r.lastTradedPrice / +r.avgCostPrice - 1) * 100) : "–",
      c: r => +r.lastTradedPrice > 0 ? cls(+r.lastTradedPrice - +r.avgCostPrice) : "" },
  ], hold, { empty: "No holdings." });
  const when = (v) => { const t = Date.parse(v || ""); return Number.isNaN(t) ? 0 : t; };
  const led = ((p.ledger && p.ledger.data) || []).map((r, i) => [r, i]).sort((a, b) => (when(b[0].voucherdate) - when(a[0].voucherdate)) || (a[1] - b[1])).map(x => x[0]);
  $("acct-ledger").innerHTML = table([
    { l: "Date", k: "voucherdate" }, { l: "Narration", f: r => esc(String(r.narration || r.voucherdesc || "").slice(0, 60)) },
    { l: "Debit", n: 1, f: r => +r.debit ? rup(r.debit) : "<span class='dim'>–</span>", c: r => +r.debit ? "neg" : "" },
    { l: "Credit", n: 1, f: r => +r.credit ? rup(r.credit) : "<span class='dim'>–</span>", c: r => +r.credit ? "pos" : "" },
    { l: "Balance", n: 1, f: r => rup(r.runbal) },
  ], led, { empty: "No ledger entries in the window." });
  const tr = ((p.trades && p.trades.data) || []).slice().sort((a, b) => when(b.exchangeTime || b.createTime) - when(a.exchangeTime || a.createTime));
  $("acct-trades").innerHTML = table([
    { l: "Time", f: r => tsf(r.exchangeTime || r.createTime) },
    { l: "Symbol", f: r => r.customSymbol || r.tradingSymbol ? esc(r.customSymbol || r.tradingSymbol)
        : (r.isin ? "ISIN " + esc(r.isin) : `<span class="dim">non-market entry (allotment / corporate action)</span>`) },
    { l: "Seg", f: r => esc((r.exchangeSegment || "") + (r.productType ? " · " + r.productType : "")) },
    { l: "Side", k: "transactionType" }, { l: "Qty", n: 1, f: r => fmt(r.tradedQuantity, 0) }, { l: "Price", n: 1, f: r => +r.tradedPrice ? fmt(r.tradedPrice) : "<span class='dim'>–</span>" },
    { l: "Charges", n: 1, f: r => rup((+r.brokerageCharges || 0) + (+r.stt || 0) + (+r.exchangeTransactionCharges || 0) + (+r.sebiTax || 0) + (+r.stampDuty || 0) + (+r.serviceTax || 0)) },
  ], tr, { empty: "No trades in the window." });
}
function renderHomeJournal(s) {
  const m = (s && s.journal) || {};
  $("home-journal").innerHTML = (m.trades | 0) === 0 ? `<div class="muted">No closed paper trades yet.</div>` :
    kv("Trades", m.trades) + kv("Win rate", pct((m.win_rate || 0) * 100)) + kv("Net P&L", rup(m.net_pnl), cls(m.net_pnl)) +
    kv("Expectancy", fmt(m.expectancy_r, 2) + "R") + kv("Max drawdown", rup(m.max_drawdown), "neg") +
    kv("Sharpe", m.sharpe == null ? (m.sharpe_note || "–") : fmt(m.sharpe, 2));
}
tabLoaders.home = () => { if (!acctLoaded) loadAccount(false).catch(() => {}); if (latest) renderHomeJournal(latest); };
$("acct-refresh").onclick = async () => { $("acct-refresh").disabled = true; try { await loadAccount(true); toast("Account refreshed", "ok"); } finally { $("acct-refresh").disabled = false; } };

/* ═══════════════════════════════ LIVE ═══════════════════════════════ */
let lastSignalId = null, audioCtx = null;
function beep() {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    [880, 1320].forEach((f, i) => {
      const o = audioCtx.createOscillator(), g = audioCtx.createGain();
      o.frequency.value = f; o.connect(g); g.connect(audioCtx.destination);
      const t = audioCtx.currentTime + i * 0.18;
      g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(0.25, t + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 0.16); o.start(t); o.stop(t + 0.18);
    });
  } catch (e) {}
}
function renderSignal(s) {
  const card = $("signalCard"), body = $("signalBody"), sig = s.active_signal;
  if (!sig) {
    card.classList.add("none");
    body.innerHTML = `<div class="muted">No active signal. Last evaluation: ${esc((s.status && s.status.last_evaluation_summary) || "–")}</div>`;
    lastSignalId = null; return;
  }
  card.classList.remove("none");
  if (sig.signal_id !== lastSignalId) { lastSignalId = sig.signal_id; beep(); toast("SIGNAL: " + sig.direction + " " + sig.strike + " " + sig.opt_type, "warn"); }
  const left = Math.max(0, Math.round(s.signal_seconds_remaining || 0));
  const warn = (sig.warnings || []).length ? `<div class="warnbar">` + sig.warnings.map(esc).join("<br>") + `</div>` : "";
  body.innerHTML = `
    <div class="sig-title">BUY ${esc(sig.underlying)} ${esc(sig.expiry)} ${fmt(sig.strike, 0)} ${esc(sig.opt_type)}
      &nbsp;<span class="num" style="font-size:16px">×${sig.lots} lot (${sig.qty} qty)</span></div>
    <div class="sig-sub">Expires in <span class="countdown ${left < 20 ? "urgent" : ""}" id="cd">${left}s</span>
      · delta ${fmt(sig.delta, 2)} · IV ${fmt((sig.iv || 0) * 100, 1)} · IVP ${fmt(sig.ivp && sig.ivp.percentile, 0)} · spread ₹${fmt(sig.quote && sig.quote.spread)}</div>
    <div class="sig-nums">
      <div class="sig-num"><small>Entry (ask+slip)</small><b>${fmt(sig.entry_eff)}</b></div>
      <div class="sig-num"><small>Breakeven</small><b>${fmt(sig.breakeven)}</b></div>
      <div class="sig-num"><small>Target</small><b class="pos">${fmt(sig.target_level)}</b></div>
      <div class="sig-num"><small>Stop</small><b class="neg">${fmt(sig.stop_level)}</b></div>
      <div class="sig-num"><small>Net reward</small><b class="pos">${rup(sig.net_reward)}</b></div>
      <div class="sig-num"><small>Net risk</small><b class="neg">${rup(sig.net_risk)}</b></div>
      <div class="sig-num"><small>Net R/R</small><b>${fmt(sig.rr_net)}</b></div>
      <div class="sig-num"><small>Outlay</small><b>${rup(sig.premium_outlay)}</b></div>
    </div>${warn}
    <div class="actions">
      <label class="inline">Your actual fill <input type="number" step="0.05" id="fill" placeholder="${fmt(sig.entry_eff)}" style="width:120px"></label>
      <button class="btn take" id="takeBtn"><span>Trade taken</span></button>
      <button class="btn" id="skipBtn"><span>Skipped</span></button>
    </div>
    <div class="hint">Enter the price you actually filled at — the stop travels with your fill so your risk stays 1R.</div>`;
  $("takeBtn").onclick = async () => {
    const f = $("fill").value;
    const r = await api(`/api/signal/${sig.signal_id}/ack`, { action: "TAKE", fill_price: f ? Number(f) : null, source: "dashboard" });
    if (r && r.message) toast(r.message, r.accepted ? "ok" : "warn");
  };
  $("skipBtn").onclick = () => api(`/api/signal/${sig.signal_id}/ack`, { action: "SKIP", source: "dashboard" });
}
function renderPositions(s) {
  const el = $("positions"), ps = s.positions || [];
  if (!ps.length) { el.innerHTML = `<div class="muted">Flat.</div>`; return; }
  el.innerHTML = `<table><thead><tr><th>Contract</th><th class="n">Lots</th><th class="n">Entry</th><th class="n">Stop</th>
    <th class="n">Target</th><th class="n">Trail</th><th>State</th><th>Data</th><th></th></tr></thead><tbody>` +
    ps.map(p => `<tr><td>${fmt(p.strike, 0)} ${esc(p.opt_type)}</td><td class="n">${p.lots}</td><td class="n">${fmt(p.entry_price)}</td>
      <td class="n">${fmt(p.effective_stop)}</td><td class="n">${fmt(p.target)}</td><td class="n">${p.trail_active ? fmt(p.trail_stop) : "–"}</td>
      <td>${esc(p.state)}</td><td class="${p.degraded_data ? "neg" : "pos"}">${p.degraded_data ? "NO QUOTE" : "ok"}</td>
      <td><input type="number" step="0.05" id="x_${p.position_id}" placeholder="exit" style="width:90px;display:inline-block;margin:0">
        <button class="btn small" data-close="${p.position_id}"><span>Close</span></button></td></tr>`).join("") + `</tbody></table>`;
  el.querySelectorAll("[data-close]").forEach(b => b.onclick = async () => {
    const v = $("x_" + b.dataset.close).value;
    if (!v) { toast("Enter the price you actually exited at.", "warn"); return; }
    await api(`/api/position/${b.dataset.close}/close`, { exit_price: Number(v), reason: "MANUAL" });
  });
}
function renderPanels(s) {
  const st = s.status || {}, p = s.prices || {}, r = s.regime || {}, ivp = s.ivp || {}, risk = s.risk || {}, h = s.health || {}, ch = s.chain || {};
  $("market").innerHTML = kv("Index", fmt(p.index)) + kv("Futures", fmt(p.futures)) + kv("Expiry", esc(st.expiry || "–")) +
    kv("Lot size", st.lot_size || "–") + kv("Square-off", esc((st.session && st.session.squareoff_at) || "–")) +
    kv("Bars evaluated", st.evaluations_today || 0) + kv("Signals today", st.signals_today || 0);
  $("regime").innerHTML = kv("Regime", `${esc(r.regime || "–")} (${fmt(r.confidence, 2)})`) + kv("Method", esc(r.method || "–")) +
    kv("IV percentile", fmt(ivp.percentile, 1)) + kv("IV history", `${ivp.n_observations || 0}/${ivp.required_observations || 0}`) +
    kv("IVP usable", ivp.usable ? "yes" : "NO") + (ivp.note ? `<div class="hint">${esc(ivp.note)}</div>` : "");
  const L = risk.limits || {};
  $("risk").innerHTML = kv("State", esc(risk.state || "–")) + kv("Realised P&L", rup(risk.realized_pnl), cls(risk.realized_pnl)) +
    kv("Daily loss limit", rup(L.daily_loss_limit)) + kv("Drawdown", `${rup(risk.drawdown)} / ${rup(L.max_drawdown_limit)}`) +
    kv("Trades today", `${risk.trades_taken || 0}/${L.max_trades_per_day || "–"}`) +
    kv("Consecutive losses", `${risk.consecutive_losses || 0}/${L.max_consecutive_losses || "–"}`) +
    kv("Open", `${risk.open_positions || 0}/${L.max_open_positions || "–"}`) +
    (risk.halt_reason ? `<div class="warnbar mt">${esc(risk.halt_reason)}</div>` : "");
  $("health").innerHTML = kv("State", esc(h.state || "–")) + kv("May signal", h.may_signal ? "yes" : "NO") +
    kv("Chain age", ch.age_sec !== undefined ? ch.age_sec + "s" : "–") + kv("Chain polls", ch.polls || 0) +
    kv("Greeks repaired / rejected", `${ch.greeks_repaired || 0} / ${ch.greeks_rejected || 0}`) +
    ((h.reasons && h.reasons.length) ? `<div class="hint">` + h.reasons.map(esc).join("<br>") + `</div>` : "");
}
function renderJournal(s) {
  const m = s.journal || {};
  $("journal").innerHTML = (m.trades | 0) === 0 ? `<div class="muted">No closed trades yet.</div>` :
    kv("Trades", m.trades) + kv("Win rate", pct((m.win_rate || 0) * 100)) + kv("Net P&L", rup(m.net_pnl), cls(m.net_pnl)) +
    kv("Gross P&L", rup(m.gross_pnl)) + kv("Total costs", rup(m.total_costs), "neg") +
    kv("Costs / gross", m.costs_pct_of_gross != null ? pct(m.costs_pct_of_gross * 100) : "–") +
    kv("Expectancy", fmt(m.expectancy_r, 2) + "R") + kv("Max drawdown", rup(m.max_drawdown), "neg") +
    `<div class="mt"><a href="/api/journal.csv" download>Download CSV</a></div>`;
}
async function renderGates() {
  try {
    const j = await api("/api/gates?hours=24");
    const h = (j.histogram || []).filter(x => x.rejections > 0).slice(0, 12), el = $("gates");
    if (!h.length) { el.innerHTML = `<div class="muted">No gate evaluations recorded yet.</div>`; return; }
    const max = Math.max(...h.map(x => x.rejections));
    el.innerHTML = `<table><thead><tr><th>Gate</th><th class="n">Rejected</th><th class="n">Pass</th><th style="width:35%"></th></tr></thead><tbody>` +
      h.map(x => `<tr><td>${esc(x.gate)}</td><td class="n">${x.rejections}</td><td class="n">${fmt(x.pass_rate * 100, 0)}%</td>
        <td><div class="bar"><i style="width:${(x.rejections / max * 100).toFixed(0)}%"></i></div></td></tr>`).join("") + `</tbody></table>`;
  } catch (e) {}
}
function renderFeed(items) {
  if (!items || !items.length) return;
  const el = $("feed");
  items.forEach(n => {
    const d = document.createElement("div");
    const c = ["SIGNAL", "EXIT", "CRITICAL"].includes(n.level) ? "neg" : (n.level === "WARNING" ? "warn" : "muted");
    d.innerHTML = `<span class="lvl ${c}">${esc(n.level)}</span> <b>${esc(n.title)}</b><br><span class="muted">${esc((n.body || "").split("\n")[0])}</span>`;
    el.prepend(d);
    if (["EXIT", "CRITICAL"].includes(n.level)) toast(n.title, "bad");
  });
  while (el.children.length > 80) el.removeChild(el.lastChild);
}
function applyStream(payload) {
  latest = payload.state;
  renderBadges(latest); renderWarnings(latest); renderSignal(latest); renderPositions(latest);
  renderPanels(latest); renderJournal(latest); renderFeed(payload.notifications);
  if (document.querySelector('[data-tab="home"]').classList.contains("active")) renderHomeJournal(latest);
}
setInterval(() => {
  if (!latest || !latest.active_signal) return;
  latest.signal_seconds_remaining = Math.max(0, (latest.signal_seconds_remaining || 0) - 1);
  const el = $("cd"); if (el) { const v = Math.round(latest.signal_seconds_remaining); el.textContent = v + "s"; el.classList.toggle("urgent", v < 20); }
}, 1000);
setInterval(renderGates, 30000);
function connect() {
  const es = new EventSource("/api/stream");
  es.onmessage = e => { $("conn").textContent = " · live"; try { applyStream(JSON.parse(e.data)); } catch (err) {} };
  es.onerror = () => { $("conn").textContent = " · reconnecting…"; es.close(); setTimeout(connect, 3000); };
}
$("haltBtn").onclick = () => { if (confirm("Halt trading? No new signals will fire until you clear it.")) api("/api/risk/halt", {}); };
$("resetBtn").onclick = () => { if (confirm("Clear the halt and resume signalling?")) api("/api/risk/reset", {}); };
tabLoaders.live = renderGates;

/* ═══════════════════════════════ LAB ════════════════════════════════ */
const lab = { strategies: [], instruments: [], cat: store.get("lab.cat", "OPTIONS"), runs: [], selected: null, poll: null };
const COMMON_EXITS = new Set(["stop_atr_min", "stop_atr_max", "min_risk_pct", "stop_datr_min", "max_risk_pct", "target_r", "trail_atr_mult",
  "trail_atr_period", "trail_activate_r", "partial_r", "partial_frac", "breakeven_after_partial", "max_bars", "exit_time"]);
const COMMON_FILTERS = new Set(["allow_long", "allow_short", "entry_start", "entry_end", "min_strength", "market_filter", "regime_filter",
  "cooldown_bars", "max_trades_per_day", "timeframe"]);
const TF_CANDIDATES = ["1m", "2m", "3m", "4m", "5m", "6m", "10m", "12m", "15m", "20m", "30m", "45m", "60m", "120m", "1d"];
const mins = (tf) => tf === "1d" ? 375 : parseInt(tf, 10);

async function loadLab() {
  if (!lab.strategies.length) {
    const [s, i] = await Promise.all([api("/api/lab/strategies"), api("/api/lab/instruments")]);
    lab.strategies = s.strategies; lab.instruments = i.instruments.filter(x => x.tradeable);
    renderCats(); renderInstruments(); renderStrategies(); onStrategyChange();
  }
  refreshRuns();
}
function renderCats() {
  const cats = ["OPTIONS", "FUTURES", "STOCK", "CURRENCY"];
  $("lab-cats").innerHTML = cats.map(c => `<span class="chip ${c === lab.cat ? "active" : ""}" data-cat="${c}">${c}</span>`).join("");
  $("lab-cats").querySelectorAll(".chip").forEach(ch => ch.onclick = () => { lab.cat = ch.dataset.cat; store.set("lab.cat", lab.cat); renderCats(); renderInstruments(); onStrategyChange(); });
}
function renderInstruments() {
  const list = lab.instruments.filter(i => i.category === lab.cat);
  const prev = store.get("lab.inst." + lab.cat, null);
  $("lab-instrument").innerHTML = list.map(i => `<option value="${i.key}" ${i.key === prev ? "selected" : ""}>${esc(i.label)}</option>`).join("");
  onInstrumentChange();
}
function currentInstrument() { return lab.instruments.find(i => i.key === $("lab-instrument").value); }
function onInstrumentChange() {
  const inst = currentInstrument(); if (!inst) { $("lab-inst-hint").textContent = "no instruments in this category"; return; }
  store.set("lab.inst." + lab.cat, inst.key);
  const cov = inst.coverage || {};
  const bases = Object.keys(cov).filter(r => r !== "1d");
  const covTxt = Object.entries(cov).map(([r, c]) => `${r}: ${c.first} → ${c.last.slice(0, 10)}`).join(" · ");
  $("lab-inst-hint").innerHTML = (covTxt ? `<b>data</b> ${esc(covTxt)}` : `<span class="warn">no bars yet — backfill in the Data tab</span>`) +
    ` · cost table <b>${esc(inst.product)}</b> · unit <b>${inst.category === "STOCK" ? "1 share" : inst.lot_size + " (1 lot)"}</b>` +
    (inst.category === "OPTIONS" ? ` · <span class="warn">premiums are Black-76 synthetic</span>` : "") +
    (inst.has_volume ? "" : " · no volume → volume filters neutral");
  const allowed = TF_CANDIDATES.filter(tf => tf === "1d" ? !!cov["1d"] : bases.some(b => mins(tf) % mins(b) === 0 && mins(tf) >= mins(b)));
  const prevTf = store.get("lab.tf", "5m");
  $("lab-tf").innerHTML = (allowed.length ? allowed : ["5m"]).map(tf => `<option ${tf === prevTf ? "selected" : ""}>${tf}</option>`).join("");
  if (cov["1d"]) { const c = Object.values(cov); const first = c.map(x => x.first).sort()[0]; $("lab-start").min = first; }
  renderStrategies();
}
function renderStrategies() {
  const prev = store.get("lab.strategy", $("lab-strategy").value || "ema_cross");
  $("lab-strategy").innerHTML = lab.strategies.map(s => {
    const sug = (s.suitable_for || []).includes(lab.cat) ? " ★" : "";
    return `<option value="${s.name}" ${s.name === prev ? "selected" : ""}>${esc(s.label)}${sug}</option>`; }).join("");
}
function currentStrategy() { return lab.strategies.find(s => s.name === $("lab-strategy").value); }
function onStrategyChange(reset) {
  const s = currentStrategy(); if (!s) return;
  store.set("lab.strategy", s.name);
  $("lab-strat-hint").innerHTML = `${esc(s.description)}<br><span class="dim">suited to: ${(s.suitable_for || []).join(", ")}</span>`;
  $("lab-ind").textContent = "· " + (s.indicators || []).join(" · ");
  const saved = reset ? {} : (store.get("lab.params." + s.name, {}) || {});
  const groups = { strategy: [], exits: [], filters: [] };
  Object.entries(s.params).forEach(([k, d]) => {
    if (k === "timeframe") return;
    (COMMON_EXITS.has(k) ? groups.exits : COMMON_FILTERS.has(k) ? groups.filters : groups.strategy).push([k, d]);
  });
  const render = (arr) => arr.map(([k, d]) => {
    const v = saved[k] !== undefined ? saved[k] : d.default;
    const opt = d.opt || null;
    let input;
    if (d.type === "bool") input = `<input type="checkbox" data-p="${k}" data-t="bool" ${v ? "checked" : ""}>`;
    else if (d.type === "int" || d.type === "float") {
      const step = opt ? opt[2] : (d.type === "int" ? 1 : 0.05);
      input = `<input type="number" data-p="${k}" data-t="${d.type}" value="${v}" step="${step}" ${opt ? `min="${opt[0]}" max="${opt[1]}"` : ""}>`;
    } else input = `<input type="text" data-p="${k}" data-t="str" value="${esc(v)}">`;
    return `<label title="${esc(d.description || "")}">${k}${opt ? ` <span class="dim">[${opt[0]}–${opt[1]}]</span>` : ""}${input}<span class="d">${esc(d.description || "")}</span></label>`;
  }).join("");
  $("lab-params-strategy").innerHTML = render(groups.strategy) || `<div class="muted">no strategy-specific knobs</div>`;
  $("lab-params-exits").innerHTML = render(groups.exits);
  $("lab-params-filters").innerHTML = render(groups.filters);
}
function collectParams() {
  const s = currentStrategy(), out = {};
  document.querySelectorAll("#lab-params-strategy [data-p], #lab-params-exits [data-p], #lab-params-filters [data-p]").forEach(el => {
    const k = el.dataset.p, t = el.dataset.t, d = s.params[k];
    let v = t === "bool" ? el.checked : (t === "int" ? parseInt(el.value, 10) : (t === "float" ? parseFloat(el.value) : el.value));
    if (t !== "bool" && (v === "" || Number.isNaN(v))) return;
    if (d && JSON.stringify(v) !== JSON.stringify(d.default)) out[k] = v;
  });
  store.set("lab.params." + s.name, out);
  return out;
}
function labBody() {
  const tf = $("lab-tf").value; store.set("lab.tf", tf);
  return { strategy: $("lab-strategy").value, instrument_key: $("lab-instrument").value, timeframe: tf, params: collectParams(),
    start: $("lab-start").value || null, end: $("lab-end").value || null, slippage_pct: parseFloat($("lab-slip").value) || 0.03,
    cost_multiplier: parseFloat($("lab-costx").value) || 1 };
}
$("lab-instrument").onchange = onInstrumentChange;
$("lab-strategy").onchange = () => onStrategyChange(false);
$("lab-reset").onclick = () => onStrategyChange(true);
$("lab-run").onclick = async () => { const r = await api("/api/lab/backtest", labBody()); toast("Backtest #" + r.run_id + " queued", "ok"); lab.selected = r.run_id; startPoll(); };
$("lab-opt").onclick = async () => { const b = labBody(); b.kind = "optimize"; b.trials = parseInt($("lab-trials").value, 10) || 40; const r = await api("/api/lab/optimize", b); toast("Optimizer #" + r.run_id + " queued", "ok"); lab.selected = r.run_id; startPoll(); };
$("lab-wf").onclick = async () => { const b = labBody(); b.kind = "walkforward"; b.trials = parseInt($("lab-trials").value, 10) || 40; const r = await api("/api/lab/optimize", b); toast("Walk-forward #" + r.run_id + " queued", "ok"); lab.selected = r.run_id; startPoll(); };
$("lab-runs-refresh").onclick = refreshRuns;

function startPoll() { if (lab.poll) return; lab.poll = setInterval(refreshRuns, 2000); refreshRuns(); }
async function refreshRuns() {
  const j = await api("/api/lab/runs?limit=40"); lab.runs = j.runs;
  const busy = lab.runs.some(r => r.status === "QUEUED" || r.status === "RUNNING");
  if (!busy && lab.poll) { clearInterval(lab.poll); lab.poll = null; }
  $("lab-status").innerHTML = busy ? `<span class="warn">working…</span> ` + lab.runs.filter(r => r.status === "RUNNING").map(r => `#${r.id} ${esc(r.message || "")}`).join(" · ") : "";
  $("lab-runs").innerHTML = table([
    { l: "#", k: "id", n: 1 }, { l: "Kind", k: "kind" }, { l: "Label", f: r => esc(r.label || "") },
    { l: "Status", f: r => r.status === "RUNNING" ? `RUNNING <div class="progress"><i style="width:${Math.round((r.progress || 0) * 100)}%"></i></div>` : esc(r.status), c: r => r.status === "FAILED" ? "neg" : (r.status === "DONE" ? "pos" : "warn") },
    { l: "Trades", n: 1, f: r => r.metrics ? fmt(r.metrics.trades, 0) : "–" },
    { l: "Net ₹", n: 1, f: r => r.metrics ? rup(r.metrics.net_pnl) : "–", c: r => r.metrics ? cls(r.metrics.net_pnl) : "" },
    { l: "PF", n: 1, f: r => r.metrics ? fmt(r.metrics.profit_factor) : "–" },
    { l: "Exp R", n: 1, f: r => r.metrics ? fmt(r.metrics.expectancy_r, 3) : "–" },
    { l: "When", f: r => tsf(r.created_at) },
  ], lab.runs, { rowClass: r => "click" + (r.id === lab.selected ? " sel" : ""), empty: "No runs yet — set up a backtest on the left." });
  $("lab-runs").querySelectorAll("tr.click").forEach(tr => tr.onclick = () => { lab.selected = lab.runs[+tr.dataset.i].id; refreshRuns(); showRun(lab.selected); });
  const sel = lab.runs.find(r => r.id === lab.selected);
  if (sel && sel.status === "DONE" && $("lab-result").dataset.run !== String(sel.id)) showRun(sel.id);
  if (sel && sel.status === "FAILED") $("lab-result").innerHTML = `<div class="card"><h2>Run #${sel.id} failed</h2><div class="neg">${esc(sel.message)}</div></div>`;
}
async function showRun(id) {
  const r = await api(`/api/lab/runs/${id}`);
  const el = $("lab-result"); el.dataset.run = String(id);
  if (r.status !== "DONE") { el.innerHTML = `<div class="card"><h2>Run #${id}</h2><div class="muted">${esc(r.status)} ${esc(r.message || "")}</div></div>`; return; }
  if (r.kind === "single") renderBacktest(r); else renderOpt(r);
}
function tile(l, v, i = "", c = "") { return `<div class="tile"><small>${l}</small><b class="${c}">${v}</b><i>${i}</i></div>`; }
function renderBacktest(r) {
  const m = r.metrics || {}, a = r.artifacts || {}, cfg = r.config || {}, tp = m.thirty_pct_rule || {};
  const el = $("lab-result");
  const verdict = (m.trades || 0) < 20 ? ["meh", "TOO FEW TRADES"] : (m.profit_factor >= 1.2 && m.expectancy_r > 0.05 ? ["good", "POSITIVE"] : (m.profit_factor > 1 ? ["meh", "MARGINAL"] : ["bad", "NEGATIVE"]));
  el.innerHTML = `
    <div class="card">
      <div class="row between"><h2>#${r.id} · ${esc(cfg.strategy)} on ${esc(cfg.instrument_key)} · ${esc(cfg.timeframe)}
        <small>${esc(m.category)} · unit ${m.unit} · ${esc(m.pricing)}${m.costs_unverified ? " · costs unverified" : ""}</small></h2>
        <span class="verdict ${verdict[0]}">${verdict[1]}</span></div>
      <div class="tiles">
        ${tile("Net P&L", rup(m.net_pnl), "after all charges", cls(m.net_pnl))}
        ${tile("Trades", fmt(m.trades, 0), `${m.long_trades || 0} long / ${m.short_trades || 0} short`)}
        ${tile("Win rate", pct(m.win_rate), `payoff ${fmt(m.payoff)}`)}
        ${tile("Profit factor", fmt(m.profit_factor), `expectancy ${fmt(m.expectancy_r, 3)}R`)}
        ${tile("Expectancy", rup(m.expectancy), "per trade", cls(m.expectancy))}
        ${tile("Max drawdown", rup(m.max_drawdown_inr), pct(m.max_drawdown_pct) + " of ref capital", "neg")}
        ${tile("Sharpe", fmt(m.sharpe), m.sharpe_note || `sortino ${fmt(m.sortino)}`)}
        ${tile("Costs", rup(m.costs), `${pct(m.cost_share_pct, 0)} of gross moves`)}
        ${tile("Longest losing run", fmt(m.max_consec_losses, 0), `avg hold ${fmt(m.avg_bars_held, 1)} bars`)}
        ${tile("Setups → signals", `${(m.setups_long || 0) + (m.setups_short || 0)} → ${(m.signals_long || 0) + (m.signals_short || 0)}`, `${m.candidates || 0} candidates filled`)}
      </div>
      ${tp.payoff !== undefined ? `<div class="notice"><b>30% rule</b> · payoff ${fmt(tp.payoff)} (need ≥ ${fmt(tp.payoff_needed_for_30pct)}) · break-even win rate ${pct(tp.breakeven_win_rate)} ·
        PF at a 30% win rate <b class="${tp.pf_at_30pct_win_rate >= 1.1 ? "pos" : "neg"}">${fmt(tp.pf_at_30pct_win_rate)}</b> — ${esc(tp.verdict)}</div>` : ""}
      <div id="plot-eq" class="plot"></div>
      <div id="plot-px" class="plot tall"></div>
      <div id="plot-month" class="plot" style="height:220px"></div>
    </div>
    <div class="grid2">
      <div class="card"><h2>By exit reason</h2><div class="scroll">${bdTable(a.breakdowns && a.breakdowns.exit_reason, "exit_reason")}</div>
        <h2 class="mt">By side</h2><div class="scroll">${bdTable(a.breakdowns && a.breakdowns.side, "side")}</div></div>
      <div class="card"><h2>By hour</h2><div class="scroll">${bdTable(a.breakdowns && a.breakdowns.hour, "hour")}</div>
        <h2 class="mt">By weekday</h2><div class="scroll">${bdTable(a.breakdowns && a.breakdowns.weekday, "weekday")}</div></div>
    </div>
    <div class="card"><h2>Trades <small>${(r.trades || []).length}</small></h2><div class="scroll" style="max-height:420px;overflow:auto">${tradesTable(r.trades || [])}</div></div>
    <div class="card"><details><summary class="muted">Configuration</summary><pre class="code">${esc(JSON.stringify(cfg, null, 2))}</pre></details></div>`;
  drawEquity("plot-eq", a.equity || [], cfg.reference_capital);
  drawPrice("plot-px", a.chart || {});
  drawMonthly("plot-month", a.monthly || []);
}
function bdTable(rows, key) {
  return table([{ l: key, k: key }, { l: "Trades", n: 1, f: r => fmt(r.trades, 0) }, { l: "Win %", n: 1, f: r => pct(r.win_rate) },
    { l: "Net ₹", n: 1, f: r => rup(r.net_pnl), c: r => cls(r.net_pnl) }, { l: "Avg R", n: 1, f: r => fmt(r.avg_r, 3) }, { l: "PF", n: 1, f: r => fmt(r.profit_factor) }], rows || [], { empty: "–" });
}
function tradesTable(tr) {
  return table([
    { l: "Entry", f: r => tsf(r.entry_ts) }, { l: "Side", k: "side" },
    { l: "Detail", f: r => { const d = r.detail || {}; return d.opt_type ? `${esc(d.opt_type)} ${d.strike} Δ${fmt(d.delta_entry, 2)} IV ${fmt(d.iv_entry * 100, 0)}` : (d.qty ? `qty ${d.qty}` : ""); } },
    { l: "In", n: 1, f: r => fmt(r.entry_price) }, { l: "Out", n: 1, f: r => fmt(r.exit_price) }, { l: "Exit", f: r => tsf(r.exit_ts) },
    { l: "Reason", k: "exit_reason" }, { l: "Gross", n: 1, f: r => rup(r.gross_pnl) }, { l: "Costs", n: 1, f: r => rup(r.costs) },
    { l: "Net", n: 1, f: r => rup(r.net_pnl), c: r => cls(r.net_pnl) }, { l: "R", n: 1, f: r => fmt(r.r_multiple, 2), c: r => cls(r.r_multiple) },
    { l: "MFE/MAE R", n: 1, f: r => `${fmt(r.mfe_r, 1)} / ${fmt(r.mae_r, 1)}` }, { l: "Bars", n: 1, k: "bars_held" },
  ], tr, { empty: "No trades." });
}
function drawEquity(id, eq, cap) {
  const el = $(id); if (!plotReady()) { el.innerHTML = `<div class="muted" style="padding:12px">Charts need plotly.js (CDN). Offline?</div>`; return; }
  if (!eq.length) { el.innerHTML = `<div class="muted" style="padding:12px">No equity (no trades).</div>`; return; }
  const x = eq.map(e => e[0]), y = eq.map(e => e[1] - cap); let peak = -Infinity; const dd = y.map(v => { peak = Math.max(peak, v); return v - peak; });
  Plotly.newPlot(el, [
    { x, y, type: "scatter", mode: "lines", name: "P&L", line: { color: cssVar("--cyan"), width: 2 }, fill: "tozeroy", fillcolor: "rgba(34,211,238,.10)" },
    { x, y: dd, type: "scatter", mode: "lines", name: "Drawdown", line: { color: cssVar("--bad"), width: 1 }, fill: "tozeroy", fillcolor: "rgba(255,83,112,.15)", yaxis: "y2" },
  ], { ...PLOT_LAYOUT(), title: { text: "Cumulative net P&L (₹) and drawdown", font: { size: 13 } }, yaxis: { title: "₹", gridcolor: cssVar("--grid"), domain: [0.35, 1] }, yaxis2: { title: "DD ₹", gridcolor: cssVar("--grid"), domain: [0, 0.3] } }, { responsive: true, displaylogo: false });
}
function drawPrice(id, ch) {
  const el = $(id); if (!plotReady()) { el.innerHTML = ""; return; }
  const c = ch.candles || []; if (!c.length) { el.innerHTML = `<div class="muted" style="padding:12px">No bars for the chart.</div>`; return; }
  const t = c.map(b => new Date(b[0] * 1000));
  const traces = [{ x: t, open: c.map(b => b[1]), high: c.map(b => b[2]), low: c.map(b => b[3]), close: c.map(b => b[4]), type: "candlestick", name: "underlying",
    increasing: { line: { color: cssVar("--ok") } }, decreasing: { line: { color: cssVar("--bad") } } }];
  const tr = ch.trades || [];
  if (tr.length) {
    traces.push({ x: tr.map(x => new Date(x.entry_ts * 1000)), y: tr.map(x => x.entry), mode: "markers", type: "scatter", name: "entry",
      marker: { symbol: tr.map(x => x.side === "LONG" ? "triangle-up" : "triangle-down"), size: 12, color: tr.map(x => x.side === "LONG" ? cssVar("--cyan") : cssVar("--accent2")), line: { width: 1, color: cssVar("--panel") } },
      text: tr.map(x => `${x.side} in ${fmt(x.entry)} → ${fmt(x.exit)} (${x.reason}) net ₹${fmt(x.net_pnl, 0)} / ${fmt(x.r, 2)}R`), hoverinfo: "text" });
    traces.push({ x: tr.map(x => new Date(x.exit_ts * 1000)), y: tr.map(x => x.exit), mode: "markers", type: "scatter", name: "exit",
      marker: { symbol: "x", size: 10, color: tr.map(x => x.net_pnl > 0 ? cssVar("--ok") : cssVar("--bad")) }, hoverinfo: "skip" });
  }
  Plotly.newPlot(el, traces, { ...PLOT_LAYOUT(), title: { text: "Underlying with entries (▲▼) and exits (×)", font: { size: 13 } },
    xaxis: { gridcolor: cssVar("--grid"), rangeslider: { visible: false }, rangebreaks: [{ bounds: ["sat", "mon"] }, { bounds: [15.5, 9.25], pattern: "hour" }] } }, { responsive: true, displaylogo: false });
}
function drawMonthly(id, m) {
  const el = $(id); if (!plotReady() || !m.length) { el.innerHTML = ""; return; }
  Plotly.newPlot(el, [{ x: m.map(x => x.month), y: m.map(x => x.pnl), type: "bar", marker: { color: m.map(x => x.pnl >= 0 ? cssVar("--ok") : cssVar("--bad")) } }],
    { ...PLOT_LAYOUT(), title: { text: "Monthly net P&L (₹)", font: { size: 13 } } }, { responsive: true, displaylogo: false });
}
function renderOpt(r) {
  const a = r.artifacts || {}, cfg = r.config || {}, el = $("lab-result");
  const best = a.best_params || {};
  const applyBtn = `<button class="btn small" id="apply-best"><span>Apply best params to the form</span></button>`;
  if (r.kind === "optimize") {
    const bm = a.best_metrics || {};
    el.innerHTML = `<div class="card"><h2>#${r.id} · optimizer · ${esc(cfg.strategy)} on ${esc(cfg.instrument_key)} ${esc(cfg.timeframe)}
      <small>robust plateau selection: best of top-5 after jittering neighbours</small></h2>
      <div class="tiles">${tile("Robust score", fmt(a.best_score, 3), "SQN-style")}${tile("Trades", fmt(bm.trades, 0))}${tile("Win %", pct(bm.win_rate))}
        ${tile("PF", fmt(bm.profit_factor))}${tile("Exp R", fmt(bm.expectancy_r, 3))}${tile("Net ₹", rup(bm.net_pnl), "", cls(bm.net_pnl))}</div>
      <div class="notice"><b>Best parameters</b> <code>${esc(JSON.stringify(best))}</code> ${applyBtn}
        <div class="hint">In-sample numbers are optimistic by construction. Confirm with a walk-forward before believing any of it.</div></div>
      <h3>Trials</h3><div class="scroll" style="max-height:380px;overflow:auto">${table(
        [...Object.keys(best).map(k => ({ l: k, n: 1, f: x => esc(String(x["p_" + k] ?? "")) })),
          { l: "score", n: 1, f: x => fmt(x.score, 3) }, { l: "trades", n: 1, f: x => fmt(x.trades, 0) }, { l: "win%", n: 1, f: x => pct(x.win_rate) },
          { l: "PF", n: 1, f: x => fmt(x.profit_factor) }, { l: "expR", n: 1, f: x => fmt(x.expectancy_r, 3) }, { l: "net ₹", n: 1, f: x => rup(x.net_pnl), c: x => cls(x.net_pnl) }],
        a.trials || [])}</div></div>`;
  } else {
    const o = a.oos_metrics || {}, d = a.default_oos_metrics || {};
    const v = (o.trades || 0) >= 30 && o.profit_factor >= 1.2 && o.expectancy_r >= 0.05 ? ["good", "PASS-LIKE OOS"] : ((o.trades || 0) >= 15 && o.expectancy_r > 0 && o.profit_factor > 1 ? ["meh", "WATCH"] : ["bad", "FAIL"]);
    el.innerHTML = `<div class="card"><div class="row between"><h2>#${r.id} · walk-forward · ${esc(cfg.strategy)} on ${esc(cfg.instrument_key)} ${esc(cfg.timeframe)}
      <small>only the out-of-sample folds count</small></h2><span class="verdict ${v[0]}">${v[1]}</span></div>
      <div class="tiles">${tile("OOS trades", fmt(o.trades, 0))}${tile("OOS win %", pct(o.win_rate))}${tile("OOS PF", fmt(o.profit_factor))}
        ${tile("OOS exp R", fmt(o.expectancy_r, 3))}${tile("OOS net ₹", rup(o.net_pnl), "", cls(o.net_pnl))}${tile("OOS max DD", rup(o.max_drawdown_inr), "", "neg")}
        ${tile("Default-params OOS PF", fmt(d.profit_factor), "same windows, untuned")}</div>
      <div class="notice"><b>Last fold's parameters</b> <code>${esc(JSON.stringify(best))}</code> ${applyBtn}</div>
      <h3>Folds</h3><div class="scroll">${table([{ l: "#", k: "fold", n: 1 }, { l: "In-sample", k: "is" }, { l: "OOS", k: "oos" }, { l: "IS PF", n: 1, f: x => fmt(x.is_pf) },
        { l: "OOS trades", n: 1, k: "oos_trades" }, { l: "OOS PF", n: 1, f: x => fmt(x.oos_pf) }, { l: "OOS exp R", n: 1, f: x => fmt(x.oos_exp_r, 3), c: x => cls(x.oos_exp_r) },
        { l: "Default OOS PF", n: 1, f: x => fmt(x.default_oos_pf) }], a.folds || [])}</div></div>`;
  }
  const b = $("apply-best"); if (b) b.onclick = () => { store.set("lab.params." + cfg.strategy, best); $("lab-strategy").value = cfg.strategy; onStrategyChange(false); toast("Parameters applied", "ok"); location.hash = "#lab"; window.scrollTo(0, 0); };
}
tabLoaders.lab = () => loadLab().catch(e => console.error(e));

/* ═══════════════════════════════ DATA ═══════════════════════════════ */
let dataPoll = null;
async function loadData() {
  const [cov, jobs] = await Promise.all([api("/api/lab/coverage"), api("/api/data/jobs")]);
  $("data-coverage").innerHTML = table([{ l: "Series", k: "series_key" }, { l: "Res", k: "resolution" }, { l: "Source", k: "source" },
    { l: "First", k: "first" }, { l: "Last", k: "last" }, { l: "Bars", n: 1, f: r => Number(r.bars).toLocaleString("en-IN") }], cov.coverage, { empty: "Empty corpus — start a backfill." });
  const busy = jobs.jobs.some(j => j.status === "RUNNING" || j.status === "QUEUED");
  $("data-jobs").innerHTML = table([{ l: "#", k: "id", n: 1 }, { l: "Kind", k: "kind" },
    { l: "Status", f: j => j.status === "RUNNING" ? `RUNNING <div class="progress"><i style="width:${Math.round((j.progress || 0) * 100)}%"></i></div>` : esc(j.status), c: j => j.status === "FAILED" ? "neg" : "" },
    { l: "Message", f: j => esc(j.message || "") }, { l: "Started", f: j => tsf(j.created_at) },
    { l: "", f: j => j.status === "RUNNING" ? `<button class="btn small" data-stop="${j.id}"><span>stop</span></button>` : "" }], jobs.jobs, { empty: "No jobs yet." });
  $("data-jobs").querySelectorAll("[data-stop]").forEach(b => b.onclick = () => api(`/api/data/jobs/${b.dataset.stop}/stop`, {}));
  if (busy && !dataPoll) dataPoll = setInterval(loadData, 2500);
  if (!busy && dataPoll) { clearInterval(dataPoll); dataPoll = null; }
}
$("data-run").onclick = async () => {
  const series = $("data-series").value.trim().split(/[\s,]+/).filter(Boolean);
  const res = $("data-res").value.trim().split(/[\s,]+/).filter(Boolean);
  const r = await api("/api/data/backfill", { series: series.length ? series : null, resolutions: res.length ? res : null, provider: $("data-provider").value || null, full: $("data-full").checked });
  toast("Backfill job #" + r.job_id + " started", "ok"); loadData();
};
$("data-quality").onclick = async () => {
  const q = await api("/api/data/quality?resolution=5m"); $("data-quality-card").hidden = false;
  $("data-quality-out").innerHTML = table([{ l: "Series", k: "series" }, { l: "First", k: "first" }, { l: "Last", k: "last" }, { l: "Days", n: 1, k: "days" }, { l: "Bars", n: 1, k: "bars" },
    { l: "Short days", n: 1, k: "short_days" }, { l: "Zero-vol bars", n: 1, f: r => r.zero_volume_bars ?? "n/a" }, { l: "Max gap %", n: 1, f: r => fmt(r.max_gap_pct) }, { l: "Corp. action?", f: r => r.suspect_corporate_action ? "⚠" : "" }], q.rows);
};
tabLoaders.data = () => loadData().catch(() => {});

/* ═══════════════════════════ NEWS & SENTIMENT ═══════════════════════ */
async function loadNews() {
  const [lat, sc, nw] = await Promise.all([api("/api/sentiment/latest"), api("/api/sentiment?limit=100"), api("/api/news?limit=100")]);
  const L = Object.values(lat.latest || {});
  $("sent-board").innerHTML = L.length ? L.map(x => `<div class="tile"><small>${esc(x.scope)} ${esc(x.symbol || "")}</small>
    <b class="${x.score > 0.1 ? "pos" : (x.score < -0.1 ? "neg" : "")}">${Number(x.score) > 0 ? "+" : ""}${fmt(x.score, 2)}</b><i>${esc(x.horizon || "")} · conf ${fmt(x.confidence, 2)} · ${tsf(x.ts)}</i></div>`).join("")
    : `<div class="muted">No sentiment yet. Post one below or from your scorer via the API.</div>`;
  $("sent-list").innerHTML = table([{ l: "Time", f: r => tsf(r.ts) }, { l: "Scope", k: "scope" }, { l: "Symbol", k: "symbol" },
    { l: "Score", n: 1, f: r => fmt(r.score, 2), c: r => cls(r.score) }, { l: "Conf", n: 1, f: r => fmt(r.confidence, 2) }, { l: "Horizon", k: "horizon" }, { l: "Source", k: "source" }, { l: "Note", f: r => esc(r.note || "") }], sc.scores, { empty: "–" });
  $("news-list").innerHTML = (nw.items || []).map(n => `<div><span class="lvl ${n.sentiment > 0.1 ? "pos" : (n.sentiment < -0.1 ? "neg" : "muted")}">${n.sentiment == null ? "NEWS" : (n.sentiment > 0 ? "+" : "") + fmt(n.sentiment, 2)}</span>
    <b>${esc(n.headline)}</b> <span class="dim">${tsf(n.ts)} · ${esc(n.source || "")} ${(n.symbols || []).map(esc).join(" ")}</span>
    ${n.url ? ` <a href="${esc(n.url)}" target="_blank" rel="noopener">link</a>` : ""}<br><span class="muted">${esc((n.body || "").slice(0, 240))}</span></div>`).join("") || `<div class="muted">No news yet.</div>`;
  $("news-curl").textContent = `# post a sentiment score (token = ONELOT_API_TOKEN or data/api_token.txt)
curl -X POST ${location.origin}/api/sentiment -H "x-onelot-token: $TOKEN" -H "Content-Type: application/json" \\
  -d '{"scope":"INDEX","symbol":"BANKNIFTY","score":0.35,"confidence":0.7,"horizon":"intraday","source":"my-scorer"}'
# post a news item
curl -X POST ${location.origin}/api/news -H "x-onelot-token: $TOKEN" -H "Content-Type: application/json" \\
  -d '{"headline":"RBI holds rates","source":"wire","symbols":["BANKNIFTY"],"sentiment":0.1}'`;
}
$("sent-add").onclick = async () => {
  await api("/api/sentiment", { scope: $("sent-scope").value, symbol: $("sent-symbol").value || null, score: parseFloat($("sent-score").value), confidence: parseFloat($("sent-conf").value),
    horizon: $("sent-horizon").value, source: $("sent-source").value || "manual", note: $("sent-note").value || null });
  toast("Sentiment posted", "ok"); loadNews();
};
$("news-add").onclick = async () => {
  if (!$("news-headline").value.trim()) { toast("Headline required", "warn"); return; }
  const s = $("news-sent").value;
  await api("/api/news", { headline: $("news-headline").value, body: $("news-body").value || null, source: $("news-source").value || null, url: $("news-url").value || null,
    symbols: $("news-symbols").value.trim().split(/[\s,]+/).filter(Boolean), sentiment: s === "" ? null : parseFloat(s) });
  $("news-headline").value = ""; $("news-body").value = ""; toast("News added", "ok"); loadNews();
};
tabLoaders.news = () => loadNews().catch(() => {});

/* ═════════════════════════════ SETTINGS ═════════════════════════════ */
async function loadSettings() {
  const s = await api("/api/settings"); const c = s.credentials || {};
  $("env-path").textContent = s.env_path; $("postback-hint").textContent = s.postback_url_hint;
  $("cred-status").innerHTML = kv("Client ID", c.client_id || "<span class='warn'>not set</span>") + kv("Access token", c.access_token || "<span class='warn'>not set</span>") +
    kv("Data API token", c.data_access_token || "<span class='dim'>not set (optional)</span>") +
    kv("Account reads", s.account_adapter ? "<span class='pos'>attached</span>" : "<span class='warn'>no adapter</span>") +
    kv("Market feed", esc(s.feed_broker || "–") + (s.feed_broker === "mock" ? " <span class='dim'>(restart with credentials for live data)</span>" : ""));
  const block = (t, o) => `<h3>${t}</h3><pre class="code">${esc(JSON.stringify(o, null, 2))}</pre>`;
  $("cfg-view").innerHTML = kv("Mode", esc(s.mode)) + kv("Bind", `${esc(s.api.host)}:${s.api.port}`) +
    block("Live direction source", s.live) + block("Risk (absolute rupees, one lot)", s.risk) + block("Backtest defaults", s.backtest) + block("Data corpus", s.data);
}
$("cred-save").onclick = async () => {
  const body = {}; if ($("cred-client").value.trim()) body.client_id = $("cred-client").value.trim();
  if ($("cred-token").value.trim()) body.access_token = $("cred-token").value.trim();
  if ($("cred-data-token").value.trim()) body.data_access_token = $("cred-data-token").value.trim();
  if (!Object.keys(body).length) { toast("Nothing to save", "warn"); return; }
  const r = await api("/api/settings/dhan", body);
  $("cred-client").value = ""; $("cred-token").value = ""; $("cred-data-token").value = "";
  toast(r.ok ? "Credentials saved and attached" : "Saved, but adapter not attached: " + (r.reason || ""), r.ok ? "ok" : "warn");
  if (r.restart_needed_for_live_data) toast("Restart the engine (without --mock) for live market data", "warn");
  acctLoaded = false; loadSettings();
};
$("cred-test").onclick = async () => {
  $("cred-test-out").innerHTML = `<div class="muted">testing…</div>`;
  const r = await api("/api/settings/dhan/test", {});
  const rows = Object.entries(r.checks || {}).map(([k, v]) => ({ name: k, ...v }));
  $("cred-test-out").innerHTML = `<div class="row"><span class="badge ${r.trading_ok ? "ok" : "bad"}"><span>trading API ${r.trading_ok ? "OK" : "FAIL"}</span></span>
    <span class="badge ${r.data_ok ? "ok" : "warn"}"><span>data API ${r.data_ok ? "OK" : "not available"}</span></span></div>` +
    (r.profile ? kv("Token valid till", esc(r.profile.token_validity || "–")) + kv("Data plan", esc(r.profile.data_plan || "–")) + kv("Data valid till", esc(r.profile.data_validity || "–")) : "") +
    table([{ l: "Check", k: "name" }, { l: "Tier", k: "tier" }, { l: "Result", f: x => x.ok ? "<span class='pos'>OK</span>" : `<span class='neg'>${esc(x.error)}</span>` }, { l: "Detail", f: x => esc((x.detail || "").slice(0, 120)) }], rows) +
    ((r.notes || []).length ? `<ul class="small-list muted">${[...new Set(r.notes)].map(n => `<li>${esc(n)}</li>`).join("")}</ul>` : "");
};
$("notify-test").onclick = async () => { const r = await api("/api/notify/test", {}); toast("Notification test: " + JSON.stringify(r), "ok"); };
tabLoaders.settings = () => loadSettings().catch(() => {});

/* ── boot ─────────────────────────────────────────────────────────────── */
connect();
showTab(location.hash.slice(1) || store.get("tab", "home"));
})();
