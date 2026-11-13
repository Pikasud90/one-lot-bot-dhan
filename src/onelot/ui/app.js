/* One Lot Desk — thin client over the engine API. No framework, no build step, works offline.
   Tabs: home (account, cached) · live (multi-instrument desk + legacy Bank Nifty engine, SSE)
         lab (backtests, leaderboard, sweeps) · data (corpus + EDA) · news (pulse, scores) · settings */
(() => {
"use strict";
let TOKEN = window.ONELOT_TOKEN || "";
const $ = (id) => document.getElementById(id);
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(Number(v))) ? "–" : Number(v).toFixed(d);
const rup = (v) => (v === null || v === undefined || Number.isNaN(Number(v))) ? "–"
  : (Number(v) < 0 ? "−" : "") + "₹" + Math.abs(Number(v)).toLocaleString("en-IN", { maximumFractionDigits: 0 });
const rup2 = (v) => (v === null || v === undefined || Number.isNaN(Number(v))) ? "–"
  : (Number(v) < 0 ? "−" : "") + "₹" + Math.abs(Number(v)).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const cls = (v) => v > 0 ? "pos" : (v < 0 ? "neg" : "");
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[m]));
const kv = (k, v, c = "") => `<div class="kv"><span>${esc(k)}</span><span class="${c}">${v}</span></div>`;
const pct = (v, d = 1) => (v === null || v === undefined || Number.isNaN(Number(v))) ? "–" : Number(v).toFixed(d) + "%";
const spct = (v, d = 2) => (v === null || v === undefined || Number.isNaN(Number(v))) ? "–" : (Number(v) > 0 ? "+" : "") + Number(v).toFixed(d) + "%";
const tsf = (s) => s ? String(s).replace("T", " ").slice(0, 16) : "–";
const hhmm = (s) => s ? String(s).slice(11, 16) : "–";
const hms = (s) => s ? String(s).slice(11, 19) : "–";
const tile = (l, v, i = "", c = "") => `<div class="tile"><small>${l}</small><b class="${c}">${v}</b><i>${i}</i></div>`;
// A rupee STRING from the API ("-12.05", exact from integer paise) for display: "−₹12.05", Indian digit grouping, never through a float. Plain text: esc() it in HTML.
const inr = (s) => (s === null || s === undefined || s === "") ? "–" : (String(s).startsWith("-") ? "−₹" : "₹") + String(s).replace(/^-/, "").replace(/^\d+/, (w) => w.replace(/(\d)(?=(\d\d)*\d{3}$)/g, "$1,"));
// The alert confidence as every surface shows it (notify.base.shown_confidence, US-003): floored and clamped to 0..100, so 64.9 shows 64; null when not a finite number.
const confShown = (v) => (typeof v !== "number" || !Number.isFinite(v)) ? null : Math.min(100, Math.max(0, Math.floor(v)));
const store = { get(k, d) { try { return JSON.parse(localStorage.getItem("onelot." + k)) ?? d; } catch (e) { return d; } },
                set(k, v) { try { localStorage.setItem("onelot." + k, JSON.stringify(v)); } catch (e) {} } };
const cssVar = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const C = () => window.Charts;

