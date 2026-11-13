"""Offline harness shared by the US-030 acceptance tests (paper trades on the desk).

Three surfaces are exercised, each only through its public contract (the story's Notes):

* ``GET /api/paper`` on the real FastAPI app factory (``onelot.api.server.create_app``) over a
  stub engine that carries a REAL Desk, which carries a REAL ``PaperBook`` on a throw-away
  SQLite file. Trades in every status are written straight through the schema
  (``onelot.store.schema.paper_trades``), so the tests do not depend on US-002 (closing a
  trade) being merged: CLOSED rows are inserted with exit and gross set and net NULL.
* The Paper panel's pure render helpers in ``src/onelot/ui/app.js``. They are lifted out of the
  file (top-level definitions plus whatever they call) and run in node, with no browser and no
  DOM. ``HELPER_NAMES`` is the ONE place that names them: the Manager's contract does not name
  the JS helpers, so the first alias defined in app.js wins and a rename is a one-line change.
* Static facts about ``index.html`` and ``styles.css`` (a Paper container, CSS variables only).

Nothing here touches the network, a broker, ``current_settings()`` or real credentials.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
import subprocess
import warnings
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import us001_helpers as base
from sqlalchemy import Engine

from onelot.clock import IST

ROOT = Path(__file__).resolve().parents[2]
UI_DIR = ROOT / "src" / "onelot" / "ui"
APP_JS = UI_DIR / "app.js"
INDEX_HTML = UI_DIR / "index.html"
STYLES_CSS = UI_DIR / "styles.css"

TOKEN = "test-token"
NODE = shutil.which("node")

# ── the contract, in one place ───────────────────────────────────────────────────────
STATUS_KEYS = {"opened", "filled", "no_fill", "closed", "hook_errors", "desk_hook_errors"}
TOTALS_KEYS = {"closed", "wins", "losses", "gross_rupees", "net_rupees"}
TRADE_KEYS = {
    "alert_id",
    "instrument",
    "side",
    "direction",
    "kind",
    "strategy",
    "timeframe",
    "grade",
    "status",
    "close_reason",
    "qty",
    "entry_ts",
    "entry",
    "exit",
    "gross",
    "net",
    "created_at",
    "closed_at",
}
ALL_STATUSES = ("PENDING_FILL", "OPEN", "NO_FILL", "CLOSED")
MAX_TRADES = 200

# JS helper names ASSUMED by the tests (not named in the Manager's contract). Each is a
# top-level definition in src/onelot/ui/app.js (or a src/onelot/ui/paper*.js file) that is
# a PURE function returning an HTML string:
#   chip(status)    the status chip: pending / open / no fill / closed
#   row(trade)      one trade of GET /api/paper as a table row (or card)
#   panel(payload)  the panel body for a whole GET /api/paper payload: the rows of the open
#                   trades and today's closed ones, or the empty / paper-off message
HELPER_NAMES: dict[str, tuple[str, ...]] = {
    "chip": ("paperChip", "paperStatusChip", "paperBadge", "statusChip"),
    "row": ("paperRow", "paperTradeRow", "paperRowHtml"),
    "panel": ("paperPanel", "paperHtml", "paperTable", "paperBody"),
}
# Always loaded when app.js defines them: the small formatting helpers rows are built from.
BASE_HELPERS = ("esc", "cls", "fmt", "rup", "rup2", "pct", "tsf", "hhmm", "bd", "kv", "tile")


# ══ time ═════════════════════════════════════════════════════════════════════════════
FROZEN_AT: dt.datetime | None = None  # set by freeze_clock(): "now" for the whole harness
DEFAULT_FROZEN = dt.datetime(2026, 11, 2, 11, 0, 0, tzinfo=IST)  # a Monday, mid-session


def now() -> dt.datetime:
    return FROZEN_AT or dt.datetime.now(tz=IST)


def midnight(days_ago: int = 0) -> dt.datetime:
    """00:00:00 IST of today (or `days_ago` days earlier)."""
    d = now().date() - dt.timedelta(days=days_ago)
    return dt.datetime(d.year, d.month, d.day, tzinfo=IST)


ORDER_BASE = dt.datetime(2026, 10, 20, 10, 0, 0, tzinfo=IST)  # a fixed day for ordering tests


def freeze_clock(monkeypatch, at: dt.datetime = DEFAULT_FROZEN) -> dt.datetime:
    """Make "now" `at` for the harness (`now()`, `midnight()`, the node Date) AND for the
    code under test: GET /api/paper's clock, wherever it reads it (the server's `dt`, a
    `now_ist` imported by the server or the book, or the book's own `clock`). A test that
    builds "today" rows then cannot be flaky around IST midnight."""
    import sys
    import types

    import onelot.api.server as server
    import onelot.clock as clock_module

    monkeypatch.setattr(sys.modules[__name__], "FROZEN_AT", at)

    class Frozen(dt.datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return at.astimezone(tz) if tz is not None else at.replace(tzinfo=None)

    shim = types.SimpleNamespace(**{k: getattr(dt, k) for k in dir(dt) if not k.startswith("_")})
    shim.datetime = Frozen
    monkeypatch.setattr(server, "dt", shim)
    for module in (server, clock_module):
        monkeypatch.setattr(module, "now_ist", lambda: at, raising=False)
    try:
        import onelot.paper.book as book_module

        monkeypatch.setattr(book_module, "now_ist", lambda: at, raising=False)
    except ImportError:  # pragma: no cover
        pass
    return at


def parse_ts(value: str) -> dt.datetime:
    """An ISO-8601 string as the API serves it -> an aware datetime (Z accepted)."""
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None, f"timestamp {value!r} carries no UTC offset"
    return parsed


# ══ rows ═════════════════════════════════════════════════════════════════════════════
def insert_trades(engine: Engine, rows: list[dict[str, Any]]) -> None:
    from onelot.store.schema import paper_trades

    with engine.begin() as conn:
        conn.execute(paper_trades.insert(), rows)


def trade_values(
    alert_id: str,
    *,
    status: str = "OPEN",
    instrument: str = "SBIN",
    kind: str = "STOCK",
    series: str | None = None,
    direction: str = "long",
    side: str | None = None,
    strategy: str = "ema_cross",
    timeframe: str = "10m",
    grade: str = "live",
    created: dt.datetime | None = None,
    close_reason: str | None = None,
    entry: int | None = None,
    exit: int | None = None,
    gross: int | None = None,
    costs: int | None = None,
    net: int | None = None,
    entry_ts: dt.datetime | None = None,
    exit_ts: dt.datetime | None = None,
    qty: int | None = None,
) -> dict[str, Any]:
    """One complete `paper_trades` row (money in integer paise, as the book stores it)."""
    created = created or ORDER_BASE
    alert = base.make_alert(
        alert_id,
        kind=kind,
        direction=direction,
        created=created,
        instrument_key=instrument,
        series_key=series,
        strategy=strategy,
        timeframe=timeframe,
        source="dhan" if grade == "live" else "yahoo",
    )
    if kind == "INDEX":
        side = side or ("BUY_CE" if direction == "long" else "BUY_PE")
        qty = base.LOT if qty is None else qty
        product = "index_options"
    else:
        side = side or ("BUY" if direction == "long" else "SELL")
        qty = 1 if qty is None else qty
        product = "equity_intraday"
    return {
        "alert_id": alert_id,
        "status": status,
        "close_reason": close_reason,
        "grade": grade,
        "data_source": "dhan" if grade == "live" else "yahoo",
        "instrument_key": instrument,
        "series_key": alert["series_key"],
        "kind": kind,
        "side": side,
        "direction": direction,
        "strategy": strategy,
        "timeframe": timeframe,
        "confidence": 61.5,
        "product": product,
        "qty": qty,
        "entry_ts": entry_ts,
        "fill_bar_ts": entry_ts,
        "exit_ts": exit_ts,
        "last_bar_ts": exit_ts or entry_ts,
        "resolution_min": 1,
        "entry_paise": entry,
        "exit_paise": exit,
        "gross_paise": gross,
        "costs_paise": costs,
        "net_paise": net,
        "risk_paise": None,
        "created_at": created,
        "expires_at": created + dt.timedelta(minutes=10),
        "plan": {"alert": alert, "levels": {}},
        "updated_at": exit_ts or entry_ts or created,
    }


def closed_row(
    alert_id: str, exit_ts: dt.datetime, gross: int, *, net: int | None = None, **kw: Any
) -> dict[str, Any]:
    """A CLOSED trade (US-002's output): exit and gross set, net NULL unless given."""
    entry = kw.pop("entry", 100030)
    values = trade_values(
        alert_id,
        status="CLOSED",
        close_reason=kw.pop("close_reason", "TARGET" if gross > 0 else "STOP"),
        entry=entry,
        exit=entry + gross,
        gross=gross,
        costs=None,
        net=net,
        entry_ts=exit_ts - dt.timedelta(minutes=20),
        exit_ts=exit_ts,
        created=kw.pop("created", exit_ts - dt.timedelta(minutes=25)),
        **kw,
    )
    return values


def one_row_of_every_status(day: dt.datetime) -> list[dict[str, Any]]:
    """The four statuses the book can hold, created on `day` (an aware IST datetime)."""
    return [
        trade_values("A-PENDING", status="PENDING_FILL", grade="delayed", created=day),
        trade_values(
            "A-OPEN",
            status="OPEN",
            instrument="M&M",
            created=day + dt.timedelta(seconds=1),
            entry=100030,
            entry_ts=day + dt.timedelta(minutes=1),
        ),
        trade_values(
            "A-NOFILL",
            status="NO_FILL",
            close_reason="NO_FILL",
            gross=0,
            costs=0,
            net=0,
            created=day + dt.timedelta(seconds=2),
        ),
        closed_row(
            "A-CLOSED",
            day + dt.timedelta(minutes=30),
            -1205,
            created=day + dt.timedelta(seconds=3),
            instrument="RELIANCE",
            close_reason="STOP",
        ),
    ]


# ══ the API ══════════════════════════════════════════════════════════════════════════
def realistic_rows() -> list[dict[str, Any]]:
    """A believable book on the frozen day: a trade still open from two days ago, one waiting
    since last evening, one closed today, one closed yesterday and one NO_FILL today (the book
    leaves a NO_FILL's exit time empty, as here)."""
    m = midnight()
    hour, sec = dt.timedelta(hours=1), dt.timedelta(seconds=1)
    return [
        trade_values(
            "R-OPEN",
            status="OPEN",
            instrument="M&M",
            created=m - 48 * hour,
            entry=100030,
            entry_ts=m - 48 * hour + 60 * sec,
        ),
        trade_values("R-WAIT", status="PENDING_FILL", created=m - 5 * hour),
        closed_row("R-TODAY", m + sec, -1205, instrument="RELIANCE"),
        closed_row("R-YESTERDAY", m - sec, 500, instrument="INFY"),
        trade_values(
            "R-NOFILL",
            status="NO_FILL",
            close_reason="NO_FILL",
            instrument="HDFCBANK",
            gross=0,
            costs=0,
            net=0,
            created=m + 2 * sec,
        ),
    ]


def make_app(app_cfg, db, *, enabled: bool = True, book: bool = True, desk: bool = True):
    """The real app factory over a stub engine that carries a real Desk and PaperBook.

    The stub exposes the book on `engine.desk.paper` (where the Desk holds it since US-001)
    and, as a courtesy to either wiring, on `engine.paper`; `engine.cfg.paper.enabled` agrees
    with the book's own config. Returns a namespace: client, desk, book, engine, db.
    """
    with warnings.catch_warnings():  # starlette warns that its test client prefers httpx2
        warnings.simplefilter("ignore")
        from fastapi.testclient import TestClient
    from onelot.api.server import create_app
    from onelot.config import PaperCfg

    cfg = app_cfg.model_copy(
        update={"paper": PaperCfg(**{**app_cfg.paper.model_dump(), "enabled": enabled})}
    )
    the_book = base.desk_book(cfg, db.engine, enabled=enabled) if book else None
    the_desk = base.new_desk(cfg, db, paper=the_book)[0] if desk else None
    engine = MagicMock()
    engine.desk = the_desk
    engine.paper = the_book
    engine.db = db
    engine.cfg = cfg
    client = TestClient(create_app(engine, TOKEN), raise_server_exceptions=False)
    return SimpleNamespace(client=client, desk=the_desk, book=the_book, engine=engine, db=db)


def get_paper(app, **kw):
    return app.client.get("/api/paper", **kw)


def served(app) -> dict:
    """GET /api/paper, asserted to be a 200 JSON object."""
    resp = get_paper(app)
    assert resp.status_code == 200, f"GET /api/paper -> {resp.status_code}: {resp.text[:300]}"
    body = resp.json()
    assert isinstance(body, dict), f"GET /api/paper did not return an object: {body!r}"
    return body


def trades_by_alert(body: dict) -> dict[str, dict]:
    return {t["alert_id"]: t for t in body["trades"]}


def rupees(paise: int) -> str:
    """An independent oracle for the string contract: paise -> '1000.30', exact."""
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(paise), 100)
    return f"{sign}{whole}.{frac:02d}"


