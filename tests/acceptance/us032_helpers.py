"""Offline harness shared by the US-032 acceptance tests (the Paper panel's last session).

It builds on the US-030 harness (`us030_helpers`): the real app factory over a stub engine with
a real Desk and PaperBook on a tmp SQLite file, trades written through the schema, and the real
app.js code lifted into node. What US-032 adds:

* the owner's Sunday (`owners_sunday`): 67 closes on the Friday, one NO_FILL booked on the
  Sunday, an open trade from Friday, on a clock frozen on that Sunday;
* an independent oracle for a day's totals (`totals_of`), summed from integer paise;
* `run_views`: the stream harness of US-030 with the real Today | Last session | All markup of
  index.html as the fake DOM (buttons found by attribute selector, so no attribute name is
  assumed), a localStorage that can be pre-filled and made to throw, and the document
  listeners app.js registers at load, so clicks and key presses go through the real handlers;
* `run_expr`: any JS expression over the lifted definitions of ANY app.js text (the current
  file, or an older commit for a differential check).

Nothing here touches the network, a broker or real credentials.
"""

from __future__ import annotations

import datetime as dt
import json
import random
import re
import subprocess
from html.parser import HTMLParser
from typing import Any
from unittest import mock

import us030_helpers as H

from onelot.clock import IST

FRIDAY = dt.date(2026, 10, 30)  # the session the owner wants to see
SUNDAY = dt.datetime(2026, 11, 1, 12, 0, 0, tzinfo=IST)  # the clock the owner's Sunday runs on
MIN = dt.timedelta(minutes=1)
SEC = dt.timedelta(seconds=1)
HOUR = dt.timedelta(hours=1)
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

LAST_SESSION_KEYS = {"date", "totals"}
MONDAY_0920 = dt.datetime(2026, 11, 2, 9, 20, 0, tzinfo=IST)  # the owner's Monday morning
THURSDAY = dt.date(2026, 10, 29)


def at_ist(day: dt.date, hour: int, minute: int = 0, second: int = 0) -> dt.datetime:
    return dt.datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=IST)


def heading_for(day: dt.date) -> str:
    """'Fri 30 Oct': the label AC1 asks the heading to carry (never a browser-locale one)."""
    return f"{DAY_NAMES[day.weekday()]} {day.day} {MONTH_NAMES[day.month - 1]}"


# ══ an independent oracle for a day's totals ═════════════════════════════════════════
def totals_of(grosses: list[int], nets: list[int | None] | None = None) -> dict[str, Any]:
    """The totals_today shape for closed trades, from integer paise, in plain Python ints.

    A trade wins or loses on its net when it has one, else on its gross; zero is neither; a
    sum is null as soon as one trade lacks that amount, and with no trades the net is null."""
    nets = nets if nets is not None else [None] * len(grosses)
    outcomes = [n if n is not None else g for g, n in zip(grosses, nets, strict=True)]
    net_sum = None if not grosses or any(n is None for n in nets) else sum(nets)  # type: ignore[arg-type]
    return {
        "closed": len(grosses),
        "wins": sum(1 for o in outcomes if o > 0),
        "losses": sum(1 for o in outcomes if o < 0),
        "gross_rupees": H.rupees(sum(grosses)),
        "net_rupees": None if net_sum is None else H.rupees(net_sum),
    }


# ══ the owner's Sunday ═══════════════════════════════════════════════════════════════
def friday_grosses(n: int = 67, seed: int = 32) -> list[int]:
    """`n` reproducible gross amounts in paise, wins and losses, with the awkward ones in."""
    rng = random.Random(seed)
    grosses = [rng.randrange(-150000, 150000) for _ in range(n)]
    for index, awkward in ((3, 0), (10, 1), (11, -1), (20, 5), (21, -5)):
        if index < n:
            grosses[index] = awkward
    return grosses


def session_rows(
    day: dt.date,
    grosses: list[int],
    *,
    prefix: str = "FRI",
    nets: list[int | None] | None = None,
    step: int = 5,
) -> list[dict[str, Any]]:
    """One CLOSED trade per amount on IST date `day`, `step` minutes apart from 09:30 (keep
    `step` x count under 8 hours); the instrument names are `<prefix>000`, `<prefix>001`, ...
    (the later the newer)."""
    nets = nets if nets is not None else [None] * len(grosses)
    return [
        H.closed_row(
            f"{prefix}{i:03d}",
            at_ist(day, 9, 30) + i * step * MIN,
            gross,
            net=net,
            instrument=f"{prefix}{i:03d}",
        )
        for i, (gross, net) in enumerate(zip(grosses, nets, strict=True))
    ]


