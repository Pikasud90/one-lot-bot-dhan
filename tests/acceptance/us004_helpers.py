"""Shared builders and doubles for the US-004 acceptance tests (the durable Telegram outbox).

Only the story's PUBLIC CONTRACT is used: TelegramOutbox, TelegramChannel, Notification.kind /
dedup_key, Channel.accepts / status, Notifier.add_channel / replace_channel, the
``telegram_outbox`` table, NotifyCfg.telegram_levels / telegram_quiet_outside_session and the
Engine wiring. Nothing here reaches into a worker's private state.

The module under test is imported lazily through ``us003_helpers.telegram()``, so until the
story is implemented every test FAILS at run time (AttributeError / KeyError / ValidationError)
instead of the whole file erroring at collection.

Time and threads
  * ``FakeClock`` is the injected ``clock``; its ``sleep`` advances it and records the request,
    so a test reads "the only sleep enforces 1.000 s spacing" straight off ``clock.sleeps``.
  * The database is a FILE-based tmp SQLite (the worker is a thread, ``:memory:`` would not
    be shared), created with the production ``init_database``.
  * ``Rig.drain`` calls ``drain_once`` until it returns 0, so the tests hold whether a
    ``drain_once`` handles one due row or every due row.
  * Real threads appear only in the tests that need one (start/stop, engine wiring, a second
    worker). They wait on events and ``wait_until`` with a generous timeout; no test asserts
    how long anything took.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
import shutil
import sqlite3
import threading
import time
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import numpy as np
import pandas as pd
import pytest
from sqlalchemy import Engine as SqlEngine
from sqlalchemy import event, select
from us003_helpers import (
    CHAT,
    NOW,
    TOKEN,
    Recorder,
    exit_event,
    make_http,
    ok_response,
    payload_of,
    stock_buy,
    stock_position,
    telegram,
    truthy_flag,
)

from onelot.clock import IST, now_ist
from onelot.config import AppConfig, NotifyCfg
from onelot.desk import Desk
from onelot.desk.config import DeskConfig, DeskInstrumentCfg, StrategySlotCfg
from onelot.desk.data import COLUMNS, CompositeBars
from onelot.desk.tracker import Verdict
from onelot.notify.base import Notification, NotificationLevel
from onelot.notify.manager import Notifier
from onelot.risk.governor import RiskGovernor
from onelot.store import init_database
from onelot.store.repo import Repo

REPO = Path(__file__).resolve().parents[2]
RESEND = chr(0x21BB) + " resend"  # ADR-002 amendment 1: the text of a re-sent row starts with it
SECOND = dt.timedelta(seconds=1)
MINUTE = dt.timedelta(minutes=1)


# ========================= the injected clock ===========================================
class FakeClock:
    """A thread-safe clock whose ``sleep`` advances it (and records every request)."""

    def __init__(self, start: dt.datetime = NOW) -> None:
        self._now = start
        self._lock = threading.Lock()
        self.sleeps: list[float] = []

    def __call__(self) -> dt.datetime:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> dt.datetime:
        with self._lock:
            self._now += dt.timedelta(seconds=seconds)
            return self._now

    def set(self, when: dt.datetime) -> None:
        with self._lock:
            self._now = when

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(float(seconds))
            if seconds > 0:
                self._now += dt.timedelta(seconds=seconds)


# ========================= the HTTP double ==============================================
class TimedRecorder(Recorder):
    """Records every request together with the fake-clock time it arrived at."""

    def __init__(
        self,
        respond: Callable[[httpx.Request], httpx.Response] | None,
        clock: Callable[[], dt.datetime],
    ) -> None:
        self._counter = 0
        self._clock = clock
        self._times_lock = threading.Lock()
        self.times: list[dt.datetime] = []
        super().__init__(respond or self._default)

    def _default(self, request: httpx.Request) -> httpx.Response:
        with self._times_lock:
            self._counter += 1
            return ok_response(1000 + self._counter)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self._times_lock:
            self.times.append(self._clock())
        return super().__call__(request)

    def texts(self) -> list[str]:
        return [payload_of(r)["text"] for r in list(self.requests)]

    def silent(self) -> list[bool]:
        return [
            truthy_flag(payload_of(r).get("disable_notification", False)) for r in self.requests
        ]

    def offsets(self, origin: dt.datetime) -> list[float]:
        return [(t - origin).total_seconds() for t in self.times]


def scripted(*outcomes: Any) -> Callable[[httpx.Request], httpx.Response]:
    """A MockTransport handler that plays `outcomes` in order, repeating the last one. An outcome
    is a Response, an exception (raised) or a zero-argument callable returning either."""
    lock = threading.Lock()
    position = [0]

    def respond(request: httpx.Request) -> httpx.Response:
        with lock:
            outcome = outcomes[min(position[0], len(outcomes) - 1)]
            position[0] += 1
        if callable(outcome):
            outcome = outcome()
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome.status_code, content=outcome.content, headers=outcome.headers)

    return respond


class Outage:
    """A transport that is down (502 Bad Gateway) until `down` is cleared. `calls` counts every
    request, `delivered` holds the texts of the ones that were answered 200: what the phone
    actually received (rig.rec.texts() also lists the attempts that failed)."""

    def __init__(self, down: bool = True) -> None:
        self.down = down
        self.calls = 0
        self.delivered: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.down:
            return httpx.Response(502, json={"ok": False, "description": "Bad Gateway"})
        self.delivered.append(payload_of(request)["text"])
        return ok_response(1000 + self.calls)


# ========================= the table ====================================================
def outbox_table() -> Any:
    from onelot.store.schema import METADATA

    return METADATA.tables["telegram_outbox"]


def when(value: Any) -> dt.datetime | None:
    """An aware datetime from a stored value (naive means IST, the project rule)."""
    if value is None:
        return None
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value)
    return value.replace(tzinfo=IST) if value.tzinfo is None else value


def read_rows(engine: SqlEngine) -> list[dict[str, Any]]:
    """Every telegram_outbox row, oldest first, with the JSON payload decoded."""
    table = outbox_table()
    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(select(table).order_by(table.c.id)).mappings()]
    for row in rows:
        if isinstance(row.get("payload"), str):
            row["payload"] = json.loads(row["payload"])
    return rows


def probe(path: Path, sql: str | None = None) -> list[tuple[Any, ...]]:
    """Read the file from a SEPARATE sqlite3 connection (sees only what is committed)."""
    sql = sql or "SELECT status, kind, alert_id FROM telegram_outbox ORDER BY id"
    conn = sqlite3.connect(str(path), timeout=0.5)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def can_write(path: Path) -> bool:
    """True when ANOTHER connection can take the write lock right now (0.3 s patience), i.e.
    no transaction of the code under test is open."""
    conn = sqlite3.connect(str(path), timeout=0.3, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


@contextmanager
def sql_statements(engine: SqlEngine) -> Iterator[list[str]]:
    """Every statement the engine executes inside the block."""
    seen: list[str] = []

    def before(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", before)


def wait_until(predicate: Callable[[], bool], timeout: float = 20.0) -> bool:
    """Poll a condition driven by a REAL thread. The timeout only bounds a failing test."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# ========================= fixtures: alerts and notifications ===========================