function toast(msg, kind = "") {
  const d = document.createElement("div"); d.className = "toast " + kind; d.textContent = msg;
  $("toasts").appendChild(d); setTimeout(() => d.remove(), 6000);
}
async function api(url, body, method, _retry) {
  const opt = { method: method || (body ? "POST" : "GET"), headers: { "x-onelot-token": TOKEN } };
  if (body) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(url, opt);
  const j = await r.json().catch(() => ({}));
  if (r.status === 401 && !_retry) {
    // The engine restarted or its session token changed: re-sync it once and retry.
    try { const t = await fetch("/api/session").then(x => x.json()); if (t.token && t.token !== TOKEN) { TOKEN = t.token; return api(url, body, method, true); } } catch (e) {}
  }
  if (!r.ok) { toast((j && (j.detail || j.error)) || `${r.status} ${url}`, "bad"); throw new Error(j.detail || r.status); }
  return j;
}
function table(cols, rows, opts = {}) {
  if (!rows || !rows.length) return `<div class="muted">${esc(opts.empty || "Nothing here.")}</div>`;
  const head = cols.map(c => `<th class="${c.n ? "n" : ""}">${esc(c.l)}</th>`).join("");
  const body = rows.map((r, i) => `<tr class="${opts.rowClass ? opts.rowClass(r) : ""}" data-i="${i}">` +
    cols.map(c => `<td class="${c.n ? "n" : ""} ${c.c ? c.c(r) : ""}">${c.f ? c.f(r) : esc(r[c.k])}</td>`).join("") + "</tr>").join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

/* ── theme ────────────────────────────────────────────────────────────── */
// Five themes, one block of CSS variables each; an unknown stored value falls back to Day, a stored "night" stays Night.
const THEMES = ["day", "night", "laxmi", "ramayana", "batman"];
const themeOf = (t) => THEMES.includes(t) ? t : "day";
function applyTheme(t) {
  t = themeOf(t);
  document.documentElement.dataset.theme = t; store.set("theme", t);
  $("theme-toggle").value = t;
  if (C()) C().retheme();
}
$("theme-toggle").onchange = (e) => applyTheme(e.target.value);
applyTheme(store.get("theme", "day"));

/* ── IST clock (system time, never the network) + exchange calendar ──── */
const IST_FMT = new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit", weekday: "short", day: "2-digit", month: "short", year: "numeric" });
let calData = null, calMonth = null;
function tickClock() {
  const now = new Date();
  const parts = Object.fromEntries(IST_FMT.formatToParts(now).map(p => [p.type, p.value]));
  const ms = String(now.getMilliseconds()).padStart(3, "0");
  $("clock").innerHTML = `${parts.hour}:${parts.minute}:${parts.second}<small>.${ms}</small>`;
  $("clock-date").textContent = `${parts.weekday} ${parts.day} ${parts.month} ${parts.year} IST`;
  const mins = parseInt(parts.hour, 10) * 60 + parseInt(parts.minute, 10);
  let s = "";
  if (calData && !calData.today_is_trading_day) s = calData.holidays.find(h => h.today) ? "holiday · " + calData.holidays.find(h => h.today).name : "weekend";
  else if (mins < 555) s = `pre-open · opens in ${Math.floor((555 - mins) / 60)}h ${(555 - mins) % 60}m`;
  else if (mins < 930) s = `market open · ${Math.floor((930 - mins) / 60)}h ${(930 - mins) % 60}m to close`;
  else s = "market closed";
  $("clock-session").textContent = s;
  requestAnimationFrame(tickClock);
}
requestAnimationFrame(tickClock);
async function loadCalendar(year) {
  calData = await api("/api/calendar" + (year ? `?year=${year}` : ""));
  return calData;
}
function renderCalendar() {
  if (!calData) return;
  const hol = new Map(calData.holidays.map(h => [h.date, h]));
  const wk = new Map(calData.weekend_holidays.map(h => [h.date, h]));
  const today = calData.today;
  const y = calMonth.getFullYear(), m = calMonth.getMonth();
  const first = new Date(y, m, 1), days = new Date(y, m + 1, 0).getDate();
  const cells = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map(d => `<div class="dow">${d}</div>`);
  for (let i = 0; i < (first.getDay() + 6) % 7; i++) cells.push(`<div class="d empty"></div>`);
  for (let d = 1; d <= days; d++) {
    const iso = `${y}-${String(m + 1).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
    const dow = new Date(y, m, d).getDay();
    const h = hol.get(iso), w = wk.get(iso);
    const cls = ["d"];
    if (dow === 0 || dow === 6) cls.push("wk");
    if (h) cls.push("hol", iso < today ? "past" : "next"); else if (iso < today && !(dow === 0 || dow === 6)) cls.push("past");
    if (iso === today) cls.push("today");
    cells.push(`<div class="${cls.join(" ")}" title="${esc((h || w || {}).name || "")}">${d}</div>`);
  }
  $("cal-grid").innerHTML = `<div class="cal-nav" style="grid-column:1/-1"><button class="btn small ghost" id="cal-prev">‹</button><b>${calMonth.toLocaleString("en-IN", { month: "long", year: "numeric" })}</b><button class="btn small ghost" id="cal-next">›</button></div>` + cells.join("");
  $("cal-prev").onclick = () => { calMonth = new Date(y, m - 1, 1); renderCalendar(); };
  $("cal-next").onclick = () => { calMonth = new Date(y, m + 1, 1); renderCalendar(); };
  $("cal-meta").textContent = `${calData.year} · ${calData.holidays.length} weekday holidays · ${calData.verified ? "verified " + (calData.verified_on || "") : "UNVERIFIED"} · session ${calData.session.open}–${calData.session.close}`;
  const nx = calData.next_holiday;
  $("cal-list").innerHTML = (nx ? `<div class="notice"><b>Next holiday:</b> ${esc(nx.name)} — ${nx.weekday} ${nx.date} (${nx.days_away} day${nx.days_away === 1 ? "" : "s"} away)</div>` : "") +
    table([{ l: "Date", f: r => `${r.date} <span class="dim">${r.weekday}</span>` }, { l: "Holiday", k: "name" },
      { l: "", f: r => r.today ? "<b class='warn'>today</b>" : (r.past ? "<span class='dim'>done</span>" : `<span class='pos'>in ${r.days_away} d</span>`) }],
      calData.holidays, { rowClass: r => r.past ? "dim" : "" }) +
    (calData.special_sessions.length ? `<div class="hint mt">Special sessions: ${calData.special_sessions.map(x => `${esc(x.name)} ${esc(x.date)}`).join(" · ")}</div>` : "");
}
$("clockbox").onclick = async () => {
  const pop = $("calpop");
  if (!pop.hidden) { pop.hidden = true; return; }
  if (!calData) await loadCalendar().catch(() => {});
  calMonth = calMonth || new Date();
  renderCalendar(); pop.hidden = false;
};
$("cal-close").onclick = () => { $("calpop").hidden = true; };
loadCalendar().catch(() => {});

/* ── tabs ─────────────────────────────────────────────────────────────── */
const tabLoaders = {};
function showTab(name) {
  document.querySelectorAll(".tab").forEach(s => s.classList.toggle("active", s.dataset.tab === name));
  document.querySelectorAll("#tabs a").forEach(a => a.classList.toggle("active", a.dataset.tab === name));
  store.set("tab", name);
  if (tabLoaders[name]) tabLoaders[name]();
}
window.addEventListener("hashchange", () => showTab(location.hash.slice(1) || "home"));
// Number keys 1 to 7 switch tabs, in the bar's order; never while typing in a field or with a modifier held.
const tabForKey = (key, tag, editable, mods, tabs) => (mods || editable || /^(INPUT|TEXTAREA|SELECT)$/.test(String(tag || "").toUpperCase()) || !/^[1-9]$/.test(String(key))) ? null : (tabs[Number(key) - 1] ?? null);
document.addEventListener("keydown", (e) => {
  const t = e.target || {}, tabs = [...document.querySelectorAll("#tabs a")].map(a => a.dataset.tab);
  const name = tabForKey(e.key, t.tagName, !!t.isContentEditable, e.ctrlKey || e.metaKey || e.altKey, tabs);
  if (!name) return;
  e.preventDefault(); if (location.hash.slice(1) !== name) location.hash = name; else showTab(name);
});

/* ── header badges + warnings ─────────────────────────────────────────── */
let latest = null, acctSummary = null;
function tokenBadge() {
  const tv = acctSummary && acctSummary.token_validity;        // "28/10/2026 20:00" IST
  if (!tv) return "";
  const m = String(tv).match(/(\d{2})\/(\d{2})\/(\d{4})\s+(\d{2}):(\d{2})/);
  if (!m) return "";
  const exp = new Date(`${m[3]}-${m[2]}-${m[1]}T${m[4]}:${m[5]}:00+05:30`);
  const left = (exp - Date.now()) / 3600000;
  if (left <= 0) return bd("token EXPIRED", "bad");
  return bd(`token ${left < 1 ? Math.round(left * 60) + "m" : Math.round(left) + "h"} left`, left < 2 ? "warn" : "ok");
}
const bd = (t, c = "") => `<span class="badge ${c}"><span>${esc(t)}</span></span>`;
// US-028: name the instruments on delayed (Yahoo) bars, first 3 keys then "+N". Plain text on purpose: bd() runs esc() on it once and textContent needs none, so escaping here would double-escape (M&M).
const delayNote = (s) => { const k = (s && s.delayed_keys) || []; return k.length ? `delayed ~15 min: ${k.slice(0, 3).join(", ")}${k.length > 3 ? ` +${k.length - 3}` : ""}` : (s && s.delayed ? "delayed ~15 min" : "live"); };
function renderBadges(s) {
  const st = s.status || {}, h = s.health || {}, risk = s.risk || {}, d = s.desk || {};
  const b = [];
  b.push(bd(st.mode || "?", st.mode === "LIVE" ? "bad" : "ok"));
  b.push(bd("feed " + (st.broker || "?"), st.broker === "mock" ? "warn" : "ok"));
  if (d.source) b.push(bd(`desk ${d.source.name} ${delayNote(d.source)}`, d.source.delayed ? "warn" : "ok"));
  const hs = h.state || "?";
  b.push(bd(hs, hs === "HEALTHY" ? "ok" : (hs === "DEAD" ? "bad" : "warn")));
  b.push(bd(risk.state || "?", risk.state === "HALTED" ? "bad" : "ok"));
  if (st.session && !st.session.entries_allowed) b.push(bd("entries closed", "warn"));
  b.push(tokenBadge());
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
  const snap = refresh ? await api("/api/account/refresh", {}).then(r => r.snapshot) : await api("/api/account");
  renderAccount(snap); acctLoaded = true;
}
function renderAccount(a) {
  const s = a.summary || {}, p = a.panels || {};
  acctSummary = s;
  if (latest) renderBadges(latest);
  renderToday();
  $("acct-age").textContent = a.fetched_at ? ("data as of " + tsf(a.fetched_at) + (a.stale ? " · stale" : "")) : "never fetched";
  if (!a.configured) {
    $("acct-notice").innerHTML = `<div class="notice"><svg class="mascot sm"><use href="#m-ichi"/></svg> No Dhan credentials yet. Add your client ID and a free-tier access token under
      <a href="#settings">Settings</a>; the ledger, funds, holdings and positions work without the paid Data API.</div>`;
  } else if (a.last_error) {
    const why = a.token_expired ? "The access token has expired — generate a new one on web.dhan.co and paste it in <a href='#settings'>Settings</a>."
      : (a.offline ? "Dhan could not be reached (offline?)." : "The last refresh failed.");
    $("acct-notice").innerHTML = `<div class="warnbar"><b>Showing the last good data${a.fetched_at ? " from " + tsf(a.fetched_at) : ""}.</b> ${why}
      <div class="hint">${esc(a.last_error.panel)} · ${tsf(a.last_error.at)} · ${esc(String(a.last_error.message).slice(0, 160))}</div></div>`;
  } else $("acct-notice").innerHTML = "";
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
const journal = { rows: [], shown: 10, edit: {} };
async function renderHomeJournal() {
  try {
    const j = await api("/api/desk/journal?limit=500");
    journal.rows = j.trades || [];
    drawJournal();
  } catch (e) {}
}
function drawJournal() {
  const t = journal.rows, el = $("home-journal");
  if (!t.length) { el.innerHTML = `<div class="empty-inline"><svg class="mascot sm"><use href="#m-cow-sleep"/></svg> No closed desk trades yet. A trade you take closes by itself at its exit alert's price.</div>`; return; }
  const sum = (k) => t.reduce((a, x) => a + (Number(x[k]) || 0), 0);
  const net = sum("net_pnl"), wins = t.filter(x => x.net_pnl > 0).length, mine = t.filter(x => x.user_net_pnl != null);
  const diff = mine.reduce((a, x) => a + (Number(x.diff_pnl) || 0), 0);
  const rows = t.slice(0, journal.shown), left = t.length - rows.length;
  const yours = (r, k) => r[k] != null ? fmt(r[k]) : `<span class="dim">–</span>`;
  el.innerHTML = `<div class="tiles small">` +
      `<div class="tile"><small>Trades</small><b>${t.length}</b></div>` +
      `<div class="tile"><small>Win rate</small><b>${pct(wins / t.length * 100)}</b></div>` +
      `<div class="tile"><small>Net P&amp;L · alert prices</small><b class="${cls(net)}">${rup(net)}</b></div>` +
      `<div class="tile"><small>Costs</small><b class="neg">${rup(sum("costs"))}</b></div>` +
      `<div class="tile"><small>Yours vs the alerts' (${mine.length} reported)</small><b class="${cls(diff)}">${mine.length ? rup(diff) : "–"}</b></div></div>` +
    `<div class="scroll mt"><table><thead><tr><th>Alert at</th><th>Closed</th><th>Instrument</th><th>Side</th><th>Strategy</th>` +
      `<th class="n">Alert entry</th><th class="n">Alert exit</th><th class="n">Net · alert</th><th class="n">Your entry</th><th class="n">Your exit</th><th class="n">Your net</th><th class="n">Difference</th><th>Why</th><th></th></tr></thead><tbody>` +
    rows.map(r => `<tr><td>${tsf(r.alert_created_at)}</td><td>${tsf(r.closed_at)}</td><td><b>${esc(r.label || r.instrument_key)}</b></td>` +
      `<td>${esc(String(r.side || "").replace(/_/g, " "))}</td><td>${esc(r.strategy)} <span class="dim">${esc(r.timeframe)}</span></td>` +
      `<td class="n">${fmt(r.alert_entry_price)}</td><td class="n">${r.alert_exit_price != null ? fmt(r.alert_exit_price) : `<span class="dim" title="closed by you before its exit alert">pending</span>`}</td>` +
      `<td class="n ${cls(r.net_pnl)}">${rup(r.net_pnl)}</td><td class="n">${yours(r, "user_entry_price")}</td><td class="n">${yours(r, "user_exit_price")}</td>` +
      `<td class="n ${cls(r.user_net_pnl)}">${r.user_net_pnl != null ? rup(r.user_net_pnl) : "–"}</td><td class="n ${cls(r.diff_pnl)}">${r.diff_pnl != null ? rup(r.diff_pnl) : "–"}</td>` +
      `<td>${esc(String(r.exit_reason || "").replace(/_/g, " "))}${r.exit_source === "user" ? ` <span class="dim">(you)</span>` : ""}</td>` +
      `<td><button class="btn small ghost" data-jedit="${esc(r.id)}">${journal.edit[r.id] ? "cancel" : "my prices"}</button></td></tr>` +
      (journal.edit[r.id] ? `<tr class="jedit"><td colspan="14"><label class="inline">Your entry <input type="number" step="0.05" id="je_${esc(r.id)}" value="${r.user_entry_price ?? ""}" style="width:120px"></label>
        <label class="inline">Your exit <input type="number" step="0.05" id="jx_${esc(r.id)}" value="${r.user_exit_price ?? ""}" style="width:120px"></label>
        <button class="btn small" data-jsave="${esc(r.id)}">Save my prices</button> <span class="dim">the alert's prices stay the record; yours show the difference</span></td></tr>` : "")).join("") +
    `</tbody></table></div>` +
    (left > 0 ? `<div class="actions mt"><button class="btn small" id="journal-more">Load 10 more</button><span class="muted">${rows.length} of ${t.length} shown</span></div>` : "");
  el.querySelectorAll("[data-jedit]").forEach(b => b.onclick = () => { const k = b.dataset.jedit; journal.edit[k] = !journal.edit[k]; drawJournal(); });
  el.querySelectorAll("[data-jsave]").forEach(b => b.onclick = async () => {
    const k = b.dataset.jsave, en = $("je_" + k).value, ex = $("jx_" + k).value;
    if (!en && !ex) { toast("Type your entry and/or exit price first.", "warn"); return; }
    const r = await api(`/api/desk/position/${k}/report`, { entry_price: en ? Number(en) : null, exit_price: ex ? Number(ex) : null });
    toast(r.ok ? `Saved · difference ${r.diff_pnl != null ? rup(r.diff_pnl) : "–"}` : (r.message || "not saved"), r.ok ? "ok" : "warn");
    if (r.ok) { journal.edit[k] = false; renderHomeJournal(); }
  });
  const more = $("journal-more"); if (more) more.onclick = () => { journal.shown += 10; drawJournal(); };
}
/* ── Home: alerts (pending cards, monitoring, archive with audited outcomes) ── */
const alerts = { open: store.get("alerts.open", {}), rows: [], rest: [], scoreboard: {}, paper: null, primary: [], shown: 10, loadedAt: 0 };
const srcTag = (d) => d ? `${esc(d.source)} ${esc(d.resolution)}${d.delayed ? " · <span class='warn'>~15 min delayed</span>" : " · live"}` : "–";
function confBadge(c) { return c ? `<div class="conf ${c.label}" title="${esc(JSON.stringify(c.parts))}">${fmt(confShown(c.score), 0)} ${esc(c.label)}</div>` : `<div class="conf medium">–</div>`; }
// OF-5: every alert's one exit is its paper trade's close (s.exit); the audit is a fallback
// for alerts from before the paper book.
function exitCell(x) {
  if (x.status === "NO_FILL") return `<span class="outc NO_FILL" title="${esc(x.reason)}">NO FILL</span>`;
  return `<span class="outc ${esc(x.reason)}">EXIT ${esc(String(x.reason).replace(/_/g, " "))}</span> <span class="num">${x.price != null ? rup2(x.price) : "–"}</span>` +
    (x.net != null ? ` <span class="${cls(x.net)}" style="font-family:var(--mono)">${rup(x.net)}</span>` : "");
}
function outcomeCell(s) {
  const o = s.outcome;
  if (s.status === "PENDING") return `<span class="countdown" data-exp="${esc(s.expires_at)}">…</span>`;
  if (s.exit) return exitCell(s.exit) + (s.trade ? ` <span class="dim">· yours ${rup(s.trade.net_pnl)}</span>` : "");
  if (s.status === "TAKEN") return s.trade ? `<span class="outc ${s.trade.net_pnl >= 0 ? "TARGET" : "STOP"}">CLOSED ${rup(s.trade.net_pnl)}</span>` : `<span class="outc OPEN">MONITORING</span>`;
  if (!o || !o.outcome || o.outcome === "NO_DATA") return `<span class="dim">awaiting its exit</span>`;
  return `<span class="outc ${esc(o.outcome)}">${esc(o.outcome.replace("_", " "))}</span> <span class="${cls(o.r_now)}" style="font-family:var(--mono)">${fmt(o.r_now, 2)}R</span> <span class="dim">audit</span>`;
}
function alertRow(s) {
  const e = s.economics || {}, o = s.option || e.option || {}, out = s.outcome || {}, c = s.confidence || {};
  const sell = s.direction === "short";
  const what = s.kind === "INDEX" ? `${esc(s.side.replace("_", " "))} ${esc(s.label)} ${o.strike ? fmt(o.strike, 0) + " " + esc(o.opt_type) : ""}` : `${sell ? "SELL" : "BUY"} ${esc(s.label)}`;
  const parts = c.parts || {};
  const body = `
    <div class="grid3">
      <div><h4>Alert</h4>${kv("Raised", tsf(s.created_at) + " IST")}${kv("Signal bar", hhmm(s.bar_ts))}${kv("Valid until", hhmm(s.expires_at))}${kv("Status", esc(s.status))}
        ${kv("Data", srcTag(s.data))}${kv("Horizon", esc(s.horizon || "intraday"))}
        <h4>Exit alert</h4>${s.exit ? kv("Exit", exitCell(s.exit)) + kv("Exit at", tsf(s.exit.at) + " IST") + kv("Paper trade", "#" + esc(s.exit.trade_id) + " · " + esc(s.exit.grade || "")) : `<div class="muted">not yet: its paper trade is still waiting or open</div>`}</div>
      <div><h4>One-unit economics</h4>${kv("Unit", esc(e.unit_label || ""))}${kv(s.kind === "INDEX" ? "Est. premium" : "Entry", rup2(e.entry_price))}${kv(s.kind === "INDEX" ? "Premium at stop" : "Stop", rup2(e.stop_price), "neg")}
        ${kv(s.kind === "INDEX" ? "Premium at target" : "Target", e.target_price != null ? rup2(e.target_price) : "trail", "pos")}${kv("Risk ₹ (incl. charges)", rup(e.risk_inr), "neg")}${kv("Net reward ₹", e.reward_inr != null ? rup(e.reward_inr) : "–", "pos")}${kv("Net R/R", fmt(e.rr_net))}
        ${s.kind === "INDEX" ? kv("Underlying entry / stop / target", `${fmt(e.underlying_entry, 0)} / ${fmt(e.underlying_stop, 0)} / ${e.underlying_target != null ? fmt(e.underlying_target, 0) : "–"}`) : ""}${e.synthetic ? `<div class="hint warn">premium synthetic (Black-76) — no live chain at alert time</div>` : (o.spread_pct != null ? kv("Chain spread", pct(o.spread_pct)) : "")}</div>
      <div><h4>Confidence ${fmt(confShown(c.score), 0)} <span class="dim">${esc(c.label || "")}</span></h4>
        ${["signal_strength", "backtest_evidence", "live_record", "regime_fit"].map(k => `<div class="kv"><span>${k.replace("_", " ")}</span><span>${fmt(parts[k], 0)}</span></div><div class="conf-bar"><i style="width:${Math.max(0, Math.min(100, parts[k] || 0))}%"></i></div>`).join("")}
        <div class="hint">${esc(parts.backtest_note || "")}<br>${esc(parts.live_note || "")}</div>
        <h4>Audit at the alerted levels</h4>${out.outcome ? kv("Result", outcomeCell(s)) + kv("Best / worst excursion", `${fmt(out.mfe_r, 2)}R / −${fmt(out.mae_r, 2)}R`) + kv("Est. P&L (1 unit)", rup(out.pnl_est), cls(out.pnl_est)) + kv("Resolved", out.exit_ts ? tsf(out.exit_ts) : "–") : `<div class="muted">audited once the next bars arrive</div>`}</div>
    </div>
    <pre>${esc(s.text || "")}</pre>
    ${s.status === "PENDING" ? `<div class="actions"><label class="inline">Your fill (optional, to compare) <input type="number" step="0.05" id="afill_${s.id}" placeholder="${fmt(e.entry_price)}" style="width:130px"></label>
      <button class="btn take" data-take="${s.id}">Trade taken</button><button class="btn" data-skip="${s.id}">Skipped</button></div>` : ""}`;
  return `<div class="alert-row ${s.status === "PENDING" ? "pending" : ""} ${s.status === "TAKEN" ? "taken" : ""} ${sell ? "sell" : ""} ${alerts.open[s.id] ? "open" : ""}" data-aid="${s.id}">
    <div class="alert-head" data-atoggle="${s.id}">
      <div class="when">${hhmm(s.created_at)}<small>${String(s.created_at || "").slice(0, 10)}</small></div>
      <div class="what"><b>${what}</b><span class="dim">${esc(e.unit_label || "")} · ${s.kind === "INDEX" ? "~" + rup(e.outlay) + " outlay" : rup2(e.entry_price)} · ${srcTag(s.data)}</span></div>
      <div class="strat"><b>${esc(s.strategy_label || s.strategy)}</b>${esc(s.timeframe)} · strength ${fmt(s.strength, 0)}</div>
      ${confBadge(c)}
      <div>${outcomeCell(s)}</div>
      <div class="dim" style="font-size:12px">${esc(s.status)}${s.resolved_at ? "<br>" + hhmm(s.resolved_at) : ""}</div>
      <div class="acc-caret">▾</div>
    </div>
    <div class="alert-body">${body}</div></div>`;
}
async function loadAlerts(force) {
  const days = $("alerts-days").value || 7, filt = $("alerts-filter").value;
  const r = await api(`/api/desk/signals?days=${days}`).catch(() => null);
  if (!r) {
    $("home-signals").innerHTML = `<div class="card"><div class="empty-inline"><svg class="mascot sm"><use href="#m-cow-sweat"/></svg> Live Desk not running.</div></div>`;
    if (latest && latest.desk) renderHomePositions(latest.desk);  // the snapshot in hand still lists them
    return;
  }
  alerts.delivery = r.delivery_alerts || [];
  alerts.rows = r.signals || []; alerts.scoreboard = r.scoreboard || {}; alerts.paper = r.paper_scoreboard || null; alerts.primary = r.primary_strategies || [];
  const rows = alerts.rows.filter(s => !filt || s.status === filt || (filt === "TAKEN" && s.status === "CLOSED"));
  const pend = rows.filter(s => s.status === "PENDING");
  alerts.rest = rows.filter(s => s.status !== "PENDING");
  const exited = alerts.rows.filter(s => s.exit).length;
  $("alerts-meta").textContent = `${alerts.rows.length} alerts · ${pend.length} pending · ${alerts.rows.filter(s => s.status === "TAKEN" || s.status === "CLOSED").length} taken · ${exited} exited`;
  $("home-signals").innerHTML = dlvHome(latest && latest.desk ? latest.desk.delivery : null) + (pend.length ? `<h3>Pending — take it or let it expire · its paper trade is already open</h3>` + pend.map(alertRow).join("") : `<div class="card"><div class="empty-inline"><svg class="mascot sm"><use href="#m-cow"/></svg> No pending option alert. The desk is tracking ${latest && latest.desk ? latest.desk.counts.instruments : "the watchlist"} instruments: an option alert comes only when all seven timeframes newly align; the pullback rule trades rarely: weeks can pass.</div></div>`);
  wireAlerts($("home-signals"));
  drawArchive();
  drawScoreboard();
  if (latest && latest.desk) renderHomePositions(latest.desk);
}
// The archive shows the newest 10; "Load 10 more" adds a page without asking the server again.
function drawArchive() {
  const rest = alerts.rest, page = rest.slice(0, alerts.shown), left = rest.length - page.length;
  $("home-archive").innerHTML = dlvArchive(alerts.delivery) + (page.length ? page.map(alertRow).join("") : `<div class="muted">Nothing archived in this window.</div>`);
  wireAlerts($("home-archive"));
  $("home-archive-more").innerHTML = left > 0
    ? `<button class="btn small" id="archive-more">Load ${Math.min(10, left)} more</button><span class="muted">${page.length} of ${rest.length} shown</span>`
    : (rest.length > 10 ? `<span class="muted">all ${rest.length} shown</span> <button class="btn small ghost" id="archive-less">Show the newest 10</button>` : "");
  const more = $("archive-more"); if (more) more.onclick = () => { alerts.shown += 10; drawArchive(); };
  const less = $("archive-less"); if (less) less.onclick = () => { alerts.shown = 10; drawArchive(); };
}
function drawScoreboard() {
  const ps = alerts.paper;
  if (!Array.isArray(ps)) {  // no paper book: the audit scoreboard, as before
    const sb = Object.values(alerts.scoreboard).sort((a, b) => (b.avg_r || 0) - (a.avg_r || 0));
    $("home-scoreboard").innerHTML = table([{ l: "Strategy", k: "strategy" }, { l: "Alerts", n: 1, k: "n" }, { l: "Positive %", n: 1, f: x => pct(x.hit_rate) },
      { l: "Avg R", n: 1, f: x => fmt(x.avg_r, 2), c: x => cls(x.avg_r) }, { l: "Σ est. P&L", n: 1, f: x => rup(x.pnl_sum), c: x => cls(x.pnl_sum) }], sb,
      { empty: "No resolved alerts yet." });
    return;
  }
  // one row per strategy, timeframe and market (paper test, 2026-11-08): an earlier timeframe
  // of the same strategy is its own row and never counts toward the test
  const market = x => x.kind === "INDEX" ? "index options" : x.kind === "STOCK" ? "stocks" : "";
  const slot = x => [x.timeframe, market(x)].filter(Boolean).join(" · ");
  const prim = ps.filter(x => x.primary), others = ps.filter(x => !x.primary && x.trades);
  $("home-scoreboard").innerHTML = table([
    { l: "Strategy", f: x => `<b>${esc(x.strategy)}</b>` + (slot(x) ? ` <span class="muted">${esc(slot(x))}</span>` : "") + (x.paused ? ` <span class="muted">· paused</span>` : "") },
    { l: "Trades", n: 1, k: "trades" },
    { l: "Win %", n: 1, f: x => x.win_rate != null ? pct(x.win_rate) : "–" },
    { l: "Net ₹", n: 1, f: x => esc(inr(x.net)), c: x => x.net == null ? "" : cls(Number(x.net)) },
    { l: "Avg / trade", n: 1, f: x => esc(inr(x.avg_net)), c: x => x.avg_net == null ? "" : cls(Number(x.avg_net)) },
    { l: "Profit factor", n: 1, f: x => fmt(x.profit_factor, 2) },
    { l: "Avg R", n: 1, f: x => fmt(x.avg_r, 2), c: x => cls(x.avg_r) },
    { l: "Open", n: 1, k: "open" },
    { l: "No fill", n: 1, k: "no_fill" },
  ], prim, { empty: "No primary strategy configured." }) +
  `<div class="hint mt">One lot per option trade, every alert, net of costs; delivery trades are shares worth about ${esc(rup(latest && latest.desk && latest.desk.delivery && latest.desk.delivery.unit_inr != null ? latest.desk.delivery.unit_inr : 10000))} each. Each row is one strategy on one timeframe and market, so trades from an earlier setting are not counted in it. A row with no trades has not alerted yet.` +
  (others.length ? ` Earlier strategies (no longer primary): ${others.map(x => `${esc(x.strategy)}${slot(x) ? ` ${esc(slot(x))}` : ""} ${x.trades} trades ${esc(inr(x.net))}`).join(" · ")}.` : "") + `</div>`;
}
function wireAlerts(root) {
  root.querySelectorAll("[data-take]").forEach(b => b.onclick = async (e) => { e.stopPropagation();
    const f = $("afill_" + b.dataset.take).value;
    const r = await api(`/api/desk/signal/${b.dataset.take}/ack`, { action: "TAKE", fill_price: f ? Number(f) : null, source: "dashboard" });
    toast(r.message, r.accepted ? "ok" : "warn"); desk.force = true; setTimeout(() => loadAlerts(true), 400); });
  root.querySelectorAll("[data-skip]").forEach(b => b.onclick = async (e) => { e.stopPropagation();
    await api(`/api/desk/signal/${b.dataset.skip}/ack`, { action: "SKIP", source: "dashboard" }); desk.force = true; setTimeout(() => loadAlerts(true), 400); });
}
document.addEventListener("click", (e) => {
  const h = e.target.closest("[data-atoggle]"); if (!h || e.target.closest("input,button")) return;
  const k = h.dataset.atoggle; alerts.open[k] = !alerts.open[k]; store.set("alerts.open", alerts.open);
  h.parentElement.classList.toggle("open", alerts.open[k]);
});
function renderHomePositions(d) {
  drawPositions("home-positions", (d && d.positions) || [], () => loadAlerts(true));
}
// One positions table in the page at a time (QA cycle 1, BUG-1): the Home and Live Desk tables
// use the same input ids, so the hidden tab's copy is cleared before the visible one is drawn
// (each tab draws only while it is shown); a cleared Live table is redrawn on its next render.
// QA cycle 2: a render that lands for a tab no longer shown (a late Home reload while the
// owner is on the Live Desk) draws nothing and clears nothing (BUG-13); a price typed but not
// saved in the cleared table is kept as a draft and put back when that table is drawn again
// (BUG-16).
const positionDrafts = {};
const tabShown = (name) => { const s = document.querySelector(`section[data-tab="${name}"]`); return !!s && s.classList.contains("active"); };
function drawPositions(box, ps, after) {
  if (!tabShown(box === "home-positions" ? "home" : "live")) { if (box === "desk-positions") desk.sig.pos = null; return; }
  const other = $(box === "home-positions" ? "desk-positions" : "home-positions");
  if (other && other.querySelectorAll("input[id]").length) {
    other.querySelectorAll("input[id]").forEach(i => { if (i.value !== "" && i.dataset.dirty) positionDrafts[i.id] = i.value; });
    other.innerHTML = "";
    if (other.id === "desk-positions") desk.sig.pos = null;
  }
  keepInputs($(box), () => { $(box).innerHTML = positionsTable(ps); });
  wirePositions($(box), after);
}
// Re-rendering a table must not wipe a price the owner is typing.
function keepInputs(root, render) {
  const vals = {}, focus = document.activeElement && root.contains(document.activeElement) ? document.activeElement.id : null;
  root.querySelectorAll("input[id]").forEach(i => { if (i.value !== "" && i.dataset.dirty) vals[i.id] = i.value; });
  render();
  root.querySelectorAll("input[id]").forEach(i => { if (positionDrafts[i.id] !== undefined && vals[i.id] === undefined) { vals[i.id] = positionDrafts[i.id]; } delete positionDrafts[i.id]; });
  Object.entries(vals).forEach(([id, v]) => { const i = $(id); if (i) { i.value = v; i.dataset.dirty = "1"; } });
  if (focus && $(focus)) $(focus).focus();
  root.querySelectorAll("input[id]").forEach(i => i.addEventListener("input", () => { i.dataset.dirty = "1"; }));
}
function wirePositions(root, after) {
  root.querySelectorAll("[data-dreport]").forEach(b => b.onclick = async () => {
    const id = b.dataset.dreport, en = $("du_" + id).value, ex = $("dx_" + id).value;
    if (!en && !ex) { toast("Type your entry and/or exit price first.", "warn"); return; }
    const r = await api(`/api/desk/position/${id}/report`, { entry_price: en ? Number(en) : null, exit_price: ex ? Number(ex) : null });
    toast(r.ok ? "Saved: your prices sit beside the alert's" : (r.message || "not saved"), r.ok ? "ok" : "warn");
    if (r.ok) { ["du_", "dx_"].forEach(k => { const i = $(k + id); if (i) delete i.dataset.dirty; }); }
    desk.force = true; if (after) after();
  });
  root.querySelectorAll("[data-dclose]").forEach(b => b.onclick = async () => {
    const id = b.dataset.dclose, v = $("dx_" + id).value;
    if (!v) { toast("Type the price you exited at, then Close now.", "warn"); return; }
    if (!confirm("Close it now at your exit price? It would otherwise close by itself at its exit alert's price.")) return;
    const r = await api(`/api/desk/position/${id}/close`, { exit_price: Number(v), reason: "MANUAL" });
    toast(r.closed ? `Closed: net ${rup(r.net_pnl)}` : (r.message || "not closed"), r.closed ? (r.net_pnl >= 0 ? "ok" : "bad") : "warn");
    desk.force = true; renderHomeJournal(); if (after) after();
  });
}
["alerts-days", "alerts-filter"].forEach(id => $(id).onchange = () => { alerts.shown = 10; loadAlerts(true); });
tabLoaders.home = () => { if (!acctLoaded) loadAccount(false).catch(() => {}); if (latest && latest.desk) renderHomePositions(latest.desk); renderHomeJournal(); loadAlerts(true).catch(() => {}); };
$("acct-refresh").onclick = async () => { $("acct-refresh").disabled = true; try { await loadAccount(true); toast("Account refreshed", "ok"); } finally { $("acct-refresh").disabled = false; } };

/* ═══════════════════ LIVE: legacy Bank Nifty engine ═════════════════ */
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
      <button class="btn take" id="takeBtn">Trade taken</button>
      <button class="btn" id="skipBtn">Skipped</button>
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
        <button class="btn small" data-close="${p.position_id}">Close</button></td></tr>`).join("") + `</tbody></table>`;
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
    d.innerHTML = `<span class="lvl ${c}">${esc(n.level)}</span> <b>${esc(n.title)}</b> <span class="dim">${hhmm(n.created_at)}</span><br><span class="muted">${esc((n.body || "").split("\n")[0])}</span>`;
    el.prepend(d);
    const hf = $("home-feed"); if (hf) { hf.prepend(d.cloneNode(true)); while (hf.children.length > 60) hf.removeChild(hf.lastChild); }
    if (n.level === "SIGNAL") { beep(); toast(n.title, "warn"); desk.force = true; }
    if (["EXIT", "CRITICAL"].includes(n.level)) { toast(n.title, "bad"); desk.force = true; }
  });
  while (el.children.length > 80) el.removeChild(el.lastChild);
}

/* ═══════════════════ LIVE: the multi-instrument desk ════════════════ */
const desk = { rendered: 0, force: true, sig: {}, open: store.get("desk.open", {}) };
function hdot(state) { return `<span class="hs ${state === "OK" ? "ok" : (state === "CLOSED" || state === "LAGGING" ? "warn" : (state === "NO DATA" ? "" : "bad"))}"></span>`; }
function sigNum(l, v, c = "") { return `<div class="sig-num"><small>${l}</small><b class="${c}">${v}</b></div>`; }
function signalCard(s) {
  const e = s.economics || {}, sell = s.direction === "short", o = e.option || {};
  const title = s.kind === "INDEX" ? `BUY ${esc(s.label)} ${o.strike ? fmt(o.strike, 0) : ""} ${esc(o.opt_type || "")} <span class="num" style="font-size:15px">${esc(e.unit_label)}</span>`
    : `${sell ? "SELL" : "BUY"} ${esc(s.label)} <span class="num" style="font-size:15px">${esc(e.unit_label || "delivery")}</span>`;
  return `<div class="card sigcard ${sell ? "sell" : ""}" data-sig="${s.id}">
    <div class="row between"><div class="sig-title">${title}</div><span class="countdown" data-exp="${esc(s.expires_at)}">…</span></div>
    <div class="sig-sub">${esc(s.strategy_label || s.strategy)} · ${esc(s.timeframe)} · signal bar ${hhmm(s.bar_ts)} · strength ${fmt(s.strength, 0)}${e.synthetic ? " · <span class='warn'>premium synthetic</span>" : ""}</div>
    <div class="sig-nums">
      ${sigNum(s.kind === "INDEX" ? "Est. premium" : "Entry", rup2(e.entry_price))}
      ${sigNum(s.kind === "INDEX" ? "Premium at stop" : "Stop", rup2(e.stop_price), "neg")}
      ${sigNum(s.kind === "INDEX" ? "Premium at target" : "Target", e.target_price != null ? rup2(e.target_price) : "trail", "pos")}
      ${sigNum("Risk ₹ (incl. charges)", rup(e.risk_inr), "neg")}
      ${sigNum("Net reward ₹", e.reward_inr != null ? rup(e.reward_inr) : "–", "pos")}
      ${sigNum("Net R/R", fmt(e.rr_net))}
      ${sigNum("Outlay", rup(e.outlay))}
      ${s.kind === "INDEX" ? sigNum("Underlying stop / target", `${fmt(e.underlying_stop, 0)} / ${e.underlying_target != null ? fmt(e.underlying_target, 0) : "–"}`) : ""}
    </div>
    <pre>${esc(s.text)}</pre>
    <div class="actions">
      <label class="inline">Your fill (optional, to compare) <input type="number" step="0.05" id="dfill_${s.id}" placeholder="${fmt(e.entry_price)}" style="width:130px"></label>
      <button class="btn take" data-take="${s.id}">Trade taken</button>
      <button class="btn" data-skip="${s.id}">Skipped</button>
    </div></div>`;
}
function positionAlertAt(p) {
  if (p.alert_created_at) return p.alert_created_at;
  const a = alerts.rows.find(s => s.id === p.signal_id);
  return a ? a.created_at : null;
}
function positionExit(p) {
  if (p.paper_status) return [`on its exit alert <span class="dim">· paper #${esc(p.paper_trade_id)} ${esc(String(p.paper_status).replace(/_/g, " ").toLowerCase())}</span>`, "muted"];
  if (p.session_over) return [esc(p.hold_check || "session over"), "neg pos-advice"];
  if (p.exit_advice) return [`EXIT · ${esc(p.exit_advice.replace(/_/g, " "))}`, "neg pos-advice"];
  return ["hold", "muted"];
}
function positionsTable(ps) {
  if (!ps.length) return `<div class="empty-inline"><svg class="mascot sm"><use href="#m-cow-sleep"/></svg> Flat. Moo is napping until you take a signal.</div>`;
  return `<table><thead><tr><th>Alert at</th><th>Taken at</th><th>Instrument</th><th>Side</th><th class="n">Alert entry</th><th class="n">Your entry</th><th class="n">Underlying now</th><th class="n">Stop</th><th class="n">Target</th>
    <th class="n">R now</th><th class="n">Unrealised</th><th>Exit</th><th class="n">Held</th><th>Your prices <span class="dim">(optional)</span></th></tr></thead><tbody>` +
    ps.map(p => { const [ex, exCls] = positionExit(p); return `<tr><td>${tsf(positionAlertAt(p))}</td><td>${tsf(p.opened_at)}</td>
      <td><b>${esc(p.label)}</b> <span class="dim">${esc(p.strategy)} ${esc(p.timeframe)}${p.option && p.option.strike ? ` · ${fmt(p.option.strike, 0)} ${esc(p.option.opt_type)}` : ""}</span></td>
      <td>${esc(p.side.replace("_", " "))}</td><td class="n">${fmt(p.alert_entry_price != null ? p.alert_entry_price : p.entry_price)}</td>
      <td class="n">${p.user_entry_price != null ? fmt(p.user_entry_price) : "<span class='dim'>–</span>"}</td><td class="n">${fmt(p.last_underlying)}</td>
      <td class="n">${fmt(p.stop_underlying)}${p.trail_active ? " <span class='dim'>trail</span>" : ""}</td><td class="n">${p.target_underlying != null ? fmt(p.target_underlying) : "–"}</td>
      <td class="n ${cls(p.r_now)}">${fmt(p.r_now, 2)}R</td><td class="n ${cls(p.unrealized)}">${rup(p.unrealized)}</td>
      <td class="${exCls}">${ex}</td><td class="n">${p.bars_held} bars</td>
      <td class="pform"><input type="number" step="0.05" id="du_${p.id}" placeholder="your entry" value="${p.user_entry_price != null ? p.user_entry_price : ""}" style="width:110px">
        <input type="number" step="0.05" id="dx_${p.id}" placeholder="your exit" value="${p.user_exit_price != null ? p.user_exit_price : ""}" style="width:110px">
        <button class="btn small" data-dreport="${p.id}">Save</button>
        <button class="btn small ghost" data-dclose="${p.id}" title="You exited before its exit alert: close it now at your exit price">Close now</button></td></tr>`; }).join("") + `</tbody></table>`;
}
/* ── paper trades (US-030): every alert's paper trade · one unit · never an order ──
   Pure one-line helpers (node-tested) render GET /api/paper; money arrives as rupee strings. */
const todayIST = () => { const p = Object.fromEntries(new Intl.DateTimeFormat("en-GB", { timeZone: "Asia/Kolkata", year: "numeric", month: "2-digit", day: "2-digit" }).formatToParts(new Date()).map((x) => [x.type, x.value])); return `${p.year}-${p.month}-${p.day}`; };
const paperChip = (st) => { const c = st === "PENDING_FILL" ? ["pending", "pending"] : st === "OPEN" ? ["open", "open"] : st === "NO_FILL" ? ["nofill", "no fill"] : st === "CLOSED" ? ["closed", "closed"] : ["", String(st ?? "?").toLowerCase()]; return `<span class="pchip ${c[0]}">${esc(c[1])}</span>`; };
const paperWhen = (s, today) => !s ? "–" : (String(s).slice(0, 10) === today ? hhmm(s) : tsf(s));
const paperRow = (t, today = todayIST()) => `<tr><td>${paperChip(t.status)}</td> <td><b>${esc(t.instrument)}</b> <span class="dim">${esc(t.strategy)} · ${esc(t.timeframe)}${t.qty > 1 ? ` · qty ${esc(t.qty)}` : ""}</span></td> <td>${esc(String(t.side ?? "").replace(/_/g, " "))}</td> <td class="${t.grade === "live" ? "pos" : "warn"}">${esc(t.grade)}</td> <td class="n">${esc(inr(t.entry))}</td> <td class="n">${esc(inr(t.exit))}</td> <td class="n ${t.gross == null ? "" : cls(Number(t.gross))}">${esc(inr(t.gross))}</td> <td class="n ${t.net == null ? "" : cls(Number(t.net))}">${esc(inr(t.net))}</td> <td>${esc(t.close_reason ? String(t.close_reason).replace(/_/g, " ") : "–")}</td> <td class="n">${esc(paperWhen(t.created_at, today))}${t.closed_at ? ` → ${esc(paperWhen(t.closed_at, today))}` : ""}</td></tr>`;
const paperList = (trades) => Array.isArray(trades) ? trades.filter((t) => t && typeof t === "object") : [];
const paperOpen = (trades) => paperList(trades).filter((t) => t.status === "PENDING_FILL" || t.status === "OPEN");
const paperClosedOn = (trades, day) => paperList(trades).filter((t) => (t.status === "CLOSED" || t.status === "NO_FILL") && String(t.closed_at ?? "").slice(0, 10) === day);
const paperShown = (trades, today) => [...paperOpen(trades), ...paperClosedOn(trades, today)];
// US-032: a session date "2026-10-30" -> "Fri 30 Oct", read from the string itself (never the browser's zone); anything else is shown as it came.
const paperDay = (d) => { const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(d ?? "")); const t = m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3])) : null; return t && t.getUTCMonth() === +m[2] - 1 && t.getUTCDate() === +m[3] ? `${["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][t.getUTCDay()]} ${+m[3]} ${["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][+m[2] - 1]}` : (d == null ? "–" : String(d)); };
const paperSession = (p) => (p && p.last_session && typeof p.last_session === "object" && /^\d{4}-\d{2}-\d{2}$/.test(String(p.last_session.date))) ? p.last_session : null;
const paperFallback = (p) => !!paperSession(p) && !!p.totals_today && p.totals_today.closed === 0;
const paperEmpty = (msg) => `<div class="empty-inline"><svg class="mascot sm"><use href="#m-cow-sleep"/></svg> ${esc(msg)}</div>`;
const paperGroup = (label) => `<tr class="pgroup"><td colspan="10">${esc(label)}</td></tr>`;
const paperHead = (s, note) => `<div class="paper-head"><b>Last session · ${esc(paperDay(s.date))}</b>${note ? ` <span class="dim">${esc(note)}</span>` : ""}</div>`;
const paperTable = (body) => `<table class="paper"><thead><tr><th>Status</th> <th>Instrument</th> <th>Side</th> <th>Grade</th> <th class="n" title="price per share, or per option unit of the lot">Entry / unit</th> <th class="n" title="price per share, or per option unit of the lot">Exit / unit</th> <th class="n" title="the whole trade before costs: (exit - entry) x qty for a buy or an option, (entry - exit) x qty for a sell">Gross (trade)</th> <th class="n" title="the whole trade after costs; empty until costs are applied">Net (trade)</th> <th>Close reason</th> <th class="n">Time</th></tr></thead><tbody>${body}</tbody></table>`;
const paperCut = (s, past) => { const listed = paperList(past).filter((t) => t.status === "CLOSED").length, total = Number((s.totals || {}).closed) || 0; return listed < total ? `${listed} of ${total} closes listed (the list holds the 200 newest trades)` : ""; };
const paperPanel = (p, view = "today") => { if (!p || !p.enabled) return paperEmpty("Paper trading is off: no new paper trades are opened."); const today = todayIST(), all = paperList(p.trades), s = paperSession(p), rows = (ts) => ts.map((t) => paperRow(t, today)).join(""), gone = "None of its trades is in the list (it holds the 200 newest)."; if (!all.length) return paperEmpty("No paper trades yet."); if (view === "all") return paperTable(rows(all)); if (view === "last") { if (!s) return paperEmpty("No earlier session with closed or no-fill paper trades yet."); const past = paperClosedOn(all, s.date); return paperHead(s, paperCut(s, past)) + (past.length ? paperTable(rows(past)) : paperEmpty(gone)); } if (!paperFallback(p)) { const shown = paperShown(all, today); return shown.length ? paperTable(rows(shown)) : paperEmpty("Nothing open, and none closed today."); } const now = [...paperOpen(all), ...paperClosedOn(all, today)], past = paperClosedOn(all, s.date), cut = paperCut(s, past), label = `Last session · ${paperDay(s.date)}`, head = paperHead(s, cut ? `nothing closed today yet · ${cut}` : "nothing closed today yet"); if (!now.length && !past.length) return head + paperEmpty(gone); return head + paperTable(rows(now) + (now.length || !past.length ? paperGroup(past.length ? label : `${label} · none of its trades is in the list`) : "") + rows(past)); };
const paperSince = (s) => { const errors = (Number(s.hook_errors) || 0) + (Number(s.desk_hook_errors) || 0); return `since restart: ${s.opened ?? 0} opened · ${s.filled ?? 0} filled · ${s.no_fill ?? 0} no fill${errors ? ` · ${errors} errors (see the log)` : ""}`; };
const paperTotals = (label, d) => `${label}: ${d.closed ?? 0} closed · ${d.wins ?? 0} won · ${d.losses ?? 0} lost · gross ${inr(d.gross_rupees)} · net ${inr(d.net_rupees)}${d.net_rupees == null ? " (no costs yet)" : ""}`;
const paperMeta = (p, view = "today") => { if (!p || !p.enabled) return ""; const st = p.status || {}, s = paperSession(p), last = s ? paperTotals(`last session ${paperDay(s.date)}`, s.totals || {}) : "last session: none before today"; if (view === "all") return `all: ${paperList(p.trades).length} trades listed, newest first (at most 200) · ${paperSince(st)}`; if (view === "last") return `${last} · ${paperSince(st)}`; if (paperFallback(p)) return `${last} · today: nothing closed yet · ${paperSince(st)}`; return `${paperTotals("today", p.totals_today || {})} · ${paperSince(st)}`; };
// US-032: which trades the panel shows. Arrow keys / Home / End move along the radio group.
const PAPER_VIEWS = ["today", "last", "all"];
const paperViewOf = (v) => PAPER_VIEWS.includes(v) ? v : "today";
const paperViewKey = (view, key) => { const n = PAPER_VIEWS.length, i = PAPER_VIEWS.indexOf(paperViewOf(view)); return key === "ArrowRight" || key === "ArrowDown" ? PAPER_VIEWS[(i + 1) % n] : key === "ArrowLeft" || key === "ArrowUp" ? PAPER_VIEWS[(i + n - 1) % n] : key === "Home" ? PAPER_VIEWS[0] : key === "End" ? PAPER_VIEWS[n - 1] : null; };
// Fetched when the desk snapshot's version moves (and on opening the tab), at most every 5 s.
// `view` is the chosen view, `last` the last good answer (re-rendered at once on a switch).
const paperFeed = { at: 0, timer: null, version: null, shown: false, view: "today", last: null };
const liveTabActive = () => { const s = document.querySelector('section[data-tab="live"]'); return !!s && s.classList.contains("active"); };
// ── fill costs (owner, 2026-11-07): measured one-unit spreads beside the Lab's assumptions ──
const fillCost = { at: 0 };
const pctTxt = (v, d = 3) => (v === null || v === undefined || Number.isNaN(Number(v))) ? "–" : `${Number(v).toFixed(d)}%`;
async function loadFillCosts(force) {
  if (!force && Date.now() - fillCost.at < 300000) return;
  fillCost.at = Date.now();
  try {
    const r = await fetch("/api/desk/fill-costs?days=30");
    const f = await r.json().catch(() => null);
    if (!r.ok || !f || !Array.isArray(f.by_instrument)) throw new Error((f && f.detail) || `HTTP ${r.status}`);
    const a = f.lab_assumptions || {}, m = f.measured || {};
    $("desk-fillcost-meta").textContent = `${f.samples} quotes · last ${f.days} days`;
    const head = `<p class="muted">Stocks: measured half-spread <b>${pctTxt(m.stock_half_spread_median_pct)}</b> a side (Lab assumes ${pctTxt(a.stock_slippage_pct_per_side, 2)} slippage). Options: <b>${pctTxt(m.option_half_spread_median_pct)}</b> a side (Lab assumes ${pctTxt(a.option_half_spread_pct_per_side, 1)} + ${esc(a.option_slippage_ticks_per_side)} ticks). Positive drift = a worse price for that alert's side.</p>`;
    const rows = f.by_instrument.map(x => `<tr><td>${esc(x.instrument)}</td><td>${esc(x.kind)}</td><td class="num">${esc(x.samples)}</td><td class="num">${pctTxt(x.half_spread_median_pct)}</td><td class="num">${pctTxt(x.half_spread_p90_pct)}</td><td>${Object.entries(x.by_time || {}).map(([b, v]) => `${esc(b)} ${pctTxt(v)}`).join(" · ")}</td></tr>`).join("");
    const inst = rows ? `<table><thead><tr><th>Instrument</th><th>Kind</th><th>Quotes</th><th>Half-spread (median)</th><th>p90</th><th>By time of day</th></tr></thead><tbody>${rows}</tbody></table>` : `<div class="muted">No quotes yet: the desk samples every 5 minutes while the market is open (Dhan Data API).</div>`;
    const al = (f.alerts || []).slice(-10).reverse().map(x => `<tr><td>${esc((x.at || "").slice(0, 16).replace("T", " "))}</td><td>${esc(x.instrument)}</td><td>${esc(x.side)}</td><td class="num">${pctTxt(x.half_spread_pct)}</td><td class="num">${pctTxt(x.fill_vs_alert_pct)}</td><td class="num">${pctTxt(x.after_60s_pct)}</td><td class="num">${pctTxt(x.after_120s_pct)}</td><td class="num">${pctTxt(x.after_300s_pct)}</td></tr>`).join("");
    const alerts = al ? `<h3>At each alert</h3><table><thead><tr><th>Alert</th><th>Instrument</th><th>Side</th><th>Half-spread</th><th>Fill vs alert</th><th>+1 min</th><th>+2 min</th><th>+5 min</th></tr></thead><tbody>${al}</tbody></table>` : "";
    const own = (f.owner_fills || []).map(x => `<tr><td>${esc(x.instrument)}</td><td>${esc(x.leg)}</td><td class="num">${esc(x.alert_price)}</td><td class="num">${esc(x.your_price)}</td><td class="num">${pctTxt(x.slippage_pct)}</td></tr>`).join("");
    const mine = own ? `<h3>Your own fills</h3><table><thead><tr><th>Instrument</th><th>Leg</th><th>Alert price</th><th>Your price</th><th>Slippage</th></tr></thead><tbody>${own}</tbody></table>` : "";
    const ch = ((f.charges || {}).by_product || []).map(x => `<tr><td>${esc(x.product)}</td><td>${esc(x.side)}</td><td class="num">${esc(x.fills)}</td><td class="num">${rup(x.turnover)}</td><td class="num">${rup2(x.actual)} (${pctTxt(x.actual_pct)})</td><td class="num">${rup2(x.model)} (${pctTxt(x.model_pct)})</td></tr>`).join("");
    const charges = ch ? `<h3>Charges on your own fills <small>Dhan's trade book, last ${esc((f.charges || {}).days)} days · billed vs the cost table</small></h3><table><thead><tr><th>Product</th><th>Side</th><th>Fills</th><th>Turnover</th><th>Billed by Dhan</th><th>Cost table</th></tr></thead><tbody>${ch}</tbody></table>` : "";
    $("desk-fillcost").innerHTML = head + inst + alerts + mine + charges;
  } catch (e) {
    $("desk-fillcost-meta").textContent = `fill costs unavailable: ${e.message}`;
  }
}
function wantPaper(force) {
  if (paperFeed.timer || (!force && paperFeed.version === lastDeskVersion)) return;
  paperFeed.timer = setTimeout(loadPaper, Math.max(0, 5000 - (Date.now() - paperFeed.at)));
}
async function loadPaper() {
  const version = lastDeskVersion;
  paperFeed.at = Date.now();
  try {
    const r = await fetch("/api/paper");  // a read: no token, and no toast every 5 s on failure
    const p = await r.json().catch(() => null);
    if (!r.ok) throw new Error((p && p.detail) || `HTTP ${r.status}`);
    // Only a reply that says enabled true or false is an answer: a proxy page, a non-JSON body
    // or a half-written object must never read as "Paper trading is off".
    if (!p || typeof p.enabled !== "boolean") throw new Error("unexpected reply from /api/paper");
    paperFeed.version = version;
    paperFeed.shown = true;
    paperFeed.last = p;
    $("desk-paper").innerHTML = paperPanel(p, paperFeed.view);
    $("desk-paper-meta").className = "muted";
    $("desk-paper-meta").textContent = paperMeta(p, paperFeed.view);
  } catch (e) {
    // The last table stays (still worth reading); the day line turns into a warning, and a
    // panel that never loaded says why instead of "Loading" for ever.
    const why = `paper trades unavailable: ${e.message}`;
    $("desk-paper-meta").className = "warn";
    $("desk-paper-meta").textContent = why;
    if (!paperFeed.shown) $("desk-paper").innerHTML = `<div class="empty-inline"><span class="warn">${esc(why)}</span></div>`;
  } finally {
    paperFeed.timer = null;
    if (paperFeed.version !== lastDeskVersion && liveTabActive()) wantPaper(false);
  }
}
// Select a view: one checked, tabbable button (roving tabindex), remembered per browser through
// `store` (localStorage in a try/catch: a private window simply forgets) unless `remember` is
// false (the start-up restore), shown at once from the last good answer. A failure line on the
// day line is left as it is. Keys with Alt / Ctrl / Meta / Shift are the browser's, not ours.
function showPaperView(view, remember = true) {
  paperFeed.view = paperViewOf(view);
  if (remember) store.set("paper.view", paperFeed.view);  // start-up restores, never writes
  document.querySelectorAll("[data-paper-view]").forEach((b) => {
    const on = b.dataset.paperView === paperFeed.view;
    b.setAttribute("aria-checked", on ? "true" : "false");
    b.tabIndex = on ? 0 : -1;
  });
  if (!paperFeed.last) return;
  $("desk-paper").innerHTML = paperPanel(paperFeed.last, paperFeed.view);
  if ($("desk-paper-meta").className !== "warn") $("desk-paper-meta").textContent = paperMeta(paperFeed.last, paperFeed.view);
}
const paperViewClick = (e) => { const b = e.target && e.target.closest ? e.target.closest("[data-paper-view]") : null; if (b) showPaperView(b.dataset.paperView); };
const paperViewKeydown = (e) => { if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return; const b = e.target && e.target.closest ? e.target.closest("[data-paper-view]") : null, next = b ? paperViewKey(b.dataset.paperView, e.key) : null; if (!next) return; e.preventDefault(); showPaperView(next); const nb = document.querySelector(`[data-paper-view="${next}"]`); if (nb) nb.focus(); };
document.addEventListener("click", paperViewClick);
document.addEventListener("keydown", paperViewKeydown);
showPaperView(store.get("paper.view", "today"), false);
/* ── micro trend: Supertrend(14, 1.7) read on six timeframes, `trend` of every desk row (desk/tracker.py _read_trend).
   Pure one-line helpers so tests/desk/test_trend_ladder_ui.py can run them in node. Every field goes through esc(). ── */