def no_fill_row(
    alert_id: str,
    booked_at: dt.datetime,
    *,
    created: dt.datetime | None = None,
    instrument: str = "HDFCBANK",
) -> dict[str, Any]:
    """A NO_FILL the book gave up on at `booked_at`: it leaves the exit time empty and stamps
    updated_at, so its close time is `booked_at` (the US-030 `closed_at` rule)."""
    row = H.trade_values(
        alert_id,
        status="NO_FILL",
        close_reason="NO_FILL",
        instrument=instrument,
        gross=0,
        costs=0,
        net=0,
        created=created or booked_at - 11 * MIN,
    )
    row["updated_at"] = booked_at
    return row


def sunday_no_fill() -> dict[str, Any]:
    """The NO_FILL booked on the Sunday for an alert of Friday 15:20 (a reconcile at start-up)."""
    return no_fill_row(
        "SUN-NOFILL", SUNDAY - 2 * HOUR, created=at_ist(FRIDAY, 15, 20), instrument="HDFCBANK"
    )


def friday_open_trade() -> dict[str, Any]:
    return H.trade_values(
        "FRI-OPEN",
        status="OPEN",
        instrument="TCS",
        created=at_ist(FRIDAY, 11, 0),
        entry=350025,
        entry_ts=at_ist(FRIDAY, 11, 1),
    )


def owners_sunday() -> tuple[list[dict[str, Any]], list[int]]:
    """(rows, the Friday grosses): 67 Friday closes, an open trade from Friday, and the Sunday
    NO_FILL. Run it on `SUNDAY`."""
    grosses = friday_grosses()
    return [*session_rows(FRIDAY, grosses), friday_open_trade(), sunday_no_fill()], grosses


def owners_monday() -> tuple[list[dict[str, Any]], list[int], list[int]]:
    """(rows, the Friday grosses, the Thursday grosses): Thursday's 20 closes, Friday's 67, a
    NO_FILL that ended Friday 15:31, an open trade from Friday, and the NO_FILL stamped on the
    Sunday by a start-up reconcile. Run it on `MONDAY_0920`: nothing has closed today."""
    friday = friday_grosses()
    thursday = friday_grosses(20, seed=5)
    rows = [
        *session_rows(THURSDAY, thursday, prefix="THU"),
        *session_rows(FRIDAY, friday),
        no_fill_row("FRI-NOFILL", at_ist(FRIDAY, 15, 31), instrument="INFY"),
        friday_open_trade(),
        sunday_no_fill(),
    ]
    return rows, friday, thursday


# ══ JS: any expression over the lifted definitions of any app.js text ════════════════
def run_expr(
    defs: dict[str, str],
    roots: list[str],
    expression: str,
    payload: Any = None,
    *,
    at: dt.datetime | None = None,
    tz: str = "Asia/Kolkata",
) -> Any:
    """Evaluate `expression` (`P` is the JSON payload) after the lifted `roots` of `defs`
    (name -> source, from `us030_helpers.top_level_definitions`), with `Date` frozen at `at`
    (default: the harness clock) and the browser zone `tz`. Returns the JSON result."""
    assert H.NODE, "node is not installed"
    missing = [r for r in roots if r not in defs]
    assert not missing, f"app.js defines no {missing}"
    seen: set[str] = set()
    parts = []
    for name in H._closure(defs, roots):
        if defs[name] not in seen:
            seen.add(defs[name])
            parts.append(defs[name])
    script = "\n".join(
        [
            H.PRELUDE,
            *parts,
            "const P = JSON.parse(process.argv[1]);",
            f"console.log(JSON.stringify({expression}));",
        ]
    )
    done = subprocess.run(
        [
            H.NODE,
            "-e",
            script,
            json.dumps(payload),
            json.dumps({"now": (at or H.now()).isoformat()}),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "TZ": tz},
    )
    assert done.returncode == 0, f"`{expression[:80]}` failed in node:\n{done.stderr[-1500:]}"
    return json.loads(done.stdout)


def panel(payload: Any, view: str | None = "today", **kw) -> str:
    """The real paperPanel(payload, view) of the current app.js, as HTML."""
    arg = "" if view is None else ", P.view"
    out = run_expr(
        H.js_definitions(),
        ["paperPanel"],
        f"paperPanel(P.p{arg})",
        {"p": payload, "view": view},
        **kw,
    )
    assert isinstance(out, str), f"paperPanel must return an HTML string, got {out!r}"
    return out