# ══ HTML (what the JS helpers return) ════════════════════════════════════════════════
class Node:
    def __init__(self, tag: str, attrs: dict[str, str | None], parent: Node | None) -> None:
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children: list[Node | str] = []

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def text(self) -> str:
        return "".join(c if isinstance(c, str) else c.text() for c in self.children)

    def walk(self):
        yield self
        for c in self.children:
            if isinstance(c, Node):
                yield from c.walk()


VOID = {"br", "hr", "img", "input", "meta", "link", "col", "wbr"}
# Elements that start a new visual block: the text on either side is NOT one word.
BLOCKISH = {"td", "th", "tr", "table", "thead", "tbody", "div", "p", "li", "ul", "br", "section"}


class _Tree(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#root", {}, None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        if tag in BLOCKISH:
            self.cur.children.append(" ")
        node = Node(tag, dict(attrs), self.cur)
        self.cur.children.append(node)
        if tag not in VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        if tag in BLOCKISH:
            self.cur.children.append(" ")
        self.cur.children.append(Node(tag, dict(attrs), self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not self.root and n.tag != tag:
            n = n.parent  # type: ignore[assignment]
        if n is not self.root:
            self.cur = n.parent  # type: ignore[assignment]
            if tag in BLOCKISH:
                self.cur.children.append(" ")

    def handle_data(self, data):
        self.cur.children.append(data)


def parse_html(html: str) -> Node:
    tree = _Tree()
    tree.feed(html)
    tree.close()
    return tree.root


def visible_text(html: str) -> str:
    """The text a reader sees: tags dropped, entities decoded, whitespace collapsed."""
    return " ".join(parse_html(html).text().split())


def money_text(text: str) -> str:
    """A displayed amount reduced to plain digits: no rupee sign, grouping or space; ASCII minus."""
    return (
        text.replace("₹", "")
        .replace(",", "")
        .replace("−", "-")
        .replace("–", "-")
        .replace("+", "")
        .replace(" ", "")
        .replace(" ", "")
        .strip()
    )


def cells_showing(html: str, amount: str) -> list[Node]:
    """The innermost elements whose whole text is the rupee `amount` ('-12.05' == '−₹12.05')."""
    root = parse_html(html)
    hits = []
    for node in root.walk():
        if node is root or node.tag in ("tr", "table", "tbody", "thead"):
            continue
        if money_text(node.text()) == amount:
            hits.append(node)
    # keep the innermost: drop a node when one of its descendants is also a hit
    return [n for n in hits if not any(m is not n and _inside(m, n) for m in hits)]


def _inside(node: Node, ancestor: Node) -> bool:
    cur = node.parent
    while cur is not None:
        if cur is ancestor:
            return True
        cur = cur.parent
    return False


def classes_on_or_above(node: Node) -> set[str]:
    """Classes of the node and of its ancestors below the fragment's outermost element."""
    out: set[str] = set()
    cur: Node | None = node
    while cur is not None and cur.parent is not None and cur.parent.tag != "#root":
        out |= cur.classes
        cur = cur.parent
    return out


# ══ JS lifted out of app.js and run in node ══════════════════════════════════════════
_REGEX_AFTER = {"return", "typeof", "case", "in", "of", "delete", "void", "throw", "new", "else"}


def _skip_template(src: str, i: int) -> int:
    n, j = len(src), i + 1
    while j < n:
        ch = src[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "`":
            return j + 1
        if ch == "$" and src[j + 1 : j + 2] == "{":
            j = _scan(src, j + 2, True, None)
            continue
        j += 1
    return n


def _scan(src: str, i: int, nested: bool, out: list[tuple[int, str]] | None) -> int:
    """Walk JS code from `i` (strings, comments, regex literals and templates are opaque),
    appending (index, char) of every code character to `out`. With `nested`, stop after the
    `}` that closes a template expression. A small lexer, not a parser: enough to find the
    top-level statements of a hand-written, semicolon-terminated browser script."""
    n, depth, prev, ident = len(src), 0, "", ""
    while i < n:
        c = src[i]
        if c in " \t\r\n":
            i += 1
            continue
        two = src[i : i + 2]
        if two == "//":
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        if two == "/*":
            j = src.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if c in "'\"":
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            i, prev, ident = j + 1, "v", ""
            continue
        if c == "`":
            i, prev, ident = _skip_template(src, i), "v", ""
            continue
        if c == "/" and (prev in "(,=:[!&|?{};" or (prev == "a" and ident in _REGEX_AFTER)):
            j, in_class = i + 1, False
            while j < n:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif (ch == "/" and not in_class) or ch == "\n":
                    break
                j += 1
            i = j + 1
            while i < n and src[i].isalpha():
                i += 1
            prev, ident = "v", ""
            continue
        if c.isalnum() or c in "_$":
            j = i
            while j < n and (src[j].isalnum() or src[j] in "_$"):
                if out is not None:
                    out.append((j, src[j]))
                j += 1
            i, prev, ident = j, "a", src[i:j]
            continue
        if out is not None:
            out.append((i, c))
        prev, ident = c, ""
        if c in "({[":
            depth += 1
        elif c in ")}]":
            if c == "}" and nested and depth == 0:
                return i + 1
            depth -= 1
        i += 1
    return n


_BLOCK_KINDS = {"function", "async", "class", "if", "else", "for", "while", "try", "catch"}
_BLOCK_KINDS |= {"finally", "switch", "do"}


def top_level_statements(src: str) -> list[str]:
    """The source of every top-level statement of a script (inside its outer IIFE, if it has
    one), in file order. Declarations, functions and expression statements alike."""
    code: list[tuple[int, str]] = []
    _scan(src, 0, False, code)
    head = "".join(ch for _, ch in code[:24])  # the first code characters, comments dropped
    base_depth = 2 if re.match(r"\((async)?(\(\)=>\{|function\(\)\{)", head) else 0
    out: list[str] = []
    depth, start, kind = 0, None, ""
    for idx, ch in code:
        if depth == base_depth and start is None and ch not in ";}":
            start = idx
            word = re.match(r"[A-Za-z_$][\w$]*", src[idx:])
            kind = word.group(0) if word else ""
        if ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth -= 1
        if start is None or depth != base_depth:
            continue
        if ch == ";" or (ch == "}" and kind in _BLOCK_KINDS):
            out.append(src[start : idx + 1])
            start = None
    return out


def declared_names(statement: str) -> list[str]:
    """Names a top-level statement defines: `function f`, `const a = 1, b = 2` -> a, b."""
    m = re.match(
        r"(?:async\s+)?(?:function\s*\*?\s*|(?:const|let|var)\s+)([A-Za-z_$][\w$]*)", statement
    )
    if not m:
        return []
    names = [m.group(1)]
    if re.match(r"(?:const|let|var)\b", statement):
        code: list[tuple[int, str]] = []
        _scan(statement, 0, False, code)
        depth = 0
        for idx, ch in code:
            if ch in "({[":
                depth += 1
            elif ch in ")}]":
                depth -= 1
            elif ch == "," and depth == 0:
                more = re.match(r"\s*([A-Za-z_$][\w$]*)\s*[=,;]", statement[idx + 1 :])
                if more:
                    names.append(more.group(1))
    return names


def top_level_definitions(src: str) -> dict[str, str]:
    """name -> source of every top-level `const|let|var NAME = ...;` and `function NAME(){}`
    of a script, in file order. A multi-declarator `let a = 1, b = 2;` is under both names."""
    defs: dict[str, str] = {}
    for statement in top_level_statements(src):
        for name in declared_names(statement):
            defs[name] = statement
    return defs


def js_sources() -> list[Path]:
    return [APP_JS, *sorted(UI_DIR.glob("paper*.js"))]


def js_definitions() -> dict[str, str]:
    defs: dict[str, str] = {}
    for path in js_sources():
        defs.update(top_level_definitions(path.read_text(encoding="utf-8")))
    return defs


def resolve_helper(kind: str) -> str:
    defs = js_definitions()
    for name in HELPER_NAMES[kind]:
        if name in defs:
            return name
    raise AssertionError(
        f"no top-level JS helper for the Paper {kind!r} found in {[p.name for p in js_sources()]}: "
        f"looked for {list(HELPER_NAMES[kind])}. (Names assumed by the US-030 tests, see "
        "us030_helpers.HELPER_NAMES.)"
    )


def _closure(defs: dict[str, str], roots: list[str], skip=lambda name: False) -> list[str]:
    """`roots` plus every top-level definition they (transitively) mention, in file order.
    Names for which `skip(name)` is true are neither followed nor included."""
    wanted, todo = set(), list(roots)
    while todo:
        name = todo.pop()
        if name in wanted or name not in defs or (skip(name) and name not in roots):
            continue
        wanted.add(name)
        todo.extend(w for w in re.findall(r"[A-Za-z_$][\w$]*", defs[name]) if w in defs)
    return [n for n in defs if n in wanted]


def lifted_source(roots: list[str], *, skip=lambda name: False, extra: tuple[str, ...] = ()) -> str:
    """The roots, what they call, and then the `extra` top-level statements, as one script.
    A statement shared by several names (`let a = 1, b = 2;`) is emitted once."""
    defs = js_definitions()
    seen: set[str] = set()
    parts: list[str] = []
    for name in _closure(defs, roots, skip):
        if defs[name] not in seen:
            seen.add(defs[name])
            parts.append(defs[name])
    return "\n".join([*parts, *extra])


def statement_starting(prefix: str) -> str:
    """The one top-level statement of app.js that starts with `prefix` (e.g. 'tabLoaders.live =')."""
    found = [
        t for t in top_level_statements(APP_JS.read_text(encoding="utf-8")) if t.startswith(prefix)
    ]
    assert len(found) == 1, (
        f"expected one top-level statement starting {prefix!r}, found {len(found)}"
    )
    return found[0]


PRELUDE = r"""
const window = {}; const navigator = {}; const location = { hash: "" };
const localStorage = { getItem: () => null, setItem: () => {} };
const document = { getElementById: () => null, querySelector: () => null,
  querySelectorAll: () => [], documentElement: { dataset: {} } };
const __RealDate = Date, __frozen = new __RealDate(JSON.parse(process.argv[2]).now).getTime();
globalThis.Date = class extends __RealDate {
  constructor(...a) { if (a.length === 0) super(__frozen); else super(...a); }
  static now() { return __frozen; }
};
"""


def run_js(
    kind: str, payload: Any, *, at: dt.datetime | None = None, tz: str = "Asia/Kolkata"
) -> Any:
    """Call the Paper helper `kind` (chip | row | panel) with `payload` in node and return its
    JSON result. `at` freezes `Date` (default: the real now); `tz` is the browser's zone."""
    assert NODE, "node is not installed"
    entry = resolve_helper(kind)
    defs = js_definitions()
    body = lifted_source([entry, *[h for h in BASE_HELPERS if h in defs]])
    script = "\n".join(
        [
            PRELUDE,
            body,
            "const P = JSON.parse(process.argv[1]);",
            f"console.log(JSON.stringify({entry}(P)));",
        ]
    )
    args = json.dumps({"now": (at or now()).isoformat()})
    done = subprocess.run(
        [NODE, "-e", script, json.dumps(payload), args],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "TZ": tz},
    )
    assert done.returncode == 0, f"{entry}() failed in node:\n{done.stderr[-1500:]}"
    return json.loads(done.stdout)


# ══ the stream: applyStream() over a fake browser (clock, timers, fetch, DOM) ═════════
STREAM_STUB = re.compile(r"^(render|load)[A-Z]")  # screens other than the Paper panel
STREAM_ROOTS = ("applyStream", "esc", "hhmm", "tsf", "cls", "$")
STREAM_HARNESS = r"""
const __T0 = new Date(JSON.parse(process.argv[2]).now).getTime();
let __clock = __T0, __id = 1;
const __RealDate = Date;
globalThis.Date = class extends __RealDate {
  constructor(...a) { if (a.length === 0) super(__clock); else super(...a); }
  static now() { return __clock; }
};
const __timers = [], __fetches = [], __els = {}, __tab = { live: true, home: false };
globalThis.setTimeout = (fn, ms) => { const id = __id++; __timers.push({ at: __clock + Math.max(0, Number(ms) || 0), fn, id }); return id; };
globalThis.clearTimeout = (id) => { const i = __timers.findIndex((t) => t.id === id); if (i >= 0) __timers.splice(i, 1); };
globalThis.setInterval = () => 0;
let __fail = false, __version = 0, __delay = 0, __inflight = 0, __maxInflight = 0;
let __answer = async () => {
  if (__delay) await new Promise((r) => setTimeout(r, __delay));  // a slow server, on the fake clock
  return __fail ? { throw: "network down" }
    : { status: 200, body: { enabled: true, status: {}, trades: [], totals_today: {} } };
};
globalThis.fetch = async (url, opts) => {
  __fetches.push({ url: String(url), opts: opts || null, at: __clock - __T0, version: __version });
  __inflight++; __maxInflight = Math.max(__maxInflight, __inflight);
  let a;
  try { a = await __answer(String(url)); } finally { __inflight--; }
  if (a.throw) throw new Error(a.throw);
  return { ok: a.status < 400, status: a.status, json: async () => a.body };
};
const __el = (id) => (__els[id] = __els[id] || { id, innerHTML: "", textContent: "", className: "",
  dataset: {}, style: {}, classList: { toggle() {}, add() {}, remove() {}, contains: () => false },
  querySelectorAll: () => [], addEventListener() {}, appendChild() {}, remove() {}, set onclick(v) {} });
const window = { ONELOT_TOKEN: "t" }, navigator = {}, location = { hash: "#live" };
const localStorage = { getItem: () => null, setItem: () => {} };
const document = {
  getElementById: __el,
  querySelector: (sel) => ({ classList: { contains: (c) => /data-tab="live"/.test(sel) ? __tab.live : /data-tab="home"/.test(sel) ? __tab.home : false } }),
  querySelectorAll: () => [], documentElement: { dataset: {} },
};
const __flush = () => new Promise((r) => setImmediate(r));
async function settle() { for (let i = 0; i < 6; i++) await __flush(); }
async function advance(ms) {
  const end = __clock + ms;
  for (;;) {
    __timers.sort((a, b) => a.at - b.at || a.id - b.id);
    if (!__timers.length || __timers[0].at > end) break;
    const t = __timers.shift(); __clock = t.at; t.fn(); await settle();
  }
  __clock = end; await settle();
}
// one SSE message carrying a desk snapshot of `version` (default: the next version)
function snapshot(version) {
  __version = version === undefined ? __version + 1 : version;
  applyStream({ state: { desk: { version: __version, booting: false } }, notifications: [] });
}
function state() {
  const els = {};
  for (const [id, e] of Object.entries(__els)) els[id] = { innerHTML: e.innerHTML, textContent: e.textContent, className: e.className };
  return { fetches: __fetches, els, clock: __clock - __T0, version: __version, maxInflight: __maxInflight };
}
"""


def run_stream(
    scenario: str,
    *,
    answer: str | None = None,
    at: dt.datetime | None = None,
    extra: tuple[str, ...] = (),
    tz: str = "Asia/Kolkata",
) -> dict[str, Any]:
    """Run `scenario` (async JS, see STREAM_HARNESS) against the REAL `applyStream` and Paper
    code of app.js: every other screen is a no-op, the browser is a fake. `answer` is a JS
    expression for the fake server `__answer = (url) => ...` returning {status, body} or
    {throw}. Returns the harness `state()`: fetches [{url, opts, at(ms)}], els {id: ...}."""
    assert NODE, "node is not installed"
    defs = js_definitions()
    stubs = [n for n in defs if STREAM_STUB.match(n) and "paper" not in n.lower()]
    mentioned = [w for text in extra for w in re.findall(r"[A-Za-z_$][\w$]*", text) if w in defs]
    body = lifted_source(
        [r for r in (*STREAM_ROOTS, "tabLoaders", *mentioned) if r in defs and r not in stubs],
        skip=lambda name: name in stubs,
        extra=extra,
    )
    stub_src = "\n".join(f"function {n}() {{ return Promise.resolve(); }}" for n in stubs)
    script = "\n".join(
        [
            STREAM_HARNESS,
            stub_src,
            body,
            f"__answer = {answer};" if answer else "",
            f"(async () => {{ {scenario}\nawait settle(); console.log(JSON.stringify(state())); }})();",
        ]
    )
    done = subprocess.run(
        [NODE, "-e", script, "null", json.dumps({"now": (at or now()).isoformat()})],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "TZ": tz},
    )
    assert done.returncode == 0, f"stream scenario failed in node:\n{done.stderr[-2000:]}"
    return json.loads(done.stdout.strip().splitlines()[-1])


def html_of(
    kind: str, payload: Any, *, at: dt.datetime | None = None, tz: str = "Asia/Kolkata"
) -> str:
    out = run_js(kind, payload, at=at, tz=tz)
    assert isinstance(out, str), f"{resolve_helper(kind)}() must return an HTML string, got {out!r}"
    return out


def paper_text(state: dict[str, Any]) -> str:
    """Everything the reader sees in the Paper card after a stream scenario, lower case."""
    chunks = []
    for el_id, el in state["els"].items():
        if "paper" in el_id.lower():
            chunks += [visible_text(el["innerHTML"]), " ".join(str(el["textContent"]).split())]
    return " ".join(chunks).lower()


def paper_element(state: dict[str, Any], part: str) -> dict[str, str]:
    """The fake DOM element whose id contains both 'paper' and `part` (e.g. 'meta')."""
    hits = [e for i, e in state["els"].items() if "paper" in i.lower() and part in i.lower()]
    assert len(hits) == 1, (
        f"expected one element with 'paper' and {part!r} in its id: {list(state['els'])}"
    )
    return hits[0]


def paper_container(state: dict[str, Any]) -> dict[str, str]:
    """The element the table is written into: the paper element with HTML in it, not the card
    or the day line."""
    hits = [
        e
        for i, e in state["els"].items()
        if "paper" in i.lower() and "meta" not in i.lower() and e["innerHTML"]
    ]
    assert len(hits) == 1, f"expected the table in one paper element: {list(state['els'])}"
    return hits[0]


# ══ static UI facts ══════════════════════════════════════════════════════════════════
COLOUR_VALUE = re.compile(
    r"(?<![\w-])#[0-9a-fA-F]{3,8}(?![\w-])"
    r"|\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch)\s*\("
    r"|\b(?:white|black|red|green|blue|yellow|orange|purple|pink|gr[ae]y|cyan|magenta|silver"
    r"|gold|navy|teal|maroon|olive|lime|aqua|brown|crimson|violet|indigo|coral|salmon)\b",
    re.I,
)


def strip_css_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def css_rules(css: str) -> list[tuple[str, str]]:
    """(selector, declarations) of every rule, descending into @media / @supports blocks."""
    css = strip_css_comments(css)
    rules: list[tuple[str, str]] = []

    def walk(text: str) -> None:
        i = 0
        while i < len(text):
            j = text.find("{", i)
            if j < 0:
                return
            selector = text[i:j].strip()
            depth, k = 1, j + 1
            while k < len(text) and depth:
                depth += {"{": 1, "}": -1}.get(text[k], 0)
                k += 1
            body = text[j + 1 : k - 1]
            if selector.startswith("@media") or selector.startswith("@supports"):
                walk(body)
            elif not selector.startswith("@"):
                rules.append((selector, body))
            i = k

    walk(css)
    return rules


def colour_literals_in_values(declarations: str) -> list[str]:
    """Hard-coded colours used as VALUES of a rule (custom properties are definitions)."""
    found: list[str] = []
    for decl in declarations.split(";"):
        prop, _, value = decl.partition(":")
        prop = prop.strip().lower()
        if not value or prop in ("font-family", "content") or prop.startswith("--"):
            continue
        value = re.sub(r"url\([^)]*\)", "", value)
        value = re.sub(r"--[\w-]+", "", value)  # a variable's NAME is not a colour (var(--cyan))
        found += [m.group(0) for m in COLOUR_VALUE.finditer(value)]
    return found


THEME_SELECTORS = (":root", ':root[data-theme="night"]', ':root[data-theme="laxmi"]',
                   ':root[data-theme="ramayana"]', ':root[data-theme="batman"]')


def colour_literal_count_outside_themes(css: str) -> int:
    return sum(
        len(colour_literals_in_values(body))
        for selector, body in css_rules(css)
        if selector.strip() not in THEME_SELECTORS
    )


def mascot_block(html: str) -> str:
    """The inline <svg> sprite sheet holding Ichi and Moo (the mascots)."""
    m = re.search(r'<svg width="0" height="0".*?</svg>', html, flags=re.S)
    assert m, "the mascot sprite sheet is missing from index.html"
    return m.group(0)


def mascot_digest(html: str) -> str:
    return hashlib.sha256(mascot_block(html).encode("utf-8")).hexdigest()


def html_colour_literals_outside_mascots(html: str) -> int:
    """Colour literals in index.html markup, outside the mascot sprite and <script> blocks."""
    rest = html.replace(mascot_block(html), "")
    rest = re.sub(r"<script.*?</script>", "", rest, flags=re.S)
    return len(COLOUR_VALUE.findall(" ".join(re.findall(r'style="([^"]*)"', rest))))


def live_section(html: str) -> str:
    m = re.search(r'<section data-tab="live"[^>]*>(.*?)</section>', html, flags=re.S)
    assert m, 'index.html has no <section data-tab="live"> (the Live Desk page)'
    return m.group(1)