def alert_id(i: int) -> str:
    """12 hex characters whose FIRST SIX differ for every i (the phone shows the first six)."""
    return f"{0xA1B000 + i:06x}{i:06x}"


def alert_at(
    now: dt.datetime,
    *,
    id: str | None = None,
    score: float = 71.6,
    minutes: float = 10.0,
    ttl: dt.timedelta | None = None,
    blocked: bool = False,
    delayed: bool = False,
) -> dict[str, Any]:
    """The US-003 stock BUY fixture, created at `now` and valid for `minutes` (or `ttl`)."""
    a = copy.deepcopy(stock_buy())
    a["id"] = id or a["id"]
    a["created_at"] = now.isoformat()
    a["expires_at"] = (now + (ttl or dt.timedelta(minutes=minutes))).isoformat()
    a["confidence"]["score"] = score
    if blocked:
        a["risk_allowed"] = False
        a["risk_reason"] = "daily loss limit reached"
    if delayed:
        a["data"].update(source="yahoo", delayed=True)
    return a


def exit_payload(pid: str, unrealized: float, advice: str = "STOP") -> dict[str, Any]:
    """The payload Desk._manage_positions sends: {"advice": ..., "position": {...}}."""
    return exit_event(stock_position(id=pid, unrealized=unrealized), advice)