def meta(payload: Any, view: str | None = "today", **kw) -> str:
    """The real paperMeta(payload, view) of the current app.js, as plain text."""
    arg = "" if view is None else ", P.view"
    out = run_expr(
        H.js_definitions(),
        ["paperMeta"],
        f"paperMeta(P.p{arg})",
        {"p": payload, "view": view},
        **kw,
    )
    assert isinstance(out, str), f"paperMeta must return text, got {out!r}"
    return out


# ══ reading the panel HTML ═══════════════════════════════════════════════════════════
def text_of(html: str) -> str:
    return H.visible_text(html).lower()


def body_rows(html: str) -> list[H.Node]:
    """The trade rows of the table (the <tr>s of its <tbody> that have a status chip)."""
    root = H.parse_html(html)
    return [
        n
        for n in root.walk()
        if n.tag == "tr" and n.parent is not None and n.parent.tag == "tbody" and n.text().strip()
    ]


def chip_words(html: str) -> list[str]:
    """The status chip text of every trade row, in order (a group heading row has none)."""
    out = []
    for row in body_rows(html):
        cells = [c for c in row.children if isinstance(c, H.Node) and c.tag == "td"]
        if cells and len(cells) > 1:
            out.append(" ".join(cells[0].text().split()).lower())
    return out


def instruments(html: str) -> list[str]:
    """The instrument name (the bold text of the second cell) of every trade row, in order."""
    names = []
    for row in body_rows(html):
        cells = [c for c in row.children if isinstance(c, H.Node) and c.tag == "td"]
        if len(cells) > 1:
            bold = [n for n in cells[1].walk() if n.tag == "b"]
            names.append(" ".join((bold[0] if bold else cells[1]).text().split()))
    return names


def heading_nodes(html: str, *needles: str) -> list[H.Node]:
    """Short elements (headings, group rows) whose own text holds every needle, innermost only."""
    root = H.parse_html(html)
    hits = [
        n
        for n in root.walk()
        if n is not root
        and len(n.text()) < 200
        and all(x in " ".join(n.text().split()).lower() for x in needles)
    ]
    return [n for n in hits if not any(m is not n and H._inside(m, n) for m in hits)]