const trendRows = (t) => (t && Array.isArray(t.rows)) ? t.rows.filter((x) => x && typeof x === "object") : [];
const trendCls = (d) => d > 0 ? "up" : (d < 0 ? "down" : "none");
const trendWord = (d) => d > 0 ? "up" : (d < 0 ? "down" : "no reading");
const trendMark = (d) => d > 0 ? "▲" : (d < 0 ? "▼" : "·");
// [class, words]. "up" / "down" only when EVERY timeframe agrees: one without a reading breaks the agreement, as it keeps the strategy's gate shut.
const trendVerdict = (t) => { const rows = trendRows(t), n = rows.length, up = rows.filter((x) => x.direction > 0).length, dn = rows.filter((x) => x.direction < 0).length; if (n && rows[0].trigger) return !(up + dn) ? ["none", "no trend reading"] : up === n ? ["up", "aligned up"] : dn === n ? ["down", "aligned down"] : ["mixed", "not aligned"]; return !n || !(up + dn) ? ["none", "no trend reading"] : up === n ? ["up", `all ${n} up`] : dn === n ? ["down", `all ${n} down`] : ["mixed", `${up} up · ${dn} down${up + dn < n ? ` · ${n - up - dn} no reading` : ""}`]; };
const trendDay = (s) => { const m = /^\d{4}-(\d{2})-(\d{2})/.exec(String(s ?? "")); return m ? `${+m[2]} ${["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][+m[1] - 1] ?? "?"}` : "–"; };
// When a timeframe's trend began: the time if that was on the day of the reading, else the day (with the time for the intraday timeframes); "or earlier" when the bars held do not reach the turn.
const trendSince = (x, at) => !x || !x.since ? "–" : (x.timeframe === "1d" ? trendDay(x.since) : String(x.since).slice(0, 10) === String(at ?? "").slice(0, 10) ? hhmm(x.since) : `${trendDay(x.since)} ${hhmm(x.since)}`) + (x.since_exact === false ? " or earlier" : "");
// How far the last price stands from a timeframe's line, in % of the line: the room left before that line is touched.
const trendGap = (x, price) => { const p = Number(price), l = Number(x && x.line); return !x || x.line == null || price == null || !Number.isFinite(p) || !Number.isFinite(l) || l === 0 ? "–" : `${Math.abs((p - l) / l * 100).toFixed(2)}% ${p >= l ? "above" : "below"}`; };
const trendTitle = (t) => `Supertrend(${t.period}, ${t.mult}): ` + trendRows(t).map((x) => `${x.timeframe} ${trendWord(x.direction)}`).join(" · ");
// The strip on a card's head: one pip per timeframe, fastest first, then the count. The direction is in the glyph as well as the colour.
const trendReversal = (t) => { const r = t && t.reversal; return r ? ` · reversed ${esc(r.reversed)} of ${esc(r.of)}, exit at ${esc(r.exit_at)}${r.warning ? ` · ${esc(r.warning)}` : ""}` : ""; };
const trendPips = (t) => { const rows = trendRows(t), v = trendVerdict(t); return !rows.length ? `<div class="trend-strip"><span class="lbl">Micro trend</span><span class="tverdict none">no reading yet</span></div>` : `<div class="trend-strip" title="${esc(trendTitle(t))}"><span class="lbl">Micro trend</span>${rows.map((x) => `<span class="tpip ${trendCls(x.direction)}"${x.trigger ? ' title="the trigger"' : ""}>${trendMark(x.direction)} ${esc(x.timeframe)}${x.trigger ? " trigger" : ""}</span>`).join("")}<span class="tverdict ${v[0]}">${esc(v[1])}${trendReversal(t)}</span></div>`; };
// The table in an open card and in the focus panel: each timeframe's trend, its line, the room to it and since when.
const trendBlock = (t, price) => { const rows = trendRows(t), v = trendVerdict(t); if (!rows.length) return `<h4>Micro trend</h4><div class="hint">No trend reading yet: it comes with the first bars.</div>`; const body = rows.map((x) => `<tr><td><b>${esc(x.timeframe)}</b>${x.trigger ? " <span class=\"dim\">trigger</span>" : ""}</td> <td class="tdir ${trendCls(x.direction)}">${trendMark(x.direction)} ${trendWord(x.direction)}</td> <td class="n">${fmt(x.line)}</td> <td class="n">${esc(trendGap(x, price))}</td> <td class="n">${esc(trendSince(x, t.at))}</td></tr>`).join(""); return `<h4>Micro trend · Supertrend(${esc(t.period)}, ${esc(t.mult)})</h4><div class="tbl"><table class="trend"><thead><tr><th>Timeframe</th> <th>Trend</th> <th class="n" title="the Supertrend line: under the price in an up trend, over it in a down trend">Line</th> <th class="n" title="how far the last price stands from the line">Price vs line</th> <th class="n" title="when this trend began">Since</th></tr></thead><tbody>${body}</tbody></table></div><div class="hint"><b class="tverdict ${v[0]}">${esc(v[1])}</b>${trendReversal(t)} · a micro trend needs all ${rows.length} to agree · read at ${esc(hhmm(t.at))} from ${esc(t.base)} bars</div>${t.note ? `<div class="hint warn">${esc(t.note)}</div>` : ""}`; };
// How a slot trails its stop, for the strategies table: along the strategy's own line or by ATR.
const slotTrail = (s) => { const t = s && s.trail, p = (s && s.params) || {}; return t ? `${esc(t.timeframe)} line` : Number(p.trail_atr_mult) > 0 ? `ATR × ${esc(p.trail_atr_mult)}` : "–"; };
// Under that table, for a slot that trails along its own line: where the line lets a stop stand now, for the sides the slot trades. `kind` INDEX names the sides CE / PE.
const slotTrailNote = (s, kind) => { const t = s && s.trail, p = (s && s.params) || {}; if (!t) return ""; const side = (k, word) => p["allow_" + k] === false ? "" : t[k] != null ? ` · ${word} stop level now <b>${fmt(t[k])}</b>` : ` · no ${word} stop level now (the line is on the other side)`; return `<div class="hint"><b>${esc(s.name)} ${esc(s.timeframe)}</b> trails its stop along the ${esc(t.timeframe)} Supertrend line${side("long", kind === "INDEX" ? "CE" : "long")}${side("short", kind === "INDEX" ? "PE" : "short")}</div>`; };
// ── US-067: the pullback delivery trades (alerts and paper only; the system never places an order) ──
const pbTick = (ok) => ok ? `<span class="pos">yes</span>` : `<span class="neg">no</span>`;
const pbBlock = (pb) => { if (!pb || typeof pb !== "object") return ""; const r = pb.reading, t = pb.trade, hist = pb.history_to ? `daily history complete to ${esc(pb.history_to)}` : "no complete daily history"; const body = r ? `${kv("Session", esc(pb.session))}${kv("Below the 20-session closing high", `${fmt(r.below_high_pct, 2)}% of ${fmt(r.needed_pct, 0)}% needed`, r.deep ? "pos" : "")}${kv("Close above SMA200", `${pbTick(r.above_sma200)} (${fmt(r.close)} vs ${fmt(r.sma200)})`)}${kv("NIFTY above its SMA200", pbTick(r.nifty_above_sma200))}${kv("Signal", r.signal ? `<b class="pos">BUY at the next open</b>` : esc(r.why))}` : ""; return `<h4>Pullback (delivery, daily)</h4>${body}${t ? `<div class="hint">${esc(String(t.status).replace(/_/g, " ").toLowerCase())}: ${esc(t.qty)} shares, signal ${esc(t.signal_date)}${t.warning ? ` · <span class="warn">${esc(t.warning)}</span>` : ""}</div>` : ""}${pb.note ? `<div class="hint warn">${esc(pb.note)}</div>` : ""}<div class="hint">${hist}${pb.stored_to ? ` · store to ${esc(pb.stored_to)}` : ""}</div>${pb.evidence ? `<div class="hint">${esc(pb.evidence)}</div>` : ""}`; };
const dlvRow = (t) => `<tr><td><b>${esc(t.label || t.series_key)}</b> <span class="dim">${esc(t.qty)} sh · signal ${esc(t.signal_date)}</span>${t.warning ? `<div class="warn">${esc(t.warning)}</div>` : ""}</td> <td>${esc(String(t.status).replace(/_/g, " ").toLowerCase())}${t.entry_late ? " (late)" : ""}</td> <td class="n">${fmt(t.entry)}</td> <td class="n">${fmt(t.last)}</td> <td class="n ${cls(t.unrealised)}">${rup2(t.unrealised)} <span class="dim">${spct(t.unrealised_pct)}</span></td> <td class="n">${t.sessions == null ? "–" : `${esc(t.sessions)} of ${esc(t.max_sessions)}`}</td> <td class="n">${fmt(t.sma_exit)}</td> <td class="n">${fmt(t.stop)}</td></tr>`;
const dlvAlert = (t) => `<div class="card"><b>${esc(t.status === "EXIT_PENDING" ? "SELL" : "BUY")} ${esc(t.qty)} ${esc(t.label || t.series_key)}</b> <span class="dim">pullback 1d · delivery · ${esc(t.status === "EXIT_PENDING" ? `exit signal ${t.exit_signal_date}` : `signal ${t.signal_date}`)}</span><div class="hint">${esc(t.alert || "")}</div>${t.warning ? `<div class="hint warn">${esc(t.warning)}</div>` : ""}</div>`;
const dlvHome = (d) => { if (!d || typeof d !== "object") return ""; const pend = Array.isArray(d.pending) ? d.pending : [], open = Array.isArray(d.open) ? d.open : []; const head = `<h3>Delivery (pullback) <small class="muted">${d.history_to ? `daily history complete to ${esc(d.history_to)}` : "daily history not complete"}${(d.stale || []).length ? ` · <span class="warn">not fresh: ${esc(d.stale.join(", "))}</span>` : ""}${d.top_up ? ` · ${esc(d.top_up)}` : ""}</small></h3>`; const table = open.length ? `<div class="scroll"><table><thead><tr><th>Stock</th> <th>Status</th> <th class="n">Entry</th> <th class="n">Last</th> <th class="n">Unrealised</th> <th class="n">Sessions</th> <th class="n" title="a close above this exits">SMA20 exit</th> <th class="n" title="a close at or below this exits">Stop</th></tr></thead><tbody>${open.map(dlvRow).join("")}</tbody></table></div>` : `<div class="muted">No open delivery trade.</div>`; return head + pend.map(dlvAlert).join("") + table; };
const dlvArchive = (list) => !Array.isArray(list) || !list.length ? "" : `<h4>Delivery alerts</h4><div class="scroll"><table><thead><tr><th>Stock</th> <th>Signal</th> <th>Status</th> <th class="n">Entry</th> <th class="n">Exit</th> <th>Reason</th> <th class="n">Net</th></tr></thead><tbody>${list.map((t) => `<tr><td><b>${esc(t.label || t.series_key)}</b> <span class="dim">${esc(t.qty)} sh</span></td> <td>${esc(t.signal_date)}</td> <td>${esc(String(t.status).replace(/_/g, " ").toLowerCase())}${t.note ? ` <span class="dim">${esc(t.note)}</span>` : ""}</td> <td class="n">${fmt(t.entry)}${t.entry_late ? " (late)" : ""}</td> <td class="n">${fmt(t.exit)}${t.exit_late ? " (late)" : ""}</td> <td>${esc(t.exit_reason || "–")}</td> <td class="n ${cls(t.net)}">${rup2(t.net)}${t.net_pct == null ? "" : ` <span class="dim">${spct(t.net_pct)}</span>`}</td></tr>`).join("")}</tbody></table></div>`;
// ── Home's "Today" strip: one line each for the session, the feed, the token, the two rules, and the next thing to do. Pure, for tests/desk/test_today_strip_ui.py. ──
const tokenHoursLeft = (tv, now) => { const m = String(tv ?? "").match(/(\d{2})\/(\d{2})\/(\d{4})\s+(\d{2}):(\d{2})/); return m ? (new Date(`${m[3]}-${m[2]}-${m[1]}T${m[4]}:${m[5]}:00+05:30`) - now) / 3600000 : null; };
const todayLines = (d, tv, now) => { const out = [], left = tokenHoursLeft(tv, now), next = []; if (!d || typeof d !== "object" || d.booting) return [["Desk", d && d.booting ? "starting: the first bars are arriving" : "the Live Desk is not running", "warn"]]; const ses = d.session || {}, src = d.source || {}, openAt = ses.open_at ? new Date(ses.open_at) : null; out.push(["Session", ses.open ? `open · closes ${hhmm(ses.close_at)}${ses.entries_allowed ? "" : " · no new entries"}` : ses.trading_day && openAt && now < openAt ? `closed · opens at ${hhmm(ses.open_at)}` : ses.trading_day ? "closed for the day" : `closed · no session today${ses.holiday ? ` (${ses.holiday})` : ""}`, ses.open ? "ok" : ""]); out.push(["Feed", src.last_error ? `${delayNote(src)} · ${String(src.last_error).slice(0, 80)}` : src.delayed ? `${delayNote(src)} (${src.name || "?"} bars)` : `live (${src.name || "?"})`, src.last_error ? "bad" : src.delayed ? "warn" : "ok"]); if (left == null) out.push(["Dhan token", "time left unknown: refresh the portfolio", "warn"]); else { out.push(["Dhan token", left <= 0 ? "EXPIRED" : `${left < 1 ? Math.round(left * 60) + " min" : Math.floor(left) + " h " + Math.round((left % 1) * 60) + " min"} left`, left < 2 ? "bad" : "ok"]); if (left < 2) next.push(left <= 0 ? "Renew the Dhan token in Settings: it has expired." : "Renew the Dhan token in Settings: under two hours left."); } const idx = Array.isArray(d.indices) ? d.indices : [], al = idx.filter((r) => ["up", "down"].includes(trendVerdict(r.trend)[0])), pos = Array.isArray(d.positions) ? d.positions : []; out.push(["Options · micro-trend", (al.length ? `${al.length} of ${idx.length} aligned: ${al.map((r) => `${r.label || r.key} ${trendVerdict(r.trend)[0]}`).join(", ")}` : "no alignment") + (pos.length ? ` · open trade: ${pos.map((p) => p.label || p.key || p.series_key || "?").join(", ")}` : " · no open trade"), al.length || pos.length ? "ok" : ""]); const dl = d.delivery && typeof d.delivery === "object" ? d.delivery : null; if (!dl || dl.enabled === false) out.push(["Stocks · pullback delivery", "not running", "warn"]); else { const pend = Array.isArray(dl.pending) ? dl.pending.length : 0, op = Array.isArray(dl.open) ? dl.open.length : 0, stale = Array.isArray(dl.stale) ? dl.stale : []; out.push(["Stocks · pullback delivery", `${pend} pending alert${pend === 1 ? "" : "s"} · ${op} open delivery trade${op === 1 ? "" : "s"} · ${dl.history_to ? `daily history complete to ${dl.history_to}` : "daily history not complete"}`, stale.length || !dl.history_to ? "warn" : ""]); if (stale.length || !dl.history_to) next.push(`The daily history is stale${stale.length ? ` (${stale.slice(0, 3).join(", ")}${stale.length > 3 ? ` +${stale.length - 3}` : ""})` : ""}: top it up in Data.`); } out.push(["Next", next.length ? next.join(" ") : "nothing to do: the desk is watching", next.length ? "bad" : "ok"]); return out; };
const todayStrip = (d, tv, now) => `<div class="today-grid">${todayLines(d, tv, now).map(([k, v, c]) => `<div class="trow ${c}"><span>${esc(k)}</span><b>${esc(v)}</b></div>`).join("")}</div>`;
function renderToday() {
  const box = $("today-strip"); if (!box) return;
  const d = latest && latest.desk;
  box.innerHTML = todayStrip(d, acctSummary && acctSummary.token_validity, Date.now());
  if ($("today-updated")) $("today-updated").textContent = d && d.updated_at ? `updated ${hms(d.updated_at)}` : "";
}
const pbChip = (pb) => { if (!pb || typeof pb !== "object") return ""; const r = pb.reading, t = pb.trade, chip = (text, c = "", title = "") => `<span class="schip ${c}" title="${esc(title)}">${esc(text)}</span>`; if (t && t.status === "EXIT_PENDING") return chip("pullback 1d · exit at the next open", "short", t.exit_reason || ""); if (t && t.status === "OPEN") return chip(`pullback 1d · open ${t.sessions ?? "?"} of ${t.max_sessions ?? pb.max_sessions ?? "?"}`, "", t.warning || ""); if ((t && t.status === "PENDING_ENTRY") || (r && r.signal)) return chip("pullback 1d · BUY at the next open", "long", pb.evidence || ""); if (r) return chip(`pullback 1d · ${fmt(r.below_high_pct, 1)}% of ${fmt(r.needed_pct, 0)}%`, "", r.why || ""); return chip("pullback 1d · no reading", "err", pb.note || "no complete daily history"); };
const pbUnit = (pb) => `delivery: shares nearest ${rup(pb && pb.unit_inr != null ? pb.unit_inr : 10000)}`;
const pbUnitShort = (pb) => `delivery · about ${rup(pb && pb.unit_inr != null ? pb.unit_inr : 10000)}`;
function accRow(r, isOpen) {
  const L = r.last || {}, R = r.regime || {}, H = r.health || {}, K = r.risk || {};
  const chips = (r.pullback ? pbChip(r.pullback) : "") + (r.strategies || []).map(s => s.enabled === false ? `<span class="schip paused" title="paused">${esc(s.name)}@${esc(s.timeframe)}</span>` : `<span class="schip ${s.verdict !== "none" ? s.verdict : ""} ${s.error ? "err" : ""}" title="${esc(s.reason || "")}">${esc(s.name)}@${esc(s.timeframe)}</span>`).join("");
  const risk = (latest && latest.risk) || {}, LM = risk.limits || {};
  const body = `
    ${r.kind === "STOCK" ? "" : trendBlock(r.trend, L.price)}
    ${r.kind === "STOCK" ? pbBlock(r.pullback) : ""}
    <div class="grid3">
      <div><h4>Market</h4>${kv("Last", fmt(L.price))}${kv("Day change", spct(L.change_pct), cls(L.change_pct))}${kv("Prev close", fmt(L.prev_close))}
        ${kv("Last bar", hhmm(L.bar_ts))}${kv("Tracked as", esc(r.tracked))}${kv("Unit", r.pullback ? esc(pbUnit(r.pullback)) : esc(r.unit))}${r.note ? `<div class="hint">${esc(r.note)}</div>` : ""}</div>
      <div><h4>Regime &amp; volatility</h4>${kv("Regime", esc(R.regime || "–"))}${kv("Trend (EMA20/50)", esc(R.trend || "–"), R.trend === "up" ? "pos" : R.trend === "down" ? "neg" : "")}
        ${kv("ADX(14)", fmt(R.adx, 1))}${kv("ATR(14)", fmt(R.atr))}${kv("ATR percentile", fmt(R.atr_pct, 0))}${kv("RSI(14)", fmt(R.rsi, 1))}${kv("VWAP", fmt(R.vwap))}${kv("NIFTY bias", fmt(R.mkt_bias, 0))}</div>
      <div><h4>Data health</h4>${kv("State", hdot(H.state) + esc(H.state || "–"))}${kv("Source", esc(H.source || "–") + (H.delayed ? " (delayed ~15m)" : ""))}${kv("Resolution", `${H.resolution_min || "–"}m`)}
        ${kv("Bars held", H.bars || 0)}${kv("Bar age", L.age_sec != null ? Math.round(L.age_sec / 60) + " min" : "–")}<div class="hint">${esc(H.note || "")}${H.last_error ? "<br>" + esc(H.last_error) : ""}</div></div>
    </div>
    <h4>Strategies running</h4>
    <div class="tbl">${table([{ l: "Strategy", f: s => `<b>${esc(s.label)}</b> <span class="dim">${esc(s.name)}</span>` }, { l: "TF", k: "timeframe" }, { l: "Last bar", f: s => hhmm(s.last_bar_ts) },
      { l: "Verdict", f: s => `<span class="schip ${s.verdict !== "none" ? s.verdict : ""}">${esc(s.verdict)}</span>`, }, { l: "Why", f: s => esc(s.error || s.reason || "") },
      { l: "Trail", f: slotTrail, c: () => "nw" }, { l: "Strength", n: 1, f: s => fmt(s.strength, 0) }, { l: "Signals today", n: 1, k: "signals_today" }], r.strategies || [])}</div>
    ${(r.strategies || []).map(s => slotTrailNote(s, r.kind)).join("")}
    ${(r.recent || []).length ? `<h4>Recent signals</h4>` + table([{ l: "When", f: x => tsf(x.created_at) }, { l: "Side", k: "side" }, { l: "Strategy", f: x => `${esc(x.strategy)} ${esc(x.timeframe)}` }, { l: "Status", k: "status" }], r.recent) : ""}`;
  // .acc-top is the click target that opens the card: the head's grid, then the micro-trend strip as a row of its own so it wraps to the card's width.
  return `<div class="acc-row ${isOpen ? "open" : ""}" data-key="${esc(r.key)}">
    <div class="acc-top" data-toggle="${esc(r.key)}">
      <div class="acc-head">
        <div class="acc-name"><b>${esc(r.label)}</b><span class="dim">${r.kind === "INDEX" ? "index · options" : "stock"} · ${r.pullback ? esc(pbUnitShort(r.pullback)) : esc(r.unit)}</span></div>
        <div class="acc-price">${fmt(L.price)}<span class="${cls(L.change_pct)}">${spct(L.change_pct)}</span></div>
        <div class="acc-strats">${chips}</div>
        <div class="state-badge st-${esc(r.state)}">${esc(r.state)}</div>
        <div class="acc-caret">▾</div>
      </div>
      ${r.kind === "STOCK" ? "" : trendPips(r.trend)}
    </div>
    <div class="acc-body">${body}</div></div>`;
}
function renderDesk(d) {
  if (!d) { $("desk-source").textContent = "Live Desk not running (see Settings / log)"; return; }
  if (d.booting) { $("desk-source").textContent = "Live Desk starting — first bars arriving…"; return; }
  const now = Date.now();
  if (!desk.force && now - desk.rendered < 4000) return;
  desk.rendered = now; desk.force = false;
  const src = d.source || {}, ses = d.session || {};
  $("desk-source").textContent = `${src.name} · ${src.resolution} bars · ${delayNote(src)} · poll ${src.poll_seconds}s · ` +
    `${ses.open ? "market open" : (ses.trading_day ? "market closed" : "holiday / weekend")} · ${ses.entries_allowed ? "entries allowed" : "no new entries"} · updated ${hhmm(d.updated_at)}` +
    (src.last_error ? ` · ⚠ ${src.last_error.slice(0, 80)}` : "");
  $("desk-risk-line").textContent = `${(d.positions || []).length} open · each closes at its exit alert's price`;
  if ($("desk-updated")) $("desk-updated").textContent = `updated ${hms(d.updated_at)}`;
  // positions (re-render only when something changed)
  const psig = JSON.stringify((d.positions || []).map(p => [p.id, p.exit_advice, p.session_over, p.stop_underlying, p.last_underlying, p.unrealized, p.paper_status, p.user_entry_price, p.user_exit_price]));
  if (desk.sig.pos !== psig) {
    desk.sig.pos = psig;
    drawPositions("desk-positions", d.positions || []);
  }
  const ssig = JSON.stringify((d.signals || []).map(s => s.id));
  if (desk.sig.sig !== ssig) {
    desk.sig.sig = ssig;
    $("desk-signals").innerHTML = (d.signals || []).map(signalCard).join("");
    $("desk-signals").querySelectorAll("[data-take]").forEach(b => b.onclick = async () => {
      const f = $("dfill_" + b.dataset.take).value;
      const r = await api(`/api/desk/signal/${b.dataset.take}/ack`, { action: "TAKE", fill_price: f ? Number(f) : null, source: "dashboard" });
      toast(r.message, r.accepted ? "ok" : "warn"); desk.force = true;
    });
    $("desk-signals").querySelectorAll("[data-skip]").forEach(b => b.onclick = async () => {
      await api(`/api/desk/signal/${b.dataset.skip}/ack`, { action: "SKIP", source: "dashboard" }); desk.force = true;
    });
  }
  $("desk-context").innerHTML = (d.context || []).map(c => `<div class="tile ${c.manual ? "manual" : ""}" title="${esc(c.note || "")}"><small>${esc(c.label)}</small>
    <b>${c.last != null ? fmt(c.last, c.key === "USDINR" ? 3 : 2) : "–"} <span class="${cls(c.change_pct)}" style="font-size:14px">${spct(c.change_pct)}</span></b>
    <i>${c.last != null ? `${esc(c.source || "")} · ${hhmm(c.as_of)}` : (c.manual ? "manual slot — POST /api/desk/context" : "no data")}</i></div>`).join("");
  const draw = (el, rows) => {
    const have = new Map([...el.querySelectorAll(".acc-row")].map(x => [x.dataset.key, x]));
    rows.forEach(r => {
      const sig = JSON.stringify([r.state, r.last, r.health && r.health.state, r.strategies, r.signal && r.signal.id, r.position && [r.position.id, r.position.exit_advice, r.position.stop_underlying], r.risk, r.regime, r.trend]);
      const open = !!desk.open[r.key];
      const ex = have.get(r.key);
      if (ex && ex.dataset.sig === sig) { ex.classList.toggle("open", open); return; }
      const tmp = document.createElement("div"); tmp.innerHTML = accRow(r, open);
      const node = tmp.firstElementChild; node.dataset.sig = sig;
      if (ex) ex.replaceWith(node); else el.appendChild(node);
    });
  };
  draw($("desk-indices"), d.indices || []); draw($("desk-stocks"), d.stocks || []);
  // US-067: say how many slots are live and how many are paused; the delivery rule counts as one live slot a stock
  const dlv = d.delivery && d.delivery.enabled ? d.delivery : null;
  $("desk-idx-count").textContent = `${(d.indices || []).length} tracked · micro-trend rule`;
  $("desk-stk-count").textContent = `${(d.stocks || []).length} tracked · ${dlv ? "pullback rule (delivery)" : "pullback rule off"}`;
  const stkSub = $("desk-stk-sub");
  if (stkSub) stkSub.textContent = dlv ? `delivery · pullback rule · shares nearest ${rup(dlv.unit_inr != null ? dlv.unit_inr : 10000)} · carried for days` : "pullback rule · delivery · not running";
}
document.addEventListener("click", (e) => {
  const h = e.target.closest("[data-toggle]"); if (!h) return;
  const k = h.dataset.toggle; desk.open[k] = !desk.open[k]; store.set("desk.open", desk.open);
  h.parentElement.classList.toggle("open", desk.open[k]);
});
setInterval(() => {
  document.querySelectorAll("[data-exp]").forEach(el => {
    const left = Math.max(0, Math.round((new Date(el.dataset.exp) - Date.now()) / 1000));
    el.textContent = left ? `valid ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}` : "expired";
    el.classList.toggle("urgent", left < 60);
  });
  if (!latest || !latest.active_signal) return;
  latest.signal_seconds_remaining = Math.max(0, (latest.signal_seconds_remaining || 0) - 1);
  const el = $("cd"); if (el) { const v = Math.round(latest.signal_seconds_remaining); el.textContent = v + "s"; el.classList.toggle("urgent", v < 20); }
}, 1000);