def signal_notification(alert: dict[str, Any], created_at: dt.datetime) -> Notification:
    """Built as Desk._raise_signal builds it (level, signal_id, ttl, payload=the alert)."""
    return Notification(
        title=f"BUY {alert['label']}",
        body=str(alert["text"]),
        level=NotificationLevel.SIGNAL,
        signal_id=alert["id"],
        ttl_seconds=int(
            (dt.datetime.fromisoformat(alert["expires_at"]) - created_at).total_seconds()
        ),
        created_at=created_at,
        payload=alert,
    )


def exit_notification(
    pid: str, unrealized: float, created_at: dt.datetime, advice: str = "STOP"
) -> Notification:
    """Built as Desk._manage_positions builds it (level EXIT, position_id, payload)."""
    return Notification(
        title=f"EXIT State Bank of India - {advice}",
        body="Close it at the market and record the exit price.",
        level=NotificationLevel.EXIT,
        position_id=pid,
        created_at=created_at,
        payload=exit_payload(pid, unrealized, advice),
    )


def critical_notification(
    title: str, body: str = "", created_at: dt.datetime | None = None
) -> Notification:
    return Notification(
        title=title, body=body, level=NotificationLevel.CRITICAL, created_at=created_at
    )


MISSING = object()


def find_key(obj: Any, key: str) -> Any:
    """The first value stored under `key` anywhere in a nested dict (MISSING if none): the
    tests care that the alert's JSON booleans survive, not whether the outbox wraps the alert."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = find_key(value, key)
            if found is not MISSING:
                return found
    return MISSING


def holds_alert(payload: Any, alert: dict[str, Any]) -> bool:
    """True when the row's payload IS the alert, or carries it whole under some key."""
    if payload == alert:
        return True
    return isinstance(payload, dict) and any(holds_alert(v, alert) for v in payload.values())


def code_ids(text: str) -> list[str]:
    """The <code>...</code> short ids of a message."""
    return re.findall(r"<code>([^<]*)</code>", text)


def mentions_count(text: str, n: int) -> bool:
    """True when the phone-visible text states the number `n` on its own (not inside 12, 4:30)."""
    plain = re.sub(r"<[^>]+>", "", text)
    return re.search(rf"(?<![0-9A-Za-z:.,]){n}(?![0-9A-Za-z:.,])", plain) is not None