# ══ the stream harness with the real view control ════════════════════════════════════
def control_buttons(index_html: str) -> list[dict[str, Any]]:
    """The radio buttons of the Paper panel's view control as index.html declares them: each
    {text, attrs} in document order (role="radio" inside the role="radiogroup")."""

    class Finder(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.in_group = False
            self.depth = 0
            self.buttons: list[dict[str, Any]] = []
            self.current: dict[str, Any] | None = None

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if a.get("role") == "radiogroup":
                self.in_group, self.depth = True, 0
            if self.in_group:
                self.depth += 1
                if a.get("role") == "radio":
                    self.current = {"text": "", "attrs": {k: v or "" for k, v in a.items()}}
                    self.buttons.append(self.current)

        def handle_endtag(self, tag):
            if self.in_group:
                self.depth -= 1
                if tag == "button":
                    self.current = None
                if self.depth <= 0:
                    self.in_group = False

        def handle_data(self, data):
            if self.current is not None:
                self.current["text"] += data

    finder = Finder()
    finder.feed(index_html)
    return finder.buttons


VIEWS_DOM = r"""
const __stored = %(storage)s;
let __throwing = %(throwing)s, __focused = null, __prevented = 0;
const localStorage = {
  getItem: (k) => { if (__throwing) throw new Error("storage denied"); return k in __stored ? __stored[k] : null; },
  setItem: (k, v) => { if (__throwing) throw new Error("storage denied"); __stored[k] = String(v); },
};
// the radio buttons of the REAL index.html; selectors are matched against their attributes
const __attrOf = (b, name) => Object.prototype.hasOwnProperty.call(b.attrs, name) ? b.attrs[name] : null;
const __matches = (b, sel) => {
  const m = /^\[([\w-]+)(?:="([^"]*)")?\]$/.exec(sel);
  if (!m) return false;
  const v = __attrOf(b, m[1]);
  return v !== null && (m[2] === undefined || v === m[2]);
};
const __camel = (s) => s.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
const __buttons = %(buttons)s.map((spec) => {
  const b = { attrs: Object.assign({}, spec.attrs), dataset: {}, text: spec.text.trim(), focusCount: 0 };
  for (const [k, v] of Object.entries(b.attrs)) if (k.startsWith("data-")) b.dataset[__camel(k.slice(5))] = v;
  b.tabIndex = Number(b.attrs.tabindex === undefined ? 0 : b.attrs.tabindex);
  b.setAttribute = (k, v) => { b.attrs[k] = String(v); };
  b.getAttribute = (k) => __attrOf(b, k);
  b.focus = () => { __focused = b.text; b.focusCount++; };
  b.closest = (sel) => (__matches(b, sel) ? b : null);
  return b;
});
const __handlers = {};
const document = {
  getElementById: __el,
  querySelector: (sel) => {
    const hit = __buttons.find((b) => __matches(b, sel));
    if (hit) return hit;
    return { classList: { contains: (c) => /data-tab="live"/.test(sel) ? __tab.live : /data-tab="home"/.test(sel) ? __tab.home : false } };
  },
  querySelectorAll: (sel) => __buttons.filter((b) => __matches(b, sel)),
  addEventListener: (type, fn) => { (__handlers[type] = __handlers[type] || []).push(fn); },
  documentElement: { dataset: {} },
};
const __outside = { closest: () => null };
const __button = (text) => { const b = __buttons.find((x) => x.text.toLowerCase() === text.toLowerCase()); if (!b) throw new Error("no button " + text); return b; };
function click(text) { for (const fn of __handlers.click || []) fn({ target: text === null ? __outside : __button(text) }); }
function press(key, from, mods) { for (const fn of __handlers.keydown || []) fn(Object.assign({ key, target: from === null ? __outside : __button(from), preventDefault() { __prevented++; } }, mods || {})); }
"""

VIEWS_STATE = r"""
function state() {
  const els = {};
  for (const [id, e] of Object.entries(__els)) els[id] = { innerHTML: e.innerHTML, textContent: e.textContent, className: e.className };
  return {
    fetches: __fetches, els, clock: __clock - __T0, version: __version, maxInflight: __maxInflight,
    controls: {
      checked: __buttons.filter((b) => b.attrs["aria-checked"] === "true").map((b) => b.text),
      tabbable: __buttons.filter((b) => b.tabIndex === 0).map((b) => b.text),
      focused: __focused, prevented: __prevented, stored: __stored,
    },
  };
}
"""


def patched_harness(storage: dict[str, str], throwing: bool, index_html: str) -> str:
    """STREAM_HARNESS with the real view control, a scriptable localStorage and richer state."""
    buttons = control_buttons(index_html)
    assert buttons, "index.html has no role=radio buttons inside a role=radiogroup"
    harness = H.STREAM_HARNESS
    harness, n1 = re.subn(r"const localStorage = [^\n]*\n", "", harness, count=1)
    harness, n2 = re.subn(r"const document = \{.*?\n\};\n", "", harness, count=1, flags=re.S)
    harness, n3 = re.subn(r"function state\(\) \{.*?\n\}\n", "", harness, count=1, flags=re.S)
    assert (n1, n2, n3) == (1, 1, 1), "the US-030 stream harness changed shape"
    dom = VIEWS_DOM % {
        "storage": json.dumps(storage),
        "throwing": "true" if throwing else "false",
        "buttons": json.dumps(buttons),
    }
    return harness + dom + VIEWS_STATE


def load_time_statements() -> tuple[str, ...]:
    """The top-level statements of app.js that run at page load and concern the Paper panel
    (document listeners, restoring the remembered view, the tab loader): the page's own wiring."""
    src = H.APP_JS.read_text(encoding="utf-8")
    return tuple(
        s
        for s in H.top_level_statements(src)
        if not re.match(r"(const|let|var|function|async)\b", s) and re.search("paper", s, re.I)
    )


def run_views(
    scenario: str,
    *,
    answer: str | None = None,
    storage: dict[str, str] | None = None,
    throwing: bool = False,
    at: dt.datetime | None = None,
    tz: str = "Asia/Kolkata",
) -> dict[str, Any]:
    """Run `scenario` (async JS; helpers `snapshot()`, `advance(ms)`, `click(text)`,
    `press(key, fromText, {ctrlKey: true})`, ...) on the REAL page code with the REAL view control; see
    `us030_helpers.run_stream`. The page's own load-time wiring runs first. Returns state()."""
    index_html = H.INDEX_HTML.read_text(encoding="utf-8")
    harness = patched_harness(storage or {}, throwing, index_html)
    with mock.patch.object(H, "STREAM_HARNESS", harness):
        return H.run_stream(scenario, answer=answer, at=at, tz=tz, extra=load_time_statements())


def answer_with(body: Any) -> str:
    return f"() => ({{ status: 200, body: {json.dumps(body)} }})"


def paper_html(state: dict[str, Any]) -> str:
    return H.paper_container(state)["innerHTML"]


def old_app_js(commit: str) -> str | None:
    """app.js as of `commit`, or None when the history is not available."""
    try:
        done = subprocess.run(
            ["git", "show", f"{commit}:src/onelot/ui/app.js"],
            cwd=H.ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 and done.stdout else None