/* ── stream ──────────────────────────────────────────────────────────── */
let lastDesk = null, lastDeskVersion = -1;
function applyStream(payload) {
  latest = payload.state;
  if (latest.desk && latest.desk.unchanged) latest.desk = lastDesk; else if (latest.desk) lastDesk = latest.desk;
  renderBadges(latest); renderWarnings(latest); renderToday(); renderSignal(latest); renderPositions(latest);
  renderPanels(latest); renderJournal(latest); renderFeed(payload.notifications);
  const changed = latest.desk && latest.desk.version !== lastDeskVersion;
  if (changed) lastDeskVersion = latest.desk.version;
  if (document.querySelector('[data-tab="live"]').classList.contains("active")) { renderDesk(latest.desk); if (changed) { renderFocus(latest.desk); wantPaper(false); } loadFillCosts(false).catch(() => {}); }
  if (changed && document.querySelector('[data-tab="home"]').classList.contains("active")) { renderHomePositions(latest.desk); if (Date.now() - alerts.loadedAt > 15000) { alerts.loadedAt = Date.now(); loadAlerts(true).catch(() => {}); } }
}
function connect() {
  const es = new EventSource("/api/stream");
  es.onmessage = e => { $("conn").textContent = " · live"; try { applyStream(JSON.parse(e.data)); } catch (err) {} };
  es.onerror = () => { $("conn").textContent = " · reconnecting…"; es.close(); setTimeout(connect, 3000); };
}
setInterval(renderGates, 30000);
$("haltBtn").onclick = () => { if (confirm("Halt trading? No new signals will fire until you clear it.")) api("/api/risk/halt", {}); };
$("resetBtn").onclick = () => { if (confirm("Clear the halt and resume signalling?")) api("/api/risk/reset", {}); };
/* ── instrument focus: one instrument, everything, plus its chart with signal markers ── */
const focus = { key: store.get("focus.key", "BANKNIFTY"), loading: false, lastKey: null, lastTf: null, lastAt: 0 };
function renderFocus(d) {
  if (!d || d.booting) return;
  const rows = [...(d.indices || []), ...(d.stocks || [])];
  const sel = $("focus-key");
  if (sel.options.length !== rows.length) {
    sel.innerHTML = rows.map(r => `<option value="${esc(r.key)}" ${r.key === focus.key ? "selected" : ""}>${esc(r.label)} (${r.kind === "INDEX" ? "options" : "stock"})</option>`).join("");
  }
  const r = rows.find(x => x.key === focus.key) || rows[0]; if (!r) return;
  const L = r.last || {}, R = r.regime || {}, H = r.health || {}, K = r.risk || {}, risk = d.risk || {}, LM = risk.limits || {};
  $("focus-body").innerHTML = `
    <div class="row between"><div class="acc-name"><b>${esc(r.label)}</b><span class="dim">${r.kind === "INDEX" ? "index · options" : "stock"} · ${r.pullback ? esc(pbUnitShort(r.pullback)) : esc(r.unit)} · tracked as ${esc(r.tracked)}</span></div><div class="state-badge st-${esc(r.state)}">${esc(r.state)}</div></div>
    ${r.kind === "STOCK" ? "" : trendBlock(r.trend, L.price)}
    <div class="grid3">
      <div><h4>Market</h4>${kv("Last", fmt(L.price) + (L.live ? " <span class='pos'>●</span>" : ""))}${kv("Day change", spct(L.change_pct), cls(L.change_pct))}${kv("Prev close", fmt(L.prev_close))}${kv("Last bar", hhmm(L.bar_ts))}${L.live ? kv("Bid / ask", `${fmt(L.live.bid)} / ${fmt(L.live.ask)}`) : ""}</div>
      <div><h4>Regime &amp; volatility</h4>${kv("Regime", esc(R.regime || "–"))}${kv("Trend", esc(R.trend || "–"), R.trend === "up" ? "pos" : R.trend === "down" ? "neg" : "")}${kv("ADX(14)", fmt(R.adx, 1))}${kv("ATR(14)", fmt(R.atr))}${kv("ATR pct", fmt(R.atr_pct, 0))}${kv("RSI(14)", fmt(R.rsi, 1))}${kv("VWAP", fmt(R.vwap))}</div>
      <div><h4>Data health</h4>${kv("State", hdot(H.state) + esc(H.state || "–"))}${kv("Source", esc(H.source || "–") + (H.delayed ? " (~15m delayed)" : ""))}${kv("Bars", `${H.bars || 0} × ${H.resolution_min || "–"}m`)}${kv("Bar age", L.age_sec != null ? Math.round(L.age_sec / 60) + " min" : "–")}<div class="hint">${esc(H.note || "")}</div></div>
    </div>
    <h4>Strategies</h4><div class="tbl">${table([{ l: "Strategy", f: s => `<b>${esc(s.label)}</b>` }, { l: "TF", k: "timeframe" }, { l: "Bar", f: s => hhmm(s.last_bar_ts) },
      { l: "Verdict", f: s => `<span class="schip ${s.verdict !== "none" ? s.verdict : ""}">${esc(s.verdict)}</span>` }, { l: "Why", f: s => esc(s.error || s.reason || "") }, { l: "Trail", f: slotTrail, c: () => "nw" }], r.strategies || [])}</div>
    ${(r.strategies || []).map(s => slotTrailNote(s, r.kind)).join("")}
    ${r.signal ? `<div class="notice mt"><b>Pending alert:</b> ${esc(r.signal.side.replace("_", " "))} · ${esc(r.signal.strategy)} ${esc(r.signal.timeframe)} · conf ${fmt(confShown((r.signal.confidence || {}).score), 0)} — act on it from <a href="#home">Home</a>.</div>` : ""}
    ${r.position ? `<div class="notice mt"><b>Monitoring:</b> ${esc(r.position.side.replace("_", " "))} from ${fmt(r.position.entry_price)} · ${fmt(r.position.r_now, 2)}R · ${esc(r.position.hold_check || "")}${r.position.exit_advice ? ` · <b class="neg">EXIT: ${esc(r.position.exit_advice)}</b>` : ""}</div>` : ""}`;
  const tf = $("focus-tf").value;
  if (focus.lastKey !== r.key || focus.lastTf !== tf || Date.now() - focus.lastAt > 55000) {
    focus.lastKey = r.key; focus.lastTf = tf; focus.lastAt = Date.now();
    api(`/api/desk/bars?key=${encodeURIComponent(r.key)}&timeframe=${tf}&n=400`).then(b => {
      if (!C() || !b.bars.length) { $("focus-chart-note").textContent = "no bars yet"; return; }
      const trades = (b.markers || []).map(m => ({ entry_ts: m.ts, exit_ts: m.ts, entry: m.entry, exit: null, side: m.side === "long" ? "LONG" : "SHORT", net_pnl: (m.outcome === "TARGET") ? 1 : (m.outcome === "STOP" ? -1 : 0), r: null, reason: m.strategy }));
      C().candles($("focus-chart"), b.bars.map(x => x.slice(0, 5)), trades.map(t => ({ ...t, exit_ts: t.entry_ts })), { fit: "last" });
      $("focus-chart-note").textContent = `${b.bars.length} × ${b.timeframe} bars · ${b.source}${b.live ? " · live tick " + hhmm(b.live.ts) : ""} · ▲▼ = alerts raised on this instrument`;
    }).catch(() => {});
  }
}
$("focus-key").onchange = () => { focus.key = $("focus-key").value; store.set("focus.key", focus.key); if (latest) renderFocus(latest.desk); };
$("focus-tf").onchange = () => { if (latest) renderFocus(latest.desk); };
tabLoaders.live = () => { renderGates(); desk.force = true; wantPaper(true); loadFillCosts(true).catch(() => {}); if (latest) { renderDesk(latest.desk); focus.lastAt = 0; renderFocus(latest.desk); } };