# ========================= the rig: database + client + outbox ==========================
class Rig:
    """One file-based database, one fake clock, one MockTransport client, one outbox."""

    def __init__(
        self,
        tmp_path: Path,
        index: int,
        respond: Callable[[httpx.Request], httpx.Response] | None,
        clock: FakeClock,
        token: str,
        chat: str,
        outbox_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.path = tmp_path / f"outbox{index}.db"
        self.db = init_database(f"sqlite:///{self.path}")
        self.clock = clock
        self._token, self._chat = token, chat
        self._outbox_kwargs = dict(outbox_kwargs or {})
        self._extra: list[Any] = []
        try:
            self._build(respond)
        except BaseException:
            self.db.dispose()
            raise

    def _build(self, respond: Callable[[httpx.Request], httpx.Response] | None) -> None:
        self.rec = TimedRecorder(respond, self.clock)
        self.client = telegram().TelegramClient(self._token, self._chat, http=make_http(self.rec))
        self.outbox = telegram().TelegramOutbox(
            self.db.engine,
            self.client,
            clock=self.clock,
            sleep=self.clock.sleep,
            **self._outbox_kwargs,
        )

    # -- queueing ---------------------------------------------------------------------
    def enqueue_alert(
        self,
        alert: dict[str, Any],
        *,
        key: str | None = None,
        silent: bool = False,
        outbox: Any = None,
    ) -> bool:
        return (outbox or self.outbox).enqueue(
            dedup_key=key or f"alert:{alert['id']}",
            kind="alert",
            alert_id=alert["id"],
            payload=alert,
            alert_created_at=dt.datetime.fromisoformat(alert["created_at"]),
            expires_at=dt.datetime.fromisoformat(alert["expires_at"]),
            silent=silent,
        )

    def drain(self, outbox: Any = None, limit: int = 60) -> int:
        """drain_once until it has nothing left to do; returns the rows it processed."""
        worker = outbox or self.outbox
        total = 0
        for _ in range(limit):
            done = worker.drain_once()
            if done == 0:
                return total
            total += done
        raise AssertionError(f"drain_once still had work after {limit} calls (an endless retry?)")

    # -- reading ----------------------------------------------------------------------
    def rows(self) -> list[dict[str, Any]]:
        return read_rows(self.db.engine)

    def row(self, **match: Any) -> dict[str, Any]:
        found = [r for r in self.rows() if all(r[k] == v for k, v in match.items())]
        assert len(found) == 1, f"expected exactly one row with {match}, found {len(found)}"
        return found[0]

    def statuses(self) -> list[str]:
        return [r["status"] for r in self.rows()]

    # -- more workers on the same database ---------------------------------------------
    def another_outbox(
        self,
        respond: Callable[[httpx.Request], httpx.Response] | None = None,
        clock: FakeClock | None = None,
        **outbox_kwargs: Any,
    ) -> tuple[Any, TimedRecorder]:
        """A second TelegramOutbox (a "restarted" process, or a competing worker) on the SAME
        database file, with its own recorder."""
        clk = clock or self.clock
        rec = TimedRecorder(respond, clk)
        client = telegram().TelegramClient(TOKEN, CHAT, http=make_http(rec))
        worker = telegram().TelegramOutbox(
            self.db.engine, client, clock=clk, sleep=clk.sleep, **outbox_kwargs
        )
        self._extra.append(worker)
        return worker, rec

    def close(self) -> None:
        for worker in [*self._extra, self.outbox]:
            try:
                worker.stop()
            except Exception:  # noqa: BLE001 - teardown must not hide the real failure
                pass
        self.db.dispose()


@contextmanager
def rig_factory(tmp_path: Path) -> Iterator[Callable[..., Rig]]:
    made: list[Rig] = []

    def build(
        respond: Callable[[httpx.Request], httpx.Response] | None = None,
        *,
        clock: FakeClock | None = None,
        token: str = TOKEN,
        chat: str = CHAT,
        **outbox_kwargs: Any,
    ) -> Rig:
        rig = Rig(tmp_path, len(made), respond, clock or FakeClock(), token, chat, outbox_kwargs)
        made.append(rig)
        return rig

    try:
        yield build
    finally:
        for rig in reversed(made):
            rig.close()


# ========================= the Notifier side ============================================
class SessionProbe:
    """The `in_session` callable: a settable answer that records the times it was asked about."""

    def __init__(self, value: bool = True) -> None:
        self.value = value
        self.calls: list[dt.datetime] = []

    def __call__(self, moment: dt.datetime) -> bool:
        self.calls.append(moment)
        return self.value


class RecordingChannel:
    """A stand-in channel (the OS toast, say): records what it gets, optionally has its own
    accepts / status."""

    available = True

    def __init__(
        self,
        name: str = "toast",
        ok: bool = True,
        own_status: dict[str, Any] | None = None,
        accepts: Callable[[Notification], bool] | None = None,
    ) -> None:
        self.name = name
        self.ok = ok
        self.sent: list[Notification] = []
        if own_status is not None:
            self.status = lambda: dict(own_status)  # type: ignore[method-assign]
        if accepts is not None:
            self.accepts = accepts  # type: ignore[method-assign]

    def send(self, n: Notification) -> bool:
        self.sent.append(n)
        return self.ok


def build_notifier(
    rig: Rig,
    *,
    in_session: Callable[[dt.datetime], bool] | None = None,
    levels: list[str] | None = None,
    quiet: bool | None = None,
    extra: tuple[Any, ...] = (),
) -> tuple[Notifier, Any]:
    """A Notifier (dashboard + `extra` channels, no real OS toast) with the Telegram channel
    added the way the Engine adds it. The Notifier's own 20 s de-duplication window is off,
    so a test can raise the same alert as often as it likes (the real Desk polls every minute)."""
    options: dict[str, Any] = {"os_toast": False}
    if levels is not None:
        options["telegram_levels"] = levels
    if quiet is not None:
        options["telegram_quiet_outside_session"] = quiet
    notify_cfg = NotifyCfg(**options)
    notifier = Notifier(SimpleNamespace(notify=notify_cfg), dedup_seconds=0.0)
    for channel in extra:
        notifier.add_channel(channel)
    channel = telegram().TelegramChannel(
        rig.client, rig.outbox, notify_cfg, in_session or SessionProbe(True), clock=rig.clock
    )
    notifier.add_channel(channel)
    return notifier, channel


def bare_notifier(os_toast: bool = False, **notify_fields: Any) -> Notifier:
    """A Notifier with only the dashboard (and whatever the flags in `notify_fields` ask for)."""
    cfg = SimpleNamespace(notify=NotifyCfg(os_toast=os_toast, **notify_fields))
    return Notifier(cfg, dedup_seconds=0.0)


def level_names(values: Any) -> list[str]:
    """['SIGNAL', ...] whether the config stores strings or NotificationLevel members."""
    return [str(getattr(v, "value", v)) for v in values]


# ========================= the Engine, hermetic =========================================
HEAVY_ENGINE_STEPS = (
    "_build_broker",
    "_resolve_instruments",
    "_start_feed",
    "_load_regime",
    "_bar_loop",
    "_guardian_loop",
    "_housekeeping_loop",
)
ENGINE_THREAD_NAMES = ("bar-loop", "guardian-loop", "housekeeping")


class StubDesk:
    def __init__(self, *args: Any, **kwargs: Any) -> None: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...


@dataclass
class EngineRig:
    engine: Any
    rec: TimedRecorder
    db_path: Path
    from_env_calls: list[Any]

    def worker_threads(self, before: set[threading.Thread]) -> list[threading.Thread]:
        """Threads started since `before`, minus the Engine's own (stubbed, instant) loops."""
        return [
            t
            for t in threading.enumerate()
            if t not in before and t.is_alive() and t.name not in ENGINE_THREAD_NAMES
        ]


def patch_from_env(monkeypatch: pytest.MonkeyPatch, http: httpx.Client) -> list[Any]:
    """TelegramClient.from_env() keeps reading the (monkeypatched) environment, but the client
    it builds talks to a MockTransport instead of the network. Returns the list of its calls."""
    cls = telegram().TelegramClient
    real = cls.from_env
    calls: list[Any] = []

    def from_env(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, dict(kwargs)))
        kwargs["http"] = http
        return real(*args, **kwargs)

    monkeypatch.setattr(cls, "from_env", staticmethod(from_env))
    return calls


