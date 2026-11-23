/* US-077: market states in the Machine Learning Lab. Counting, never a signal.
   Helpers stand at column 0 inside the IIFE so the node tests can lift them. */
(function () {
"use strict";
const O = window.OneLot || {};
const esc = O.esc || ((s) => String(s ?? "").replace(/[&<>"]/g, (m) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[m])));
const DIRS = ["bull", "flat", "bear"];
const ARROW = { bull: "▲", flat: "▶", bear: "▼" };
const pc = (p) => (typeof p === "number" && Number.isFinite(p)) ? String(Math.round(100 * p)) : "–";
const upper = (s) => String(s || "").toUpperCase();

function msContext(next) {
  if (!next || !next.context) return "";
  const who = next.context.length === 2 ? "the last two sessions" : "the last session";
  return `from ${who}: ${next.context.map(esc).join(", ")} (n = ${esc(next.n)})`;
}
function msHeadline(next, base) {
  if (!next) return "Not enough history yet.";
  const score = esc(next.score);
  if (next.conviction === "none" || !next.lean) {
    return `Next session: no lean: tomorrow looks like any other session (score ${score})`;
  }
  const p = next.probs[next.lean];
  const usual = base ? base[next.lean] : null;
  return `Next session: leans ${upper(esc(next.lean))} ${pc(p)}% (usual ${pc(usual)}%) · conviction ${esc(next.conviction)} (score ${score})`;
}
function msSizeLine(sz) {
  if (!sz) return "";
  if (sz.conviction === "none" || !sz.lean) {
    return "Next session's range: no lean: it looks like any other session";
  }
  const act = sz.lean === "active";
  const p = act ? sz.p_active : 1 - sz.p_active;
  const usual = act ? sz.base_active : 1 - sz.base_active;
  return `Next session's range: leans ${upper(esc(sz.lean))} ${pc(p)}% (usual ${pc(usual)}%) · conviction ${esc(sz.conviction)}`;
}
function msBars(next, base) {
  if (!next || !base) return "";
  return DIRS.map((k) => {
    const p = next.probs[k], b = base[k];
    const lean = next.conviction !== "none" && next.lean === k ? " lean" : "";
    return `<div class="ms-bar ${esc(k)}${lean}"><span class="ms-bl">${upper(esc(k))}</span>` +
      `<span class="ms-track"><span class="ms-fill" style="width:${(100 * p).toFixed(1)}%"></span>` +
      `<span class="ms-tick" style="left:${(100 * b).toFixed(1)}%" title="usual ${pc(b)}%"></span></span>` +
      `<span class="ms-bv">${pc(p)}% <small>usual ${pc(b)}%</small></span></div>`;
  }).join("");
}
function msChips(sessions) {
  const last = (sessions || []).slice(-30);
  if (!last.length) return `<div class="muted">No stated sessions yet.</div>`;
  return last.map((s) => {
    const ret = (typeof s.r_pct === "number") ? (s.r_pct >= 0 ? "+" : "−") + Math.abs(s.r_pct).toFixed(2) + "%" : "";
    const tip = `${s.date}: ${ret}, ${s.direction}, ${s.size === "active" ? "wide" : "narrow"} range`;
    const dir = DIRS.includes(s.direction) ? s.direction : "flat";
    return `<span class="ms-chip ${dir} ${s.size === "active" ? "active" : "calm"}" title="${esc(tip)}">${ARROW[dir]}</span>`;
  }).join("");
}
function msRow(label, row) {
  if (!row) return "";
  const cells = DIRS.map((k) => `<td class="n">${pc(row.probs[k])}%</td>`).join("");
  return `<tr><td>${label}</td><td class="n">${esc(row.n)}</td>${cells}</tr>`;
}
function msPattern(d) {
  if (!d || !d.first_order || !d.last) return "";
  const last = d.last;
  const one = d.first_order.find((r) => r.context[0] === last[last.length - 1]);
  const two = last.length === 2 ? d.second_order.find((r) => r.context[0] === last[0] && r.context[1] === last[1]) : null;
  const usual = `<tr class="ms-usual"><td>usual</td><td class="n">–</td>${DIRS.map((k) => `<td class="n">${pc(d.base[k])}%</td>`).join("")}</tr>`;
  return `<table class="ms-table"><thead><tr><th>After</th><th class="n">cases</th><th class="n">bull</th><th class="n">flat</th><th class="n">bear</th></tr></thead><tbody>` +
    msRow(`last session: ${esc(last[last.length - 1])}`, one) +
    (two ? msRow(`last two: ${esc(last[0])}, ${esc(last[1])}`, two) : "") + usual + `</tbody></table>`;
}
function msRuns(runs, base) {
  if (!runs || !runs.length) return "";
  const rows = runs.map((r) => {
    const len = r.length >= 3 ? "3 or more" : String(r.length);
    const usual = base ? pc(base[r.direction]) : "–";
    const went = (r.continued === null || r.continued === undefined) ? "–" : pc(r.continued) + "%";
    return `<tr><td>${upper(esc(r.direction))}</td><td>run of ${esc(len)}</td><td class="n">${went}</td><td class="n">${usual}%</td><td class="n">${esc(r.n)}</td></tr>`;
  }).join("");
  return `<table class="ms-table"><thead><tr><th>Direction</th><th>Run so far</th><th class="n">went on</th><th class="n">usual</th><th class="n">cases</th></tr></thead><tbody>${rows}</tbody></table>`;
}
function msEvidence(wf) {
  if (!wf || !wf.n) return "Not enough past sessions yet to check how often this was right.";
  return `Checked on ${esc(wf.n)} past sessions: it named the next direction right ${pc(wf.hit)}% of the time; ` +
    `always naming the most common one scores ${pc(wf.hit_naive)}%. For the size of the range: ` +
    `${pc(wf.size_hit)}% against ${pc(wf.size_hit_naive)}%.`;
}
function msLegend() {
  return "<p><b>What a state is.</b> Each session gets a direction and a size. Direction: bull when the day's " +
    "close-to-close return is more than half a usual daily move above zero, bear when it is more than half below, " +
    "flat in between (\"usual\" is the spread of the 20 sessions before it). Size: active when the day's " +
    "high-to-low range is above the median range of the 20 sessions before it, else calm. An arrow is the " +
    "direction; a filled chip is an active day, a hollow chip a calm one. The newest session is on the right.</p>" +
    "<p><b>What the percentages are.</b> How often that kind of session followed this same pattern in this " +
    "instrument's own past. \"Usual\" is how often it happens on any day. A small count is pulled toward the usual rate.</p>" +
    "<p><b>What conviction means.</b> How far the percentage sits from the usual rate, set against how many cases " +
    "it rests on: none, weak, moderate, strong. \"No lean\" means tomorrow looks like any other session.</p>" +
    "<p><b>What it is not.</b> A lean is not an alert and not a trade. Tested on 23 instruments and about 55,000 " +
    "sessions up to 13 November 2026: direction has almost no memory (after two bull sessions the next was bull " +
    "33% of the time in indices and 29% in stocks, against a usual 34% and 29%), and a lean did not beat simply " +
    "naming the most common direction. The size of the range does carry memory: after an active day the next " +
    "was active 57% of the time, after a calm day 42%, against 49% usual. It says how big, not which way.</p>" +
    "<p><b>The table below.</b> Read the range first. The day's opening candle has its own, wider band because " +
    "it carries the overnight gap. \"Move size\" compares the band with its own history. The \"up\" figure there " +
    "is not a signal: tested, it is right no more often than a coin.</p>";
}
function msThin(d) {
  return `Only ${esc(d.n_states ?? 0)} stated sessions for this instrument: too few to read anything from. ` +
    "The card fills in once there are 250.";
}
function msCard(d, instruments, selected, open) {
  const opts = (instruments || []).map((i) => `<option value="${esc(i.key)}"${i.key === selected ? " selected" : ""}>${esc(i.label)}</option>`).join("");
  const head = `<div class="row between"><h2>Market states <small>how each session is classified, and how the states follow each other</small></h2>` +
    `<div class="actions"><select id="ms-select" aria-label="Instrument">${opts}</select></div></div>`;
  let body;
  if (d && d.thin) {
    body = `<p class="ms-thin">${msThin(d)}</p>` + (d.sessions && d.sessions.length ? `<div class="ms-strip">${msChips(d.sessions)}</div>` : "");
  } else if (!d || !d.next) {
    body = `<div class="muted">${esc((d && d.note) || "Loading the states...")}</div>`;
  } else {
    body = `<div class="ms-headline">${msHeadline(d.next, d.base)}</div>` +
      `<div class="ms-bars">${msBars(d.next, d.base)}</div>` +
      `<div class="hint">${msContext(d.next)}</div>` +
      `<div class="ms-size">${msSizeLine(d.size_next)}</div>` +
      `<div class="ms-strip">${msChips(d.sessions)}</div>` +
      `<div class="ms-tables"><div class="scroll"><h3>What followed this pattern</h3>${msPattern(d)}</div>` +
      `<div class="scroll"><h3>Runs</h3>${msRuns(d.runs, d.base)}</div></div>` +
      `<p class="ms-evidence">${msEvidence(d.walk_forward)}</p>` +
      (d.note ? `<p class="hint">${esc(d.note)}</p>` : "");
  }
  return head + body + `<details class="ms-legend"${open ? " open" : ""}><summary>How to read this page</summary>${msLegend()}</details>`;
}

const state = { list: null, key: null, data: null, busy: false, rendered: false };
function draw() {
  const el = document.getElementById("mlstates-root");
  if (!el) return;
  const first = !state.rendered && !(O.store && O.store.get("mlstates.seen", false));
  el.innerHTML = `<div class="card ms-card">${msCard(state.data, state.list, state.key, first)}</div>`;
  state.rendered = true;
  if (first && O.store) O.store.set("mlstates.seen", true);
  const sel = document.getElementById("ms-select");
  if (sel) sel.onchange = () => { state.key = sel.value; if (O.store) O.store.set("mlstates.inst", state.key); state.data = null; draw(); load(); };
}
async function load() {
  if (state.busy || !O.api) return;
  state.busy = true;
  try {
    if (!state.list) {
      const got = await O.api("/api/mllab/states/instruments");
      state.list = got.instruments || [];
      const want = O.store ? O.store.get("mlstates.inst", null) : null;
      state.key = state.list.some((i) => i.key === want) ? want : (state.list[0] ? state.list[0].key : null);
    }
    if (state.key) state.data = await O.api("/api/mllab/states?instrument=" + encodeURIComponent(state.key));
  } catch (e) {
    state.data = { note: "The market states could not be loaded just now." };
  } finally {
    state.busy = false;
  }
  draw();
}
if (O.tabLoaders) {
  const old = O.tabLoaders.mllab;
  O.tabLoaders.mllab = () => { if (old) old(); load(); };
  if (O.tabShown && O.tabShown("mllab")) load();
}
})();