/* ═══════════════════════════════ LAB ════════════════════════════════ */
const lab = { strategies: [], instruments: [], focus: [], focusTfs: [], primary: [], cat: store.get("lab.cat", "DESK"), runs: [], waiting: null, poll: null, shown: null };
const COMMON_EXITS = new Set(["stop_atr_min", "stop_atr_max", "min_risk_pct", "stop_datr_min", "max_risk_pct", "target_r", "trail_atr_mult",
  "trail_atr_period", "trail_activate_r", "partial_r", "partial_frac", "breakeven_after_partial", "max_bars", "exit_time"]);
const COMMON_FILTERS = new Set(["allow_long", "allow_short", "entry_start", "entry_end", "min_strength", "market_filter", "regime_filter",
  "cooldown_bars", "max_trades_per_day", "timeframe"]);
const TF_CANDIDATES = ["1m", "2m", "3m", "4m", "5m", "6m", "10m", "12m", "15m", "20m", "30m", "45m", "60m", "120m", "1d"];
const mins = (tf) => tf === "1d" ? 375 : parseInt(tf, 10);

async function loadLab() {
  if (!lab.strategies.length) {
    const [s, i, f] = await Promise.all([api("/api/lab/strategies"), api("/api/lab/instruments"), api("/api/lab/focus")]);
    lab.strategies = s.strategies; lab.instruments = i.instruments.filter(x => x.tradeable);
    lab.focus = f.instruments; lab.focusTfs = f.timeframes; lab.primary = f.primary_strategies || [];
    renderCats(); renderInstruments(); renderStrategies(); onStrategyChange(); renderSweepForm();
  }
  refreshRuns();
}
function renderCats() {
  const cats = [["DESK", "Desk watchlist"], ["OPTIONS", "Options"], ["FUTURES", "Futures"], ["STOCK", "Stocks"], ["CURRENCY", "Currency"]];
  $("lab-cats").innerHTML = cats.map(([c, l]) => `<span class="chip ${c === lab.cat ? "active" : ""}" data-cat="${c}">${l}</span>`).join("");
  $("lab-cats").querySelectorAll(".chip").forEach(ch => ch.onclick = () => { lab.cat = ch.dataset.cat; store.set("lab.cat", lab.cat); renderCats(); renderInstruments(); onStrategyChange(); });
}
function labList() {
  if (lab.cat === "DESK") { const order = new Map(lab.focus.map((k, i) => [k, i])); return lab.instruments.filter(i => order.has(i.key)).sort((a, b) => order.get(a.key) - order.get(b.key)); }
  return lab.instruments.filter(i => i.category === lab.cat);
}
function renderInstruments() {
  const list = labList();
  const prev = store.get("lab.inst." + lab.cat, null);
  $("lab-instrument").innerHTML = list.map(i => `<option value="${i.key}" ${i.key === prev ? "selected" : ""}>${esc(i.label)}${lab.cat === "DESK" ? ` (${i.category.toLowerCase()})` : ""}</option>`).join("");
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
  const prevTf = store.get("lab.tf", "15m");
  $("lab-tf").innerHTML = (allowed.length ? allowed : ["5m"]).map(tf => `<option ${tf === prevTf ? "selected" : ""}>${tf}</option>`).join("");
  if (cov["1d"]) { const c = Object.values(cov); const first = c.map(x => x.first).sort()[0]; $("lab-start").min = first; }
  renderStrategies();
}
function renderStrategies() {
  const prev = store.get("lab.strategy", $("lab-strategy").value || "ema_cross");
  const inst = currentInstrument(), cat = inst ? inst.category : lab.cat;
  $("lab-strategy").innerHTML = lab.strategies.map(s => {
    const sug = (s.suitable_for || []).includes(cat) ? " ★" : "";
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
    cost_multiplier: parseFloat($("lab-costx").value) || 1, sentiment_mode: $("lab-sent").value || "off" };
}
$("lab-instrument").onchange = onInstrumentChange;
$("lab-strategy").onchange = () => onStrategyChange(false);
$("lab-reset").onclick = () => { onStrategyChange(true); toast("Parameters reset to defaults", "ok"); };
async function submit(kind) {
  const body = labBody();
  let r;
  if (kind === "single") r = await api("/api/lab/backtest", body);
  else {
    const win = ($("lab-wfwin").value || "").split("/").map(x => parseInt(x, 10)).filter(x => !Number.isNaN(x));
    r = await api("/api/lab/optimize", { ...body, kind, trials: parseInt($("lab-trials").value, 10) || 40,
      is_days: kind === "walkforward" && win.length === 2 ? win[0] : null, oos_days: kind === "walkforward" && win.length === 2 ? win[1] : null });
  }
  lab.waiting = r.run_id; lab.shown = null;
  $("lab-status").innerHTML = `run <b>#${r.run_id}</b> queued… <span class="muted">(results are saved; see Runs)</span>`;
  showSub("result");
  $("lab-result").innerHTML = `<div class="card empty"><svg class="mascot lg bounce"><use href="#m-ichi"/></svg><div><b>Run #${r.run_id} is working…</b><br><span class="muted" id="lab-progress">queued</span></div></div>`;
  startPoll();
}
$("lab-run").onclick = () => submit("single").catch(() => {});
$("lab-opt").onclick = () => submit("optimize").catch(() => {});
$("lab-wf").onclick = () => submit("walkforward").catch(() => {});
function startPoll() { if (lab.poll) return; lab.poll = setInterval(refreshRuns, 2000); refreshRuns(); }
async function refreshRuns() {
  const r = await api("/api/lab/runs?limit=80"); lab.runs = r.runs;
  renderRuns(); renderSweeps();
  const busy = lab.runs.some(x => x.status === "RUNNING" || x.status === "QUEUED");
  if (!busy && lab.poll) { clearInterval(lab.poll); lab.poll = null; }
  if (busy && !lab.poll) lab.poll = setInterval(refreshRuns, 2000);
  if (lab.waiting) {
    const w = lab.runs.find(x => x.id === lab.waiting);
    if (w && (w.status === "RUNNING" || w.status === "QUEUED")) { const p = $("lab-progress"); if (p) p.textContent = `${Math.round((w.progress || 0) * 100)}% · ${w.message || ""}`; }
    if (w && w.status === "DONE") { lab.waiting = null; $("lab-status").innerHTML = `run <b>#${w.id}</b> done · ${esc(w.message || "")}`; showRun(w.id); }
    if (w && w.status === "FAILED") { lab.waiting = null; $("lab-status").innerHTML = `<span class="neg">run #${w.id} failed</span>`; $("lab-result").innerHTML = `<div class="card"><div class="empty"><svg class="mascot lg"><use href="#m-cow-sweat"/></svg><div><b>Run #${w.id} failed.</b><br>${esc(w.message)}</div></div></div>`; }
  }
}
function renderRuns() {
  const rows = lab.runs.filter(x => x.kind !== "sweep" || true);
  $("lab-runs").innerHTML = table([{ l: "#", k: "id", n: 1 }, { l: "Kind", f: x => esc(x.kind) }, { l: "Label", f: x => esc(x.label || "") },
    { l: "Status", f: x => x.status === "RUNNING" ? `RUNNING <div class="progress"><i style="width:${Math.round((x.progress || 0) * 100)}%"></i></div>` : esc(x.status), c: x => x.status === "FAILED" ? "neg" : (x.status === "DONE" ? "pos" : "") },
    { l: "Trades", n: 1, f: x => x.metrics && x.metrics.trades != null ? x.metrics.trades : (x.metrics && x.metrics.done != null ? `${x.metrics.done}/${x.metrics.combos}` : "–") },
    { l: "Net ₹", n: 1, f: x => x.metrics && x.metrics.net_pnl != null ? rup(x.metrics.net_pnl) : "–", c: x => x.metrics ? cls(x.metrics.net_pnl) : "" },
    { l: "PF", n: 1, f: x => x.metrics && x.metrics.profit_factor != null ? fmt(x.metrics.profit_factor) : "–" },
    { l: "Exp R", n: 1, f: x => x.metrics && x.metrics.expectancy_r != null ? fmt(x.metrics.expectancy_r, 3) : "–" },
    { l: "When", f: x => tsf(x.created_at) }], rows, { empty: "No runs yet.", rowClass: x => "click" + (x.id === lab.shown ? " sel" : "") });
  $("lab-runs").querySelectorAll("tr.click").forEach(tr => tr.onclick = () => { const run = rows[+tr.dataset.i]; if (run.status === "DONE" || run.status === "FAILED") { showSub("result"); showRun(run.id); } });
}
async function showRun(id) {
  const r = await api(`/api/lab/runs/${id}`);
  lab.shown = id; renderRuns();
  const el = $("lab-result"); el.dataset.run = String(id);
  if (r.status === "FAILED") { el.innerHTML = `<div class="card"><div class="empty"><svg class="mascot lg"><use href="#m-cow-sweat"/></svg><div><b>Run #${id} failed.</b><br>${esc(r.message || "")}</div></div></div>`; return; }
  if (r.status !== "DONE") { el.innerHTML = `<div class="card"><h2>Run #${id}</h2><div class="muted">${esc(r.status)} ${esc(r.message || "")}</div></div>`; return; }
  if (r.kind === "single") renderBacktest(r); else if (r.kind === "sweep") renderSweepResult(r); else renderOpt(r);
}
function verdictOf(m) {
  if ((m.trades || 0) < 20) return ["meh", "TOO FEW TRADES", "m-cow"];
  if (m.profit_factor >= 1.2 && m.expectancy_r > 0.05) return ["good", "POSITIVE", "m-cow-cheer"];
  if (m.profit_factor > 1) return ["meh", "MARGINAL", "m-cow"];
  return ["bad", "NEGATIVE", "m-cow-sweat"];
}
function renderBacktest(r) {
  const m = r.metrics || {}, a = r.artifacts || {}, cfg = r.config || {}, tp = m.thirty_pct_rule || {};
  const el = $("lab-result");
  const [vc, vt, vm] = verdictOf(m);
  const sent = cfg.sentiment_mode && cfg.sentiment_mode !== "off" ? ` · sentiment ${esc(cfg.sentiment_mode)} (${m.sentiment_blocked_long || 0}/${m.sentiment_blocked_short || 0} blocked, ${m.sentiment_scores_used || 0} scores)` : "";
  el.innerHTML = `
    <div class="card">
      <div class="row between"><h2>#${r.id} · ${esc(cfg.strategy)} on ${esc(cfg.instrument_key)} · ${esc(cfg.timeframe)}
        <small>${esc(m.category)} · unit ${m.unit} · ${esc(m.pricing)}${m.costs_unverified ? " · costs unverified" : ""}${sent}</small></h2>
        <span class="row"><svg class="mascot"><use href="#${vm}"/></svg><span class="verdict ${vc}">${vt}</span></span></div>
      <div class="tiles">
        ${tile("Net P&L", rup(m.net_pnl), "after all charges", cls(m.net_pnl))}
        ${tile("Trades", fmt(m.trades, 0), `${m.long_trades || 0} long / ${m.short_trades || 0} short`)}
        ${tile("Win rate", pct(m.win_rate), `payoff ${fmt(m.payoff)}`)}
        ${tile("Profit factor", fmt(m.profit_factor), `expectancy ${fmt(m.expectancy_r, 3)}R`)}
        ${tile("Expectancy", rup(m.expectancy), "per trade", cls(m.expectancy))}
        ${tile("Max drawdown", rup(m.max_drawdown_inr), pct(m.max_drawdown_pct) + " of ref capital", "neg")}
        ${tile("Sharpe", fmt(m.sharpe), m.sharpe_note || `sortino ${fmt(m.sortino)}`)}
        ${tile("Costs", rup(m.costs), `${pct(m.cost_share_pct, 0)} of gross moves`)}
      </div>
      ${tp.payoff !== undefined ? `<div class="notice"><b>30 % rule</b> · payoff ${fmt(tp.payoff)} (need ≥ ${fmt(tp.payoff_needed_for_30pct)}) · break-even win rate ${pct(tp.breakeven_win_rate)} ·
        PF at a 30 % win rate <b class="${tp.pf_at_30pct_win_rate >= 1.1 ? "pos" : "neg"}">${fmt(tp.pf_at_30pct_win_rate)}</b> — ${esc(tp.verdict)}</div>` : ""}
      <h3>Underlying with entries (▲▼ B/S) and exits (● R) <small>scroll to zoom · click a trade below to focus it</small></h3>
      <div id="plot-px" class="plot tall"></div>
      <div class="grid2">
        <div><h3>Cumulative net P&L (₹) and drawdown</h3><div id="plot-eq" class="plot"></div></div>
        <div><h3>Monthly net P&L (₹)</h3><div id="plot-month" class="plot"></div></div>
      </div>
    </div>
    <div class="grid2">
      <div class="card"><h2>By exit reason</h2><div class="scroll">${bdTable(a.breakdowns && a.breakdowns.exit_reason, "exit_reason")}</div>
        <h2 class="mt">By side</h2><div class="scroll">${bdTable(a.breakdowns && a.breakdowns.side, "side")}</div></div>
      <div class="card"><h2>By hour</h2><div class="scroll">${bdTable(a.breakdowns && a.breakdowns.hour, "hour")}</div>
        <h2 class="mt">By weekday</h2><div class="scroll">${bdTable(a.breakdowns && a.breakdowns.weekday, "weekday")}</div></div>
    </div>
    <div class="card"><h2>Trades <small>${(r.trades || []).length} · click a row to focus it on the chart</small></h2><div class="scroll" style="max-height:420px;overflow:auto" id="lab-trades">${tradesTable(r.trades || [])}</div></div>
    <div class="card"><details><summary class="muted">Configuration · ${Object.keys(cfg.params || {}).length ? "custom parameters" : "default parameters"}</summary><pre class="code">${esc(JSON.stringify(cfg, null, 2))}</pre></details></div>`;
  const ch = a.chart || {};
  if (!a.chart && $("plot-px")) $("plot-px").insertAdjacentHTML("afterend", `<div class="hint">Runs made by a sweep keep no price chart (it saves gigabytes). Press <b>Run backtest</b> with this configuration to draw one.</div>`);
  if (C()) {
    C().candles($("plot-px"), ch.candles || [], ch.trades || [], { fit: "last" });
    C().equity($("plot-eq"), a.equity || [], cfg.reference_capital || 100000);
    C().bars($("plot-month"), (a.monthly || []).map(x => x.month), (a.monthly || []).map(x => x.pnl));
  }
  const ctr = ch.trades || [];
  $("lab-trades").querySelectorAll("tr").forEach((tr, i) => { if (i === 0) return; tr.classList.add("click"); tr.onclick = () => { const t = ctr[i - 1]; if (t && C()) { C().focusTrade($("plot-px"), t); window.scrollTo({ top: $("plot-px").getBoundingClientRect().top + window.scrollY - 80, behavior: "smooth" }); } }; });
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
function renderOpt(r) {
  const a = r.artifacts || {}, cfg = r.config || {}, el = $("lab-result");
  const best = a.best_params || {};
  const applyBtn = `<button class="btn small" id="apply-best">Apply best params to the form</button>`;
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
    const o = a.oos_metrics || {}, d = a.default_oos_metrics || {}, plan = a.plan || {};
    const v = (o.trades || 0) >= 30 && o.profit_factor >= 1.2 && o.expectancy_r >= 0.05 ? ["good", "PASS-LIKE OOS", "m-cow-cheer"] : ((o.trades || 0) >= 15 && o.expectancy_r > 0 && o.profit_factor > 1 ? ["meh", "WATCH", "m-cow"] : ["bad", "FAIL", "m-cow-sweat"]);
    const better = (o.expectancy_r || 0) > (d.expectancy_r || 0);
    el.innerHTML = `<div class="card"><div class="row between"><h2>#${r.id} · walk-forward · ${esc(cfg.strategy)} on ${esc(cfg.instrument_key)} ${esc(cfg.timeframe)}
      <small>only the out-of-sample folds count</small></h2><span class="row"><svg class="mascot"><use href="#${v[2]}"/></svg><span class="verdict ${v[0]}">${v[1]}</span></span></div>
      <div class="plan">${esc(plan.note || "")}${plan.sessions ? ` · ${plan.sessions} sessions (${esc(plan.first)} → ${esc(plan.last)})` : ""}</div>
      <div class="tiles">${tile("OOS trades", fmt(o.trades, 0), `${plan.folds || (a.folds || []).length} folds`)}${tile("OOS win %", pct(o.win_rate))}${tile("OOS PF", fmt(o.profit_factor))}
        ${tile("OOS exp R", fmt(o.expectancy_r, 3), "", cls(o.expectancy_r))}${tile("OOS net ₹", rup(o.net_pnl), "", cls(o.net_pnl))}${tile("OOS max DD", rup(o.max_drawdown_inr), "", "neg")}
        ${tile("Untuned OOS exp R", fmt(d.expectancy_r, 3), `PF ${fmt(d.profit_factor)} · same windows`, cls(d.expectancy_r))}</div>
      <div class="notice"><b>Reading it:</b> each fold tunes on the in-sample window and is then judged on the next, unseen window. ${better ? "Tuned parameters beat the untuned defaults out of sample — the tuning carries some information." : "Tuned parameters did <b>not</b> beat the untuned defaults out of sample — the optimizer is fitting noise on this data; keep the defaults."}
        ${(o.trades || 0) < 30 ? " Fewer than 30 OOS trades: treat every number as provisional until Dhan's 5-year history is loaded." : ""}</div>
      <div class="notice"><b>Last fold's parameters</b> <code>${esc(JSON.stringify(best))}</code> ${applyBtn}</div>
      <h3>Folds</h3><div class="scroll">${table([{ l: "#", k: "fold", n: 1 }, { l: "In-sample", k: "is" }, { l: "OOS", k: "oos" }, { l: "IS PF", n: 1, f: x => fmt(x.is_pf) },
        { l: "OOS trades", n: 1, k: "oos_trades" }, { l: "OOS PF", n: 1, f: x => fmt(x.oos_pf) }, { l: "OOS exp R", n: 1, f: x => fmt(x.oos_exp_r, 3), c: x => cls(x.oos_exp_r) },
        { l: "Untuned OOS PF", n: 1, f: x => fmt(x.default_oos_pf) }], a.folds || [])}</div></div>`;
  }
  const b = $("apply-best"); if (b) b.onclick = () => { store.set("lab.params." + cfg.strategy, best); $("lab-strategy").value = cfg.strategy; onStrategyChange(false); toast("Parameters applied to the form (also pasteable into config/desk.yaml)", "ok"); window.scrollTo(0, 0); };
}
function renderSweepResult(r) {
  const a = r.artifacts || {}, m = r.metrics || {}, cfg = r.config || {};
  $("lab-result").innerHTML = `<div class="card"><h2>#${r.id} · sweep <small>${m.done || 0}/${m.combos || cfg.n} runs · ${m.failed || 0} failed · ${m.ranked || 0} ranked · ${m.positive || 0} positive${m.stopped ? " · stopped early" : ""}</small></h2>
    <div class="notice">Every row is a saved run — click one to open it. The <b>Leaderboard</b> tab ranks all sweeps together.</div>
    <h3>Best per instrument</h3><div class="scroll">${boardTable(Object.values(a.best_by_instrument || {}))}</div>
    <h3>Best per strategy</h3><div class="scroll">${boardTable(Object.values(a.best_by_strategy || {}))}</div>
    <h3>All ranked runs</h3><div class="scroll" style="max-height:520px;overflow:auto">${boardTable(a.leaderboard || [])}</div></div>`;
  wireBoard($("lab-result"));
}
function boardTable(rows) {
  return table([{ l: "#", k: "run_id", n: 1 }, { l: "Strategy", k: "strategy" }, { l: "Instrument", k: "instrument" }, { l: "TF", k: "timeframe" },
    { l: "Kind", f: r => r.oos ? "<span class='oos'>OOS</span>" : "IS", c: r => r.oos ? "oos" : "" }, { l: "Trades", n: 1, k: "trades" }, { l: "Win %", n: 1, f: r => pct(r.win_rate) },
    { l: "Profit %", n: 1, f: r => r.profit_pct == null ? "–" : spct(r.profit_pct, 2), c: r => cls(r.profit_pct) },
    { l: "PF", n: 1, f: r => fmt(r.profit_factor) }, { l: "Exp R", n: 1, f: r => fmt(r.expectancy_r, 3), c: r => cls(r.expectancy_r) }, { l: "Net ₹", n: 1, f: r => rup(r.net_pnl), c: r => cls(r.net_pnl) },
    { l: "Max DD ₹", n: 1, f: r => rup(r.max_drawdown_inr) }, { l: "Score", n: 1, f: r => r.score == null ? "<span class='dim'>unranked</span>" : fmt(r.score, 2), c: r => cls(r.score) },
    { l: "Sent.", f: r => r.sentiment_mode && r.sentiment_mode !== "off" ? esc(r.sentiment_mode) : "" }], rows, { empty: "No ranked runs yet — run a sweep.", rowClass: () => "lead" });
}
function wireBoard(root) {
  root.querySelectorAll("tr.lead").forEach(tr => tr.onclick = () => { const id = parseInt(tr.children[0].textContent, 10); if (id) { showSub("result"); showRun(id); } });
}
async function loadBoard() {
  const q = new URLSearchParams(); if ($("board-kind").value) q.set("kind", $("board-kind").value); if ($("board-cat").value) q.set("category", $("board-cat").value);
  q.set("min_trades", $("board-min").value || 10);
  q.set("sort", $("board-sort").value || "score"); q.set("order", $("board-order").dataset.order === "asc" ? "asc" : "desc");
  if ($("board-strats").value === "primary" && lab.primary && lab.primary.length) q.set("strategies", lab.primary.join(","));
  store.set("board.view", { sort: $("board-sort").value, order: $("board-order").dataset.order, strats: $("board-strats").value });
  const r = await api("/api/lab/leaderboard?" + q.toString());
  const s = r.summary || {};
  $("board-summary").innerHTML = tile("Saved runs", s.runs || 0) + tile("Ranked", s.ranked || 0, `≥ ${$("board-min").value} trades`) + tile("Positive", s.positive || 0, "PF ≥ 1.2 & score > 0", s.positive ? "pos" : "") +
    tile("Instruments covered", Object.keys(s.best_by_instrument || {}).length) + tile("Strategies covered", Object.keys(s.best_by_strategy || {}).length);
  $("lab-board").innerHTML = boardTable(r.rows || []);
  wireBoard($("lab-board"));
}
$("board-refresh").onclick = () => loadBoard().catch(() => {});
["board-kind", "board-cat", "board-sort", "board-strats"].forEach(id => $(id).onchange = () => loadBoard().catch(() => {}));
// Leaderboard order: one button flips high-first / low-first (owner feedback 2026-11-07).
const boardOrderLabel = (o) => o === "asc" ? "↑ low first" : "↓ high first";
$("board-order").onclick = () => { const b = $("board-order"); b.dataset.order = b.dataset.order === "asc" ? "desc" : "asc"; b.textContent = boardOrderLabel(b.dataset.order); loadBoard().catch(() => {}); };
(() => { const v = store.get("board.view", null); if (!v) return; if (v.sort) $("board-sort").value = v.sort; if (v.strats != null) $("board-strats").value = v.strats; if (v.order === "asc" || v.order === "desc") { $("board-order").dataset.order = v.order; $("board-order").textContent = boardOrderLabel(v.order); } })();
function renderSweepForm() {
  const chips = (el, items, key, labelOf) => {
    const sel = new Set(store.get("sweep." + key, items));
    el.innerHTML = items.map(x => `<span class="chip ${sel.has(x) ? "active" : ""}" data-v="${esc(x)}">${esc(labelOf ? labelOf(x) : x)}</span>`).join("");
    el.querySelectorAll(".chip").forEach(c => c.onclick = () => { c.classList.toggle("active"); store.set("sweep." + key, [...el.querySelectorAll(".chip.active")].map(z => z.dataset.v)); sweepPlan(); });
  };
  chips($("sweep-strats"), lab.strategies.map(s => s.name), "strats", x => (lab.strategies.find(s => s.name === x) || {}).label || x);
  chips($("sweep-insts"), lab.focus, "insts");
  chips($("sweep-tfs"), lab.focusTfs, "tfs");
  sweepPlan();
}
const sweepSel = (id) => [...$(id).querySelectorAll(".chip.active")].map(z => z.dataset.v);
function sweepPlan() { const n = sweepSel("sweep-strats").length * sweepSel("sweep-insts").length * sweepSel("sweep-tfs").length * ($("sweep-presets").checked ? 3 : 1); $("sweep-plan").textContent = `${n} runs · roughly ${Math.ceil(n * 2 / 60)} min on the Yahoo corpus, several hours on 5 years of Dhan 1-minute bars`; }
$("sweep-presets").onchange = sweepPlan;
async function loadMatrix() {
  const q = new URLSearchParams(); if ($("matrix-sweep").value) q.set("sweep_id", $("matrix-sweep").value); q.set("min_trades", $("matrix-min").value || 5);
  const sweeps = lab.runs.filter(x => x.kind === "sweep" && x.status === "DONE");
  const cur = $("matrix-sweep").value;
  $("matrix-sweep").innerHTML = `<option value="">all saved runs</option>` + sweeps.map(x => `<option value="${x.id}" ${String(x.id) === cur ? "selected" : ""}>sweep #${x.id} · ${esc((x.label || "").slice(0, 40))}</option>`).join("");
  const m = await api("/api/lab/matrix?" + q.toString());
  const strats = m.strategies || [];
  const positive = strats.reduce((a, s) => a + (s.positive || 0), 0);
  $("matrix-summary").innerHTML = tile("Ranked runs", m.runs) + tile("Strategies", strats.length) + tile("Positive cells", positive, "PF ≥ 1.2 & score > 0", positive ? "pos" : "") +
    tile("Best strategy", strats[0] && strats[0].best ? esc(strats[0].strategy) : "–", strats[0] && strats[0].best ? `${esc(strats[0].best.instrument)} ${esc(strats[0].best.timeframe)} ${esc(strats[0].best.preset)} · score ${fmt(strats[0].best.score, 2)}` : "");
  $("lab-matrix").innerHTML = strats.length ? strats.map((s, i) => `<details class="mx-strat" ${i < 2 ? "open" : ""}><summary><b>${esc(s.strategy)}</b>
      <span class="muted">${s.instruments.length} instruments · ${s.positive} positive · best ${s.best ? `${esc(s.best.instrument)} ${esc(s.best.timeframe)} ${esc(s.best.preset)} (win ${pct(s.best.win_rate)}, exp ${fmt(s.best.expectancy_r, 3)}R, score ${fmt(s.best.score, 2)})` : "–"}</span></summary>
    ${s.instruments.map(ir => `<div class="mx-inst"><h4>${esc(ir.instrument)} <span class="dim">${esc(ir.category)}</span>${ir.best ? `<span class="best">best: ${esc(ir.best.timeframe)} ${esc(ir.best.preset)} · score ${fmt(ir.best.score, 2)}</span>` : ""}</h4>
      <div class="scroll">${table([{ l: "#", k: "run_id", n: 1 }, { l: "TF", k: "timeframe" }, { l: "Setting", k: "preset" }, { l: "Trades", n: 1, k: "trades" }, { l: "Win %", n: 1, f: r => pct(r.win_rate) },
        { l: "PF", n: 1, f: r => fmt(r.profit_factor) }, { l: "Exp R", n: 1, f: r => fmt(r.expectancy_r, 3), c: r => cls(r.expectancy_r) }, { l: "Net ₹", n: 1, f: r => rup(r.net_pnl), c: r => cls(r.net_pnl) },
        { l: "Max DD ₹", n: 1, f: r => rup(r.max_drawdown_inr) }, { l: "Score", n: 1, f: r => r.score == null ? "<span class='dim'>–</span>" : fmt(r.score, 2), c: r => (ir.best && r.run_id === ir.best.run_id) ? "best-row" : cls(r.score) }],
        ir.rows, { rowClass: () => "lead" })}</div></div>`).join("")}</details>`).join("") : `<div class="empty"><svg class="mascot lg"><use href="#m-cow"/></svg><div><b>No ranked runs yet.</b><br>Run a sweep (Sweep tab) — with "three settings per strategy" ticked you get the full matrix.</div></div>`;
  wireBoard($("lab-matrix"));
}
$("matrix-refresh").onclick = () => loadMatrix().catch(() => {});
$("matrix-sweep").onchange = () => loadMatrix().catch(() => {});
$("sweep-run").onclick = async () => {
  const body = { strategies: sweepSel("sweep-strats"), instruments: sweepSel("sweep-insts"), timeframes: sweepSel("sweep-tfs"),
    cost_multiplier: parseFloat($("sweep-costx").value) || 1, sentiment_mode: $("sweep-sent").value, presets: $("sweep-presets").checked };
  if (!body.strategies.length || !body.instruments.length || !body.timeframes.length) { toast("Pick at least one of each", "warn"); return; }
  const r = await api("/api/lab/sweep", body);
  toast(`Sweep #${r.run_id} started: ${r.combos} runs`, "ok"); startPoll();
};
function renderSweeps() {
  const rows = lab.runs.filter(x => x.kind === "sweep");
  $("lab-sweeps").innerHTML = table([{ l: "#", k: "id", n: 1 }, { l: "Label", f: x => esc(x.label || "") },
    { l: "Status", f: x => x.status === "RUNNING" ? `RUNNING <div class="progress"><i style="width:${Math.round((x.progress || 0) * 100)}%"></i></div>` : esc(x.status), c: x => x.status === "FAILED" ? "neg" : "" },
    { l: "Progress", f: x => esc(x.message || "") }, { l: "Positive", n: 1, f: x => x.metrics ? (x.metrics.positive ?? "–") : "–" }, { l: "When", f: x => tsf(x.created_at) },
    { l: "", f: x => x.status === "RUNNING" || x.status === "QUEUED" ? `<button class="btn small" data-stop="${x.id}">stop</button>` : (x.status === "DONE" ? `<button class="btn small ghost" data-open="${x.id}">open</button>` : "") }],
    rows, { empty: "No sweeps yet." });
  $("lab-sweeps").querySelectorAll("[data-stop]").forEach(b => b.onclick = () => api(`/api/lab/runs/${b.dataset.stop}/stop`, {}));
  $("lab-sweeps").querySelectorAll("[data-open]").forEach(b => b.onclick = () => { showSub("result"); showRun(+b.dataset.open); });
}
function showSub(name) {
  document.querySelectorAll("#lab-subtabs a").forEach(a => a.classList.toggle("active", a.dataset.sub === name));
  document.querySelectorAll(".lab-results .sub").forEach(s => s.classList.toggle("active", s.dataset.sub === name));
  if (name === "board") loadBoard().catch(() => {});
  if (name === "matrix") loadMatrix().catch(() => {});
}
document.querySelectorAll("#lab-subtabs a").forEach(a => a.onclick = () => showSub(a.dataset.sub));
$("lab-runs-refresh").onclick = refreshRuns;
tabLoaders.lab = () => loadLab().catch(e => console.error(e));

/* ═══════════════════════════════ DATA ═══════════════════════════════ */
let dataPoll = null;
// OF-3: filter the coverage by source and timeframe; "what the Lab can use" shows the history
// per timeframe after deriving it from finer bars (15m from Dhan 1m is five years).
const covState = { data: null, view: store.get("cov.view", "effective"), source: store.get("cov.source", ""), tf: store.get("cov.tf", ""), q: "" };
function drawCoverage() {
  const cov = covState.data; if (!cov) return;
  const q = covState.q.trim().toUpperCase(), eff = covState.view === "effective";
  const keep = (r) => (!q || String(r.series_key).includes(q)) && (!covState.tf || (eff ? r.timeframe : r.resolution) === covState.tf) &&
    (!covState.source || r.source === covState.source);
  const rows = (eff ? cov.effective || [] : cov.coverage || []).filter(keep);
  const short = eff ? rows.filter(r => r.timeframe !== "1d" && r.timeframe !== "60m" && r.days < 120) : [];
  $("cov-meta").textContent = `${rows.length} row${rows.length === 1 ? "" : "s"}` + (short.length ? ` · ${short.length} intraday with under 120 days` : "");
  const lim = cov.limits || {};
  $("cov-note").innerHTML = eff
    ? `Each timeframe is built from the finest bars that can make it, so the history is the widest base's. ` +
      `<b>${esc(lim.yahoo || "")}</b>: a series only Yahoo serves (currencies, and stocks or indices without a Dhan 1-minute backfill) stays at about 60 days at 5m–30m, and grows only as later backfills append. ` +
      `${esc(lim.dhan || "")}: once your Dhan token is renewed (Settings), <b>Dhan: 5-year 1-minute history</b> lifts every Dhan-traded series to five years at every intraday timeframe. Currencies here are spot FX from Yahoo by design.`
    : `The raw rows in the corpus, per series, resolution and source.`;
  const days = (r) => `<span class="${r.timeframe !== "1d" && r.timeframe !== "60m" && r.days < 120 ? "warn" : ""}">${Number(r.days).toLocaleString("en-IN")}</span>`;
  $("data-coverage").innerHTML = eff
    ? table([{ l: "Series", k: "series_key" }, { l: "Timeframe", k: "timeframe" }, { l: "From", k: "first" }, { l: "To", k: "last" }, { l: "Days", n: 1, f: days },
        { l: "Built from", f: r => `${esc(r.base)} · ${esc(r.source)}` }, { l: "Sources", f: r => esc((r.sources || []).join(" + ")) }], rows, { empty: "Nothing matches — clear a filter, or start a backfill." })
    : table([{ l: "Series", k: "series_key" }, { l: "Res", k: "resolution" }, { l: "Source", k: "source" },
        { l: "First", k: "first" }, { l: "Last", k: "last" }, { l: "Bars", n: 1, f: r => Number(r.bars).toLocaleString("en-IN") }], rows, { empty: "Nothing matches — clear a filter, or start a backfill." });
}
[["cov-view", "view"], ["cov-source", "source"], ["cov-tf", "tf"]].forEach(([id, k]) => { const el = $(id); if (!el) return; el.value = covState[k]; el.onchange = () => { covState[k] = el.value; store.set("cov." + k, el.value); drawCoverage(); }; });
if ($("cov-q")) $("cov-q").oninput = () => { covState.q = $("cov-q").value; drawCoverage(); };
async function loadData() {
  const [cov, jobs] = await Promise.all([api("/api/lab/coverage"), api("/api/data/jobs")]);
  covState.data = cov; drawCoverage();
  const busy = jobs.jobs.some(j => j.status === "RUNNING" || j.status === "QUEUED");
  $("data-jobs").innerHTML = table([{ l: "#", k: "id", n: 1 }, { l: "Kind", k: "kind" },
    { l: "Status", f: j => j.status === "RUNNING" ? `RUNNING <div class="progress"><i style="width:${Math.round((j.progress || 0) * 100)}%"></i></div>` : esc(j.status), c: j => j.status === "FAILED" ? "neg" : "" },
    { l: "Message", f: j => esc(j.message || "") }, { l: "Started", f: j => tsf(j.created_at) },
    { l: "", f: j => j.status === "RUNNING" ? `<button class="btn small" data-stop="${j.id}">stop</button>` : "" }], jobs.jobs, { empty: "No jobs yet." });
  $("data-jobs").querySelectorAll("[data-stop]").forEach(b => b.onclick = () => api(`/api/data/jobs/${b.dataset.stop}/stop`, {}));
  if (busy && !dataPoll) dataPoll = setInterval(loadData, 2500);
  if (!busy && dataPoll) { clearInterval(dataPoll); dataPoll = null; }
  if (!$("eda-series").options.length) {
    const keys = [...new Set((cov.coverage || []).map(r => r.series_key))];
    const prev = store.get("eda.series", "BANKNIFTY");
    $("eda-series").innerHTML = keys.map(k => `<option ${k === prev ? "selected" : ""}>${esc(k)}</option>`).join("");
    if (keys.length) loadEda().catch(() => {});
  }
}
async function loadEda() {
  const key = $("eda-series").value, res = $("eda-res").value; store.set("eda.series", key);
  const [e, corr] = await Promise.all([api(`/api/data/eda?series=${encodeURIComponent(key)}&resolution=${res}`), api("/api/data/correlation")]);
  const d = e.daily || {}, rs = d.returns_stats || {}, gs = d.gap_stats || {};
  $("eda-stats").innerHTML = tile("Sessions (daily)", rs.n ?? "–") + tile("Intraday bars", e.bars != null ? Number(e.bars).toLocaleString("en-IN") : "–", res) +
    tile("Daily σ", pct(rs.std), `mean ${spct(rs.mean, 3)}`) + tile("Up days", pct(rs.positive_pct)) + tile("Skew / kurt", `${fmt(rs.skew)} / ${fmt(rs.kurtosis)}`) +
    tile("Worst / best day", `${spct(rs.min)} / ${spct(rs.max)}`) + tile("Gap σ", pct(gs.std), `5–95 % ${spct(gs.p05)}…${spct(gs.p95)}`);
  if (C() && d.dates) {
    const closePts = d.dates.map((t, i) => ({ time: t, value: d.close[i] }));
    const series = [{ name: key, points: closePts, color: cssVar("--cyan") }];
    if (d.vix) series.push({ name: "VIX", points: d.dates.map((t, i) => ({ time: t, value: d.vix[i] })).filter(p => p.value != null), color: cssVar("--accent2"), width: 1, scale: "left" });
    C().lines($("eda-close"), series, { left: !!d.vix, lastValue: true });
    const h = d.returns_hist || { edges: [], counts: [] };
    C().bars($("eda-ret"), h.edges.slice(0, -1).map(x => x.toFixed(1) + "%"), h.counts, { color: (v, i) => (h.edges[i] + h.edges[i + 1]) / 2 >= 0 ? cssVar("--ok") : cssVar("--bad") });
  }
  const p = e.profile;
  if (C() && p) C().bars($("eda-profile"), p.times, p.range_pct, { color: () => cssVar("--violet"), priceFormat: { type: "price", precision: 3, minMove: 0.001 } });
  const cv = e.coverage;
  if (C() && cv) C().bars($("eda-cov"), cv.months, cv.sessions, { color: () => cssVar("--cyan") });
  $("eda-weekday").innerHTML = table([{ l: "Weekday", k: "weekday" }, { l: "Mean return", n: 1, f: r => spct(r.mean_ret, 3), c: r => cls(r.mean_ret) }, { l: "Up %", n: 1, f: r => pct(r.win_pct) }, { l: "Days", n: 1, k: "n" }], e.weekday || [], { empty: "no daily bars" });
  const ks = corr.keys || [], M = corr.matrix || [];
  const col = (v) => { if (v == null) return "transparent"; const a = Math.min(1, Math.abs(v)); return v >= 0 ? `rgba(20,131,71,${0.12 + 0.75 * a})` : `rgba(212,31,62,${0.12 + 0.75 * a})`; };
  $("eda-corr").innerHTML = ks.length ? `<table class="heat"><thead><tr><th></th>${ks.map(k => `<th>${esc(k)}</th>`).join("")}</tr></thead><tbody>` +
    ks.map((k, i) => `<tr><th style="text-align:right">${esc(k)}</th>${M[i].map(v => `<td style="background:${col(v)}">${v == null ? "" : v.toFixed(2)}</td>`).join("")}</tr>`).join("") + `</tbody></table><div class="hint">${corr.sessions || 0} sessions</div>` : `<div class="muted">Need daily bars for at least two series.</div>`;
}
$("eda-go").onclick = () => loadEda().catch(() => {});
$("data-run").onclick = async () => {
  const series = $("data-series").value.trim().split(/[\s,]+/).filter(Boolean);
  const res = $("data-res").value.trim().split(/[\s,]+/).filter(Boolean);
  const r = await api("/api/data/backfill", { series: series.length ? series : null, resolutions: res.length ? res : null, provider: $("data-provider").value || null, full: $("data-full").checked });
  toast("Backfill job #" + r.job_id + " started", "ok"); loadData();
};
$("data-dhan-full").onclick = async () => {
  if (!confirm("Backfill from Dhan: daily bars for every series plus 1-minute bars for the desk watchlist and India VIX, five years back. Runs in the background (15–40 min). Start?")) return;
  const focusKeys = (lab.focus.length ? lab.focus : (await api("/api/lab/focus")).instruments).map(k => k.replace(/_(OPT|FUT)$/, ""));
  const series = [...new Set([...focusKeys, "INDIAVIX", "NIFTY", "BANKNIFTY", "SENSEX"])];
  const r1 = await api("/api/data/backfill", { series: null, resolutions: ["1d"], provider: "dhan", full: false });
  const r2 = await api("/api/data/backfill", { series, resolutions: ["1m"], provider: "dhan", full: false });
  toast(`Dhan backfill jobs #${r1.job_id} (daily) and #${r2.job_id} (1-minute) started`, "ok"); loadData();
};
$("data-quality").onclick = async () => {
  const q = await api("/api/data/quality?resolution=5m"); $("data-quality-card").hidden = false;
  $("data-quality-out").innerHTML = table([{ l: "Series", k: "series" }, { l: "First", k: "first" }, { l: "Last", k: "last" }, { l: "Days", n: 1, k: "days" }, { l: "Bars", n: 1, k: "bars" },
    { l: "Short days", n: 1, k: "short_days" }, { l: "Zero-vol bars", n: 1, f: r => r.zero_volume_bars ?? "n/a" }, { l: "Max gap %", n: 1, f: r => fmt(r.max_gap_pct) }, { l: "Corp. action?", f: r => r.suspect_corporate_action ? "⚠" : "" }], q.rows);
};
tabLoaders.data = () => loadData().catch(() => {});

/* ═══════════════════════════ NEWS & SENTIMENT ═══════════════════════ */
function renderPulse(p) {
  const comps = p.components || {};
  const g = p.score == null ? `<div class="gauge"><b class="muted">–</b><div class="lbl">${esc(p.label || "no data")}</div></div>`
    : `<div class="gauge"><b class="${p.score >= 0.6 ? "pos" : p.score <= 0.4 ? "neg" : "warn"}">${fmt(p.score, 2)}</b><div class="lbl">${esc(p.label)}</div><div class="bar"><i style="margin-left:${Math.round(p.score * 100)}%"></i></div><div class="hint">${tsf(p.at)}</div></div>`;
  const t = (name, c) => !c ? tile(name, "–", "not available") : (c.score == null ? tile(name, "–", esc(c.note || "")) : tile(name, fmt(c.score, 2), esc(c.note || ""), c.score >= 0.6 ? "pos" : c.score <= 0.4 ? "neg" : ""));
  $("pulse").innerHTML = g + `<div class="tiles">${t("Breadth", comps.breadth)}${t("Index trend", comps.trend)}${t("India VIX", comps.vix)}${t("NIFTY PCR", comps.pcr)}</div>`;
}
async function loadNews() {
  const [pulse, lat, sc, nw, hist] = await Promise.all([api("/api/sentiment/pulse"), api("/api/sentiment/latest"), api("/api/sentiment?limit=100"), api("/api/news?limit=100"), api("/api/sentiment?scope=MARKET&limit=2000")]);
  renderPulse(pulse);
  const L = Object.values(lat.latest || {});
  $("sent-board").innerHTML = L.length ? L.map(x => `<div class="tile"><small>${esc(x.scope)} ${esc(x.symbol || "")} · ${esc(x.source || "")}</small>
    <b class="${x.score > 0.1 ? "pos" : (x.score < -0.1 ? "neg" : "")}">${Number(x.score) > 0 ? "+" : ""}${fmt(x.score, 2)}</b><i>${esc(x.horizon || "")} · conf ${fmt(x.confidence, 2)} · ${tsf(x.ts)}</i></div>`).join("")
    : `<div class="muted">No sentiment yet. The pulse records itself every 15 minutes while the market is open; post or import your own below.</div>`;
  const bySrc = {};
  (hist.scores || []).forEach(r => { const k = r.source || "manual"; (bySrc[k] = bySrc[k] || []).push({ time: C() ? C().toT(Math.floor(Date.parse(r.ts) / 1000)) : 0, value: r.score }); });
  const palette = [cssVar("--cyan"), cssVar("--accent2"), cssVar("--violet"), cssVar("--ok"), cssVar("--bad")];
  const series = Object.entries(bySrc).map(([k, pts], i) => ({ name: k, points: pts.sort((a, b) => a.time - b.time).filter((p, j, arr) => j === 0 || p.time !== arr[j - 1].time), color: palette[i % palette.length], width: 2 }));
  if (C()) { if (series.length) C().lines($("sent-chart"), series, { timeVisible: true, lastValue: true }); else { C().dispose($("sent-chart")); $("sent-chart").innerHTML = `<div class="empty-inline" style="padding:14px"><svg class="mascot sm"><use href="#m-cow"/></svg> No MARKET scores recorded yet.</div>`; } }
  $("sent-list").innerHTML = table([{ l: "Time", f: r => tsf(r.ts) }, { l: "Scope", k: "scope" }, { l: "Symbol", k: "symbol" },
    { l: "Score", n: 1, f: r => fmt(r.score, 2), c: r => cls(r.score) }, { l: "Conf", n: 1, f: r => fmt(r.confidence, 2) }, { l: "Horizon", k: "horizon" }, { l: "Source", k: "source" }, { l: "Note", f: r => esc((r.note || "").slice(0, 80)) }], sc.scores, { empty: "–" });
  $("news-list").innerHTML = (nw.items || []).map(n => `<div><span class="lvl ${n.sentiment > 0.1 ? "pos" : (n.sentiment < -0.1 ? "neg" : "muted")}">${n.sentiment == null ? "NEWS" : (n.sentiment > 0 ? "+" : "") + fmt(n.sentiment, 2)}</span>
    <b>${esc(n.headline)}</b> <span class="dim">${tsf(n.ts)} · ${esc(n.source || "")} ${(n.symbols || []).map(esc).join(" ")}</span>
    ${n.url ? ` <a href="${esc(n.url)}" target="_blank" rel="noopener">link</a>` : ""}<br><span class="muted">${esc((n.body || "").slice(0, 240))}</span></div>`).join("") || `<div class="empty-inline"><svg class="mascot sm"><use href="#m-cow"/></svg> No news yet. Add items below or post them from your scorer.</div>`;
  $("news-curl").textContent = `# post a sentiment score (token = ONELOT_API_TOKEN or data/api_token.txt)
curl -X POST ${location.origin}/api/sentiment -H "x-onelot-token: $TOKEN" -H "Content-Type: application/json" \\
  -d '{"scope":"MARKET","score":0.35,"confidence":0.7,"horizon":"intraday","source":"my-scorer"}'
# bulk-import history for backtests (date,score[,confidence]; 0..1 or -1..1)
curl -X POST ${location.origin}/api/sentiment/import -H "x-onelot-token: $TOKEN" -H "Content-Type: application/json" \\
  -d '{"text":"2024-01-02,0.62\\n2024-01-03,0.48","scope":"MARKET","source":"my-scorer","replace_source":true}'
# post a news item
curl -X POST ${location.origin}/api/news -H "x-onelot-token: $TOKEN" -H "Content-Type: application/json" \\
  -d '{"headline":"RBI holds rates","source":"wire","symbols":["BANKNIFTY"],"sentiment":0.1}'
# feed GIFT NIFTY into the desk's context strip
curl -X POST ${location.origin}/api/desk/context -H "x-onelot-token: $TOKEN" -H "Content-Type: application/json" \\
  -d '{"key":"GIFTNIFTY","last":25410,"change_pct":0.3}'`;
}
$("pulse-refresh").onclick = async () => { renderPulse(await api("/api/sentiment/pulse?refresh=1")); };
$("sent-add").onclick = async () => {
  await api("/api/sentiment", { scope: $("sent-scope").value, symbol: $("sent-symbol").value || null, score: parseFloat($("sent-score").value), confidence: parseFloat($("sent-conf").value),
    horizon: $("sent-horizon").value, source: $("sent-source").value || "manual", note: $("sent-note").value || null });
  toast("Sentiment posted", "ok"); loadNews();
};
$("imp-run").onclick = async () => {
  const text = $("imp-text").value.trim(); if (!text) { toast("Paste some rows first", "warn"); return; }
  const r = await api("/api/sentiment/import", { text, scope: $("imp-scope").value, symbol: $("imp-symbol").value || null, source: $("imp-source").value || "import", replace_source: $("imp-replace").checked });
  $("imp-out").textContent = `imported ${r.imported} rows (${r.first.slice(0, 10)} → ${r.last.slice(0, 10)})`; toast(`Imported ${r.imported} scores`, "ok"); loadNews();
};
$("news-add").onclick = async () => {
  const sym = $("news-symbols").value.trim().split(/[\s,]+/).filter(Boolean);
  const sent = $("news-sent").value === "" ? null : parseFloat($("news-sent").value);
  await api("/api/news", { headline: $("news-headline").value, body: $("news-body").value || null, source: $("news-source").value || null, url: $("news-url").value || null, symbols: sym, sentiment: sent });
  $("news-headline").value = ""; $("news-body").value = ""; toast("News added", "ok"); loadNews();
};
tabLoaders.news = () => loadNews().catch(() => {});

/* ═════════════════════════════ SETTINGS ═════════════════════════════ */
async function loadSettings() {
  const s = await api("/api/settings"); const c = s.credentials || {};
  $("env-path").textContent = s.env_path; $("postback-hint").textContent = s.postback_url_hint;
  $("cred-status").innerHTML = kv("Client ID", c.client_id || "<span class='warn'>not set</span>") + kv("Access token", c.access_token || "<span class='warn'>not set</span>") +
    kv("Data API token", c.data_access_token || "<span class='dim'>not set (optional)</span>") +
    kv("Token valid till", s.token_expiry ? tsf(s.token_expiry) + " IST" : "<span class='dim'>unknown (refresh the account)</span>") +
    kv("Self-renew (TOTP)", c.can_self_renew ? "<span class='pos'>configured</span>" + (s.last_renew_at ? ` · last ${tsf(s.last_renew_at)}` : "") + (s.last_renew_error ? ` · <span class='neg'>${esc(s.last_renew_error)}</span>` : "") : "<span class='dim'>not configured</span>") +
    kv("Account reads", s.account_adapter ? "<span class='pos'>attached</span>" : "<span class='warn'>no adapter</span>") +
    kv("Data API plan", s.data_api_active ? "<span class='pos'>active</span>" : "<span class='warn'>not active / unknown</span>") +
    kv("Market feed", s.feed_live ? "<span class='pos'>Dhan (live)</span>" : "<span class='warn'>synthetic mock</span> <span class='dim'>— press Go live once the token is valid</span>") +
    kv("Desk bars", s.desk_source ? `${esc(s.desk_source.name)} ${esc(s.desk_source.resolution)} · ${esc(delayNote(s.desk_source))}${s.desk_source.socket ? ` · socket ${s.desk_source.socket.alive ? "<span class='pos'>connected</span>" : "<span class='warn'>down</span>"} (${s.desk_source.socket.subscribed} instruments)` : ""}` : "–");
  const block = (t, o) => `<h3>${t}</h3><pre class="code">${esc(JSON.stringify(o, null, 2))}</pre>`;
  $("cfg-view").innerHTML = kv("Mode", esc(s.mode)) + kv("Bind", `${esc(s.api.host)}:${s.api.port}`) +
    block("Live direction source (Bank Nifty engine)", s.live) + block("Risk (absolute rupees, one unit)", s.risk) + block("Backtest defaults", s.backtest) + block("Data corpus", s.data);
  try {
    const d = await api("/api/desk");
    const rows = [...(d.indices || []), ...(d.stocks || [])];
    $("desk-cfg").innerHTML = kv("File", esc(d.config_path || "config/desk.yaml")) + kv("Instruments", `${(d.indices || []).length} indices · ${(d.stocks || []).length} stocks`) +
      kv("Strategy slots", (d.counts || {}).strategies || 0) + kv("Bars", `${(d.source || {}).name} · ${(d.source || {}).resolution} · poll ${(d.source || {}).poll_seconds}s`) +
      `<div class="scroll mt">` + table([{ l: "Instrument", f: r => `<b>${esc(r.label)}</b> <span class="dim">${esc(r.tracked)}</span>` },
        { l: "Strategies", f: r => (r.strategies || []).map(x => `<span class="schip">${esc(x.name)}@${esc(x.timeframe)}${Object.keys(x.params || {}).length ? " ✎" : ""}</span>`).join(" ") }], rows) + `</div>`;
  } catch (e) { $("desk-cfg").innerHTML = `<div class="warn">Live Desk not running.</div>`; }
}
$("cred-save").onclick = async () => {
  const body = {}; if ($("cred-client").value.trim()) body.client_id = $("cred-client").value.trim();
  if ($("cred-token").value.trim()) body.access_token = $("cred-token").value.trim();
  if ($("cred-data-token").value.trim()) body.data_access_token = $("cred-data-token").value.trim();
  if ($("cred-pin").value.trim()) body.pin = $("cred-pin").value.trim();
  if ($("cred-totp").value.trim()) body.totp_secret = $("cred-totp").value.trim();
  if (!Object.keys(body).length) { toast("Nothing to save", "warn"); return; }
  $("live-status").innerHTML = `<div class="empty-inline"><svg class="mascot sm bounce"><use href="#m-ichi"/></svg> saving, checking the profile, switching the feed…</div>`;
  const r = await api("/api/settings/dhan", body);
  ["cred-client", "cred-token", "cred-data-token", "cred-pin", "cred-totp"].forEach(id => { $(id).value = ""; });
  const msg = !r.ok ? "Saved, but the adapter is not attached: " + (r.reason || "") : !r.profile_ok ? "Saved, but Dhan rejected the token (profile failed) — generate a fresh one" :
    r.went_live ? "Saved · token valid · Data API active · LIVE feed started" : (r.live ? "Saved · token valid · already live" : "Saved · token valid · " + (r.live_reason || ""));
  toast(msg, r.ok && r.profile_ok ? "ok" : "warn");
  $("live-status").innerHTML = `<div class="${r.ok && r.profile_ok ? "notice" : "warnbar"}">${esc(msg)}</div>`;
  acctLoaded = false; loadSettings(); loadAccount(true).catch(() => {});
};
$("cred-test").onclick = async () => {
  $("cred-test-out").innerHTML = `<div class="empty-inline"><svg class="mascot sm bounce"><use href="#m-ichi"/></svg> testing every endpoint (the scrip master download takes a while)…</div>`;
  const r = await api("/api/settings/dhan/test", {});
  const rows = Object.entries(r.checks || {}).map(([k, v]) => ({ name: k, ...v }));
  $("cred-test-out").innerHTML = `<div class="row"><span class="badge ${r.trading_ok ? "ok" : "bad"}"><span>trading API ${r.trading_ok ? "OK" : "FAIL"}</span></span>
    <span class="badge ${r.data_ok ? "ok" : "warn"}"><span>data API ${r.data_ok ? "OK" : "not available"}</span></span></div>` +
    (r.profile ? kv("Token valid till", esc(r.profile.token_validity || "–")) + kv("Data plan", esc(r.profile.data_plan || "–")) + kv("Data valid till", esc(r.profile.data_validity || "–")) : "") +
    table([{ l: "Check", k: "name" }, { l: "Tier", k: "tier" }, { l: "Result", f: x => x.ok ? "<span class='pos'>OK</span>" : `<span class='neg'>${esc(x.error)}</span>` }, { l: "Detail", f: x => esc((x.detail || "").slice(0, 120)) }], rows) +
    ((r.notes || []).length ? `<ul class="small-list muted">${[...new Set(r.notes)].map(n => `<li>${esc(n)}</li>`).join("")}</ul>` : "");
};
$("cred-renew").onclick = async () => {
  $("live-status").innerHTML = `<div class="empty-inline"><svg class="mascot sm bounce"><use href="#m-ichi"/></svg> minting a fresh token…</div>`;
  const r = await api("/api/settings/dhan/renew", {});
  toast(r.ok ? `Token renewed${r.expiry_time ? " · valid till " + r.expiry_time : ""}` : "Renewal failed: " + r.reason, r.ok ? "ok" : "bad");
  $("live-status").innerHTML = `<div class="${r.ok ? "notice" : "warnbar"}">${esc(r.ok ? "Token renewed" + (r.expiry_time ? " · valid till " + r.expiry_time : "") + (r.live ? " · live feed on" : "") : r.reason)}</div>`;
  acctLoaded = false; loadSettings(); loadAccount(true).catch(() => {});
};
$("cred-golive").onclick = async () => {
  $("live-status").innerHTML = `<div class="empty-inline"><svg class="mascot sm bounce"><use href="#m-ichi"/></svg> checking the Data API and starting the feed…</div>`;
  const r = await api("/api/settings/dhan/golive", {});
  toast(r.live ? (r.switched ? "Live feed started" : "Already live") : "Not live: " + r.reason, r.live ? "ok" : "warn");
  $("live-status").innerHTML = `<div class="${r.live ? "notice" : "warnbar"}">${esc(r.reason)}</div>`;
  loadSettings();
};
$("desk-reload").onclick = async () => { const r = await api("/api/desk/reload", {}); toast(`Desk reloaded: ${r.instruments} instruments`, "ok"); loadSettings(); };
$("desk-poll").onclick = async () => { await api("/api/desk/poll", {}); toast("Polling now", "ok"); desk.force = true; };
$("notify-test").onclick = async () => { const r = await api("/api/notify/test", {}); toast("Notification test: " + JSON.stringify(r), "ok"); };
tabLoaders.settings = () => loadSettings().catch(() => {});

/* ── boot ─────────────────────────────────────────────────────────────── */
connect();
api("/api/account").then(a => { acctSummary = a.summary || null; if (latest) renderBadges(latest); }).catch(() => {});
showTab(location.hash.slice(1) || store.get("tab", "home"));

/* ── Machine Learning Lab: statistical forecasts of the next candle (they inform; no alert, no trade) ── */
const MLLAB_NAMES = { arima: "ARIMA", sarima: "SARIMA", garch: "GARCH", ewma: "EWMA", empirical: "Empirical" };
const mllab = { seq: 0, inflight: 0 };
// An error's `detail` as text: FastAPI's 422 gives a list of {msg, loc}, other errors a string.
const mllabDetail = (d) => d == null || d === "" ? "" : typeof d === "string" ? d : Array.isArray(d) ? d.map(x => (x && x.msg) || JSON.stringify(x)).join("; ") : JSON.stringify(d);
function mllabCell(c) {
  if (!c || c.note || c.p_up == null) return `<td class="ml-cell"><span class="muted">${esc((c && c.note) || "no forecast")}</span></td>`;
  const up = Math.min(100, Math.max(0, Number(c.p_up) * 100));
  const tint = Math.min(0.35, Math.abs(up - 50) / 50 * 0.7).toFixed(2);
  const half = c.mean ? (Number(c.hi68) - Number(c.lo68)) / 2 / Number(c.mean) * 100 : null;
  const bt = c.backtest;
  const btLine = !bt || !bt.steps ? "no backtest yet"
    : `last ${esc(bt.steps)}: 68% band held ${pct(bt.in68 == null ? null : bt.in68 * 100, 0)}, direction right ${bt.direction == null ? "n/a" : pct(bt.direction * 100, 0)}`;
  return `<td class="ml-cell ${up >= 50 ? "up" : "down"}" style="--ml-a:${tint}" title="${esc(c.detail || "")}">` +
    `<div class="ml-p"><b class="${up > 50 ? "pos" : up < 50 ? "neg" : ""}">${pct(up, 0)}</b> <span class="dim">up</span><div class="ml-bar"><i style="width:${up.toFixed(0)}%"></i></div></div>` +
    `<div class="mono">${esc(fmt(c.lo68))} – ${esc(fmt(c.hi68))} <span class="dim">±${esc(pct(half, 2))}</span></div>` +
    `<div class="ml-small mono">95%: ${esc(fmt(c.lo95))} – ${esc(fmt(c.hi95))}</div>` +
    `<div class="ml-small">${btLine}</div></td>`;
}
function mllabTable(d) {
  const models = (d && d.models) || [];
  const rows = (d && d.rows) || [];
  if (!rows.length) return `<div class="muted">No instruments tracked yet.</div>`;
  const head = `<th>Instrument</th>` + models.map(m => `<th title="${esc(m.description)}">${esc(MLLAB_NAMES[m.key] || m.key)}</th>`).join("");
  const body = rows.map(r => `<tr><td class="ml-inst"><b>${esc(r.label)}</b> <span class="dim">${esc(r.kind)}</span><br><span class="mono">${esc(fmt(r.last_close))}</span><br><small class="muted">next candle ${esc(tsf(r.next_bar_ts))}</small>${r.bars > 0 && r.bars < 100 ? `<br><small class="warn">${esc(r.bars)} bars</small>` : ""}${r.note ? `<br><small class="muted">${esc(r.note)}</small>` : ""}</td>` +
    models.map(m => mllabCell((r.cells || {})[m.key])).join("") + `</tr>`).join("");
  return `<table class="ml-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}
async function loadMllab() {
  const seq = ++mllab.seq, tf = $("mllab-tf").value;
  mllab.inflight++;
  try {
    const r = await fetch(`/api/mllab?timeframe=${encodeURIComponent(tf)}`);  // a read: no token, no toast
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(mllabDetail(j && j.detail) || `${r.status} /api/mllab`);
    if (seq !== mllab.seq) return;  // a newer request (another timeframe) is on its way
    $("mllab-table").innerHTML = mllabTable(j);
    $("mllab-updated").textContent = `updated ${tsf(j.generated_at)} · ${j.timeframe}`;
  } catch (e) {
    if (seq !== mllab.seq) return;
    $("mllab-table").innerHTML = `<div class="warnbar">Forecasts unavailable: ${esc(e.message)}</div>`;
    $("mllab-updated").textContent = "update failed";
  } finally { mllab.inflight--; }
}
// The 60-second refresh: only while the tab is shown, and never over a request still running.
function mllabTick() {
  if (mllab.inflight || !tabShown("mllab") || document.visibilityState === "hidden") return false;
  loadMllab();
  return true;
}
$("mllab-tf").value = store.get("mllab.tf", "15m");
if (!$("mllab-tf").value) $("mllab-tf").value = "15m";
$("mllab-tf").onchange = () => { store.set("mllab.tf", $("mllab-tf").value); loadMllab(); };
tabLoaders.mllab = () => { loadMllab(); };
setInterval(mllabTick, 60000);
if (tabShown("mllab")) loadMllab();
})();