def make_engine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    token: str | None = TOKEN,
    chat: str | None = CHAT,
    telegram_flag: bool | None = True,
) -> EngineRig:
    """The REAL Engine on a throw-away project root (copy of config/), with everything that
    would touch a broker, a feed or Yahoo replaced by a no-op. What is left is exactly what
    this story changes: the Notifier, the database and the start / stop sequence.

    `telegram_flag` is notify.telegram: True (the default here) or False are set explicitly,
    None keeps whatever config/config.yaml ships (decided: true)."""
    import onelot.data.db as data_db
    import onelot.desk as desk_package
    from onelot.config import AppConfig, Secrets
    from onelot.engine import Engine

    root = tmp_path / "project"
    shutil.copytree(REPO / "config", root / "config")
    for var in ("DB_URL", "ONELOT_CONFIG", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)
    if token is not None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    if chat is not None:
        monkeypatch.setenv("TELEGRAM_CHAT_ID", chat)
    cfg = AppConfig.load(root / "config" / "config.yaml", project_root=root)
    notify = {"os_toast": False}
    if telegram_flag is not None:
        notify["telegram"] = telegram_flag
    cfg = cfg.model_copy(update={"notify": cfg.notify.model_copy(update=notify)})

    rec = TimedRecorder(None, FakeClock())
    from_env_calls = patch_from_env(monkeypatch, make_http(rec))
    for name in HEAVY_ENGINE_STEPS:
        monkeypatch.setattr(Engine, name, lambda self: None)
    monkeypatch.setattr(desk_package, "Desk", StubDesk)
    monkeypatch.setattr(data_db, "_db", None)  # Engine.__init__ points the Lab at its database
    engine = Engine(cfg, Secrets(), use_mock=True)
    return EngineRig(engine, rec, root / "data" / "onelot.db", from_env_calls)


# ========================= the Desk harness (synthetic bars, no network) ================
# A compact copy of how tests/desk/test_desk.py drives the real Desk.
def _synthetic(
    days: int = 12, base: float = 1000.0, seed: int = 3, end: Any = None
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    end = end or pd.Timestamp.now(tz=IST).floor("2min")
    day, sessions = end.normalize(), []
    while len(sessions) < days:
        if day.weekday() < 5:
            sessions.append(day)
        day -= pd.Timedelta(days=1)
    idx: list[pd.Timestamp] = []
    for d in sorted(sessions):
        idx.extend(pd.date_range(d + pd.Timedelta(hours=9, minutes=15), periods=188, freq="2min"))
    index = pd.DatetimeIndex([t for t in idx if t + pd.Timedelta(minutes=2) <= end])
    n = len(index)
    close = base * np.exp(np.cumsum(rng.normal(0.0, 0.0012, n)))
    open_ = np.r_[base, close[:-1]]
    spread = np.abs(rng.normal(0, 0.0012, n)) * close
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) + spread,
            "low": np.minimum(open_, close) - spread,
            "close": close,
            "volume": rng.integers(1000, 5000, n).astype(float),
        },
        index=index,
    )


def _yesterday_close() -> pd.Timestamp:
    return (pd.Timestamp.now(tz=IST).normalize() - pd.Timedelta(days=1)) + pd.Timedelta(
        hours=15, minutes=30
    )


class StubBars:
    """A bar source that serves synthetic bars (none for the keys in `empty_for`)."""

    delayed = False
    resolution = "2m"
    last_error = None

    def __init__(self, name: str, salt: int, empty_for: set[str] | None = None) -> None:
        self.name = name
        self.salt = salt
        self.empty_for = empty_for or set()

    def fetch(self, specs: Any, days: int, since: Any = None) -> dict[str, pd.DataFrame]:
        end = _yesterday_close()
        return {
            k: pd.DataFrame(columns=COLUMNS)
            if k in self.empty_for
            else _synthetic(seed=(zlib.crc32(k.encode()) + self.salt) % 1000, end=end)
            for k in specs
        }


class OpenClock:
    """A session clock whose window is always open (what Desk._manage_positions asks for)."""

    class W:
        def __init__(self, now: dt.datetime) -> None:
            self.open_at = now - dt.timedelta(hours=1)
            self.close_at = now + dt.timedelta(hours=3)
            self.mandatory_squareoff_at = now + dt.timedelta(hours=2)

        def is_open(self, ts: Any) -> bool:
            return True

        def entries_allowed(self, ts: Any) -> bool:
            return True

        def past_squareoff(self, ts: Any) -> bool:
            return False

    def current_window(self, ts: dt.datetime | None = None) -> Any:
        return OpenClock.W(ts or now_ist())


def desk_config(key: str = "SBIN", stock_horizon: str = "swing") -> DeskConfig:
    return DeskConfig(
        poll_seconds=60,
        history_days=10,
        source="yahoo",
        signal_valid_minutes=10,
        stock_horizon=stock_horizon,
        context=(),
        indices=(),
        stocks=(DeskInstrumentCfg(key, key, "STOCK", (StrategySlotCfg("ema_cross", "10m"),)),),
    )


def build_desk(
    db: Any,
    notifier: Any,
    *,
    monkeypatch: pytest.MonkeyPatch,
    delayed: bool = False,
    blocked: bool = False,
    key: str = "SBIN",
    stock_horizon: str = "swing",
) -> Any:
    """The real Desk on `db` (any Database, so it can share a file with the outbox), polled
    once so its tracker has bars. `delayed`: Dhan has no bars for `key`, Yahoo serves it;
    `blocked`: the risk governor is halted."""
    cfg = AppConfig.load()
    repo = Repo(db)
    governor = RiskGovernor(
        cfg.risk, cfg.account.capital_inr, cfg.mode, now_ist().date().isoformat(), repo
    )
    desk = Desk(
        db.engine,
        cfg,
        notifier,
        governor,
        OpenClock(),
        repo=repo,
        desk_cfg=desk_config(key, stock_horizon),
    )
    primary = StubBars("dhan", 0, empty_for={key} if delayed else set())
    desk.source = CompositeBars(primary, StubBars("yahoo", 1))
    desk.fine_minutes = 2
    monkeypatch.setattr(desk, "_maybe_switch_source", lambda: None)
    desk.poll()
    if blocked:
        governor.manual_panic_halt()
    return desk


def raise_signal(
    desk: Any, now: dt.datetime, *, key: str = "SBIN", direction: str = "long"
) -> dict[str, Any]:
    """Make the Desk raise one signal on `key` (as the strategy would) and return the alert."""
    t = desk.trackers[key]
    close = float(t.bars["close"].iloc[-1])
    stop = close * (0.99 if direction == "long" else 1.01)
    verdict = Verdict(
        direction, "test", t.bars.index[-1].to_pydatetime(), stop, close, 60.0, [], True
    )
    desk._raise_signal(t, t.slots[0], verdict, now)
    return list(desk.signals.values())[-1]
