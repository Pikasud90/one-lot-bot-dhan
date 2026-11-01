"""The durable Telegram outbox and its worker (US-004; ADR-002 amendments 1, 4 and 6).

Every phone message is a ``telegram_outbox`` row before it is an HTTP request:

    TelegramChannel.send ──ONE short INSERT──▶ queued ──claim + COMMIT──▶ sending
        ──HTTP (no transaction open)──▶ sent | queued (retry) | failed | skipped

What is queued, and when, is decided by the channel (.telegram_channel).

Threading (amendment 6). The Desk calls ``Notifier.send`` while it holds its own RLock, so
``TelegramChannel.send`` does one INSERT and nothing else: no HTTP, no Notifier call. The worker
(one daemon thread per outbox) never takes the Desk lock, never calls the Notifier and never
keeps a transaction open across an HTTP call. Each call has a total deadline, so one hung
request cannot hold the worker for more than ``SEND_DEADLINE_S``.

Delivery is AT-LEAST-ONCE (amendment 1): a row found in ``sending`` when the worker starts was
cut off mid-call; it is queued again and its text starts with the resend marker (once, however
often it happens; never on a missed note or a digest). A duplicate on the phone is acceptable,
a lost alert is not.

Order and pacing (amendment 4). EXIT and CRITICAL first, then SIGNAL alerts by confidence (high
first), then notes; one HTTP request per ``SPACING_S`` on the injected clock, and that spacing
is the only sleep. More than ``BURST_CAP`` queued SIGNAL alerts: the best ``BURST_CAP`` stay,
the rest are folded into one digest. A row is formatted when it is sent, so "time left" is
never stale. An alert that reaches its ``expires_at`` unsent is skipped and gets one "Missed"
note; more than ``DIGEST_THRESHOLD`` of them in one pass get one digest instead.

Times come from the injected ``clock`` and are stored through ``TZDateTime`` (UTC on disk).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import ColumnElement, Connection, Engine, and_, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from ..clock import now_ist, to_ist
from ..store.schema import telegram_outbox as OUTBOX
from .base import shown_confidence
from .telegram import (
    ELLIPSIS,
    NA,
    SEP,
    SendResult,
    TelegramClient,
    _fit,
    _header,
    _short_id,
    _when,
    format_alert,
    format_exit,
    format_missed,
)

log = logging.getLogger(__name__)

OutboxKind = Literal["alert", "exit", "critical", "paper_close", "test", "missed", "digest"]
OUTBOX_KINDS: frozenset[str] = frozenset(
    ("alert", "exit", "critical", "paper_close", "test", "missed", "digest")
)

SPACING_S = 1.0  # one HTTP request per second per chat (Telegram's own limit)
MIN_RETRY_AFTER_S = 1.0
MAX_RETRY_AFTER_S = 3600.0
MAX_BACKOFF_S = 60  # transient: min(60, 2^(attempts-1)) seconds, no jitter
BURST_CAP = 8  # SIGNAL alerts sent one by one before the rest are digested
DIGEST_THRESHOLD = 8  # more expired alerts than this in one pass: one digest
DIGEST_LINES = 10  # alerts listed by name in a digest
P95_WINDOW = 100  # the last N sent rows
# The SLO is alert to phone: notes the worker makes itself (missed, digest) are sent at ~0 ms
# after they are made and would only pull the p95 down (review round 1, decision D).
SLO_KINDS = ("alert", "exit", "critical")
SEND_DEADLINE_S = 15.0  # total time one request may hold the worker
IDLE_WAIT_S = 1.0  # the worker's nap when nothing is due (a new row wakes it at once)
RESEND_MARKER = chr(0x21BB) + " resend"  # "↻ resend"
TELEGRAM_MAX_CHARS = 4096
_CAP_TITLE = 200
_CAP_BODY = 3000
_NO_MARKER = ("missed", "digest")  # notes about other rows: a resend marker would mislead
_RANK = {"exit": 0, "critical": 0, "alert": 1}  # everything else (notes) ranks 2


# ── pure helpers ──────────────────────────────────────────────────────────────
def latency_ms(sent_at: dt.datetime, created_at: dt.datetime) -> int:
    """Whole milliseconds from `created_at` to `sent_at`, half up on whole microseconds
    (exact integer arithmetic, no float), clamped at zero for an alert dated after the send."""
    micros = (sent_at - created_at) // dt.timedelta(microseconds=1)
    return 0 if micros <= 0 else (micros + 500) // 1000


def p95_nearest_rank(values: Iterable[int]) -> int | None:
    """The nearest-rank 95th percentile: the ceil(0.95 n)-th smallest value; None when empty."""
    ordered = sorted(values)
    if not ordered:
        return None
    rank = (95 * len(ordered) + 99) // 100  # ceil(0.95 n) without a float
    return ordered[rank - 1]


def _backoff_s(attempts: int) -> int:
    """min(60, 2^(attempts-1)): 1, 2, 4 ... 32, 60, 60 (the exponent is capped first)."""
    return min(MAX_BACKOFF_S, 2 ** min(max(attempts - 1, 0), 6))


def _retry_after_s(seconds: float | None) -> float:
    """max(retry_after, 1) capped at an hour; a missing value waits the minimum."""
    return min(max(seconds if seconds is not None else 0.0, MIN_RETRY_AFTER_S), MAX_RETRY_AFTER_S)


def _raw_score(payload: Mapping[str, Any]) -> Any:
    confidence = payload.get("confidence")
    return confidence.get("score") if isinstance(confidence, Mapping) else None


def _score(payload: Mapping[str, Any]) -> float | None:
    """A SIGNAL's confidence.score as a finite float for ranking, else None (ranked last):
    missing, a bool, text, NaN, inf, or an int beyond float range (never an OverflowError)."""
    raw = _raw_score(payload)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        value = float(raw)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


def _json_default(value: Any) -> Any:
    """Non-JSON values in a payload: a datetime or date as ISO, a numpy scalar as the Python
    scalar it holds (so a numpy bool stays a JSON boolean), anything else as text."""
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    item = getattr(value, "item", None)
    if callable(item):
        try:
            scalar = item()
        except (TypeError, ValueError):
            scalar = None
        if isinstance(scalar, (bool, int, float, str)):
            return scalar
    return str(value)


def _jsonable(payload: Mapping[str, Any]) -> dict[str, Any]:
    stored = json.loads(json.dumps(dict(payload), default=_json_default))
    assert isinstance(stored, dict)
    return stored


def _time_of(value: Any) -> dt.datetime | None:
    return _when(value.strip() if isinstance(value, str) else value)


# ── the extra formatters (alert, exit and missed live in .telegram) ─────────────
def format_critical(event: Mapping[str, Any], now: dt.datetime) -> str:
    """A CRITICAL as the phone shows it: the title in bold, the body on one line, the time.
    Every value is escaped and capped, so the text always fits a Telegram message."""
    title = _fit(event.get("title"), _CAP_TITLE) or "Critical alert"
    body = _fit(event.get("body"), _CAP_BODY)
    lines = [f"<b>CRITICAL{SEP}{title}</b>"]
    if body:
        lines.append(body)
    lines.append(f"Time {to_ist(now).strftime('%H:%M:%S')} IST")
    return "\n".join(lines)


def format_note(event: Mapping[str, Any], now: dt.datetime) -> str:
    """A plain titled message (kinds without a formatter of their own, e.g. 'test')."""
    title = _fit(event.get("title"), _CAP_TITLE) or "OneLot"
    body = _fit(event.get("body"), _CAP_BODY)
    return f"<b>{title}</b>\n{body}" if body else f"<b>{title}</b>"


def _digest_line(alert: Mapping[str, Any], now: dt.datetime, expired: bool) -> str:
    shown = shown_confidence(_raw_score(alert))  # floored, as on the alert and the desk
    conf = f"Conf {NA if shown is None else shown}"
    deadline = _time_of(alert.get("expires_at"))
    if deadline is None:
        window = f"till {NA}"
    elif expired or to_ist(now) >= deadline:
        window = f"expired {to_ist(deadline).strftime('%H:%M')}"
    else:
        left = int((deadline - now).total_seconds() // 60)
        window = f"till {to_ist(deadline).strftime('%H:%M')} ({left}m left)"
    return SEP.join([_header(alert), conf, window, _short_id(alert.get("id"))])


def format_digest(event: Mapping[str, Any], now: dt.datetime) -> str:
    """One message for many alerts: the count first (the phone shows it on the lock screen),
    then up to DIGEST_LINES of them, best confidence first."""
    raw = event.get("alerts")
    alerts = [a for a in raw if isinstance(a, Mapping)] if isinstance(raw, list) else []
    count = len(alerts)
    expired = event.get("reason") == "expired"
    noun = "alert" if count == 1 else "alerts"
    if expired:
        lines = [f"<b>Missed {count} {noun}</b>{SEP}they expired before delivery"]
    else:
        lines = [f"<b>{count} more {noun}</b>{SEP}lower confidence, open the desk"]
    lines += [_digest_line(a, now, expired) for a in alerts[:DIGEST_LINES]]
    if count > DIGEST_LINES:
        lines.append(f"{ELLIPSIS} and {count - DIGEST_LINES} more on the desk")
    return "\n".join(lines)


# ── rows ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _Row:
    """The columns the worker reads back."""

    id: int
    dedup_key: str
    kind: str
    alert_id: str | None
    payload: dict[str, Any]
    attempts: int
    alert_created_at: dt.datetime
    expires_at: dt.datetime | None
    next_attempt_at: dt.datetime | None
    silent: bool
    resend: bool
    confidence: float | None

    @classmethod
    def of(cls, m: Mapping[Any, Any]) -> _Row:  # a SQLAlchemy RowMapping
        payload = m["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return cls(
            id=int(m["id"]),
            dedup_key=str(m["dedup_key"]),
            kind=str(m["kind"]),
            alert_id=m["alert_id"],
            payload=payload if isinstance(payload, dict) else {},
            attempts=int(m["attempts"] or 0),
            alert_created_at=m["alert_created_at"],
            expires_at=m["expires_at"],
            next_attempt_at=m["next_attempt_at"],
            silent=bool(m["silent"]),
            resend=bool(m["resend"]),
            confidence=m["confidence"],
        )

    def due(self, now: dt.datetime) -> bool:
        return self.next_attempt_at is None or self.next_attempt_at <= now

    def expired(self, now: dt.datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def priority(self) -> tuple[int, bool, float, int]:
        """EXIT/CRITICAL, then alerts by confidence (unknown last), then notes; FIFO within."""
        score = self.confidence
        return (_RANK.get(self.kind, 2), score is None, -(score or 0.0), self.id)


# ── the outbox ────────────────────────────────────────────────────────────────
class TelegramOutbox:
    """The durable queue in front of a TelegramClient, and its single worker thread.

    `enqueue` is safe from any thread (one INSERT). `drain_once` does at most one HTTP
    request and returns the number of rows it claimed (0 or 1); the worker calls it in a
    loop, tests call it directly. `status()` is plain JSON.
    """

    def __init__(
        self,
        engine: Engine,
        client: TelegramClient,
        clock: Callable[[], dt.datetime] = now_ist,
        sleep: Callable[[float], None] = time.sleep,
        *,
        send_deadline_s: float = SEND_DEADLINE_S,
    ) -> None:
        if engine.dialect.name != "sqlite":
            raise ValueError("the Telegram outbox needs the SQLite store")
        self._engine = engine
        self._client = client
        self._clock = clock
        self._sleep = sleep
        self._deadline_s = send_deadline_s
        self._drain_lock = threading.Lock()  # one drain at a time per outbox
        self._thread_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self._wake = threading.Event()
        # The worker's own state (under _drain_lock): when the last request started, and
        # the helper of a request that outlived its deadline (at most one is ever alive).
        self._last_send_at: dt.datetime | None = None
        self._inflight: threading.Thread | None = None
        self._orphan: int | None = None  # claimed by this outbox, outcome not recorded yet
        # Written by the worker, read by status() on the dashboard's thread: both under
        # _state_lock, never under _drain_lock (status() must not wait for an HTTP call).
        self._state_lock = threading.Lock()
        self._paused_until: dt.datetime | None = None
        self._last_error: str | None = None

    # ── enqueue (the channel's one INSERT) ─────────────────────────────────
    def enqueue(
        self,
        *,
        dedup_key: str,
        kind: OutboxKind,
        alert_id: str | None,
        payload: Mapping[str, Any],
        alert_created_at: dt.datetime,
        expires_at: dt.datetime | None,
        silent: bool = False,
    ) -> bool:
        """Queue one message with a single INSERT. True when a row exists afterwards, new or
        a duplicate (same dedup_key, or same alert_id and kind): a duplicate is not a failure.
        Raises on a database error, so the caller can count a real failure."""
        if kind not in OUTBOX_KINDS:
            raise ValueError(f"unknown outbox kind {kind!r}")
        if not dedup_key:
            raise ValueError("an outbox row needs a dedup_key")
        if not isinstance(payload, Mapping):
            raise TypeError("an outbox payload must be a mapping")
        now = self._clock()
        stored = _jsonable(payload)
        values = {
            "dedup_key": dedup_key,
            "kind": kind,
            "alert_id": alert_id,
            "payload": stored,
            "status": "queued",
            "attempts": 0,
            "created_at": now,
            "alert_created_at": alert_created_at,
            "expires_at": expires_at,
            "next_attempt_at": now,
            "silent": bool(silent),
            "confidence": _score(stored) if kind == "alert" else None,
            "resend": False,
        }
        with self._engine.begin() as conn:
            conn.execute(sqlite_insert(OUTBOX).values(**values).on_conflict_do_nothing())
        self._wake.set()
        return True

    # ── the worker ─────────────────────────────────────────────────────────
    def start(self) -> None:
        """Re-queue rows left in `sending` by a previous run, then start the worker thread.
        A second call while it runs is a no-op."""
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            try:
                self._recover()
            except Exception as exc:  # the queue still drains; the stale rows wait
                log.warning("telegram outbox: recovery failed (%s)", type(exc).__name__)
            self._stopping.clear()
            self._thread = threading.Thread(target=self._run, name="telegram-outbox", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stop the worker and join it (it finishes the request in flight first)."""
        with self._thread_lock:
            thread, self._thread = self._thread, None
        if thread is None:
            return
        self._stopping.set()
        self._wake.set()
        thread.join(timeout=self._deadline_s + SPACING_S + 5.0)
        if thread.is_alive():
            log.warning("telegram outbox: the worker did not stop in time")

    def _run(self) -> None:
        failing: str | None = None  # the error class of the current streak, logged once
        while not self._stopping.is_set():
            try:
                busy = self.drain_once() > 0
                failing = None
            except Exception as exc:  # a database hiccup must not kill the worker
                if type(exc).__name__ != failing:
                    failing = type(exc).__name__
                    log.warning("telegram outbox: drain failed (%s), retrying", failing)
                busy = False
            if not busy:
                self._wake.wait(IDLE_WAIT_S)
                self._wake.clear()  # cleared BEFORE the next drain: no wake-up is lost

    def _recover(self) -> None:
        """Every row left in `sending` by a previous run is queued again (at start-up)."""
        requeued = self._requeue_sending()
        if requeued:
            log.info("telegram outbox: re-queued %d row(s) left mid-send", requeued)

    def _requeue_sending(self, *only: ColumnElement[bool]) -> int:
        """sending -> queued, at once due, for the rows matching `only` (all when empty); the
        resend flag on rows that are the message itself (never on a note about other rows).
        Returns the rows re-queued."""
        requeue = {"status": "queued", "claimed_at": None, "next_attempt_at": self._clock()}
        stale = and_(OUTBOX.c.status == "sending", *only)
        with self._engine.begin() as conn:
            marked = conn.execute(
                update(OUTBOX)
                .where(stale, OUTBOX.c.kind.not_in(_NO_MARKER))
                .values(resend=True, **requeue)
            ).rowcount
            plain = conn.execute(
                update(OUTBOX).where(stale, OUTBOX.c.kind.in_(_NO_MARKER)).values(**requeue)
            ).rowcount
        return int(marked) + int(plain)

    def _requeue_orphan(self) -> None:
        """The row this outbox claimed last whose outcome was never recorded (the update after
        the HTTP call raised): with no request in flight it is lost in `sending`, so it is
        queued again once, as at start-up (at-least-once: it may already be on the phone)."""
        if self._orphan is None or self._busy_with_a_hung_request():
            return
        row_id = self._orphan
        self._requeue_sending(OUTBOX.c.id == row_id)
        self._orphan = None
        log.warning("telegram outbox: row %d re-queued, its result was never recorded", row_id)

    # ── one step ───────────────────────────────────────────────────────────
    def drain_once(self) -> int:
        """Settle expired rows, fold an oversized burst, then claim the best due row and send
        it. Returns the rows claimed (0 or 1). Never sleeps except to keep the spacing."""
        if not self._client.available:
            return 0
        with self._drain_lock:
            self._requeue_orphan()
            now = self._clock()
            queued = self._queued()
            if self._settle_expired(queued, now):
                queued = self._queued()
            if self._fold_burst(queued, now):
                queued = self._queued()
            if self._paused(now):
                return 0  # flood control applies to the whole chat
            if self._busy_with_a_hung_request():
                return 0  # a row claimed now would only wait behind it
            for row in sorted((r for r in queued if r.due(now)), key=_Row.priority):
                claimed = self._claim(row.id, now)
                if claimed is not None:
                    self._orphan = claimed.id  # until its outcome is recorded
                    self._deliver(claimed)
                    self._orphan = None
                    return 1
            return 0

    def _paused(self, now: dt.datetime) -> bool:
        with self._state_lock:
            return self._paused_until is not None and now < self._paused_until

    def _busy_with_a_hung_request(self) -> bool:
        """True while the helper of a timed-out request is still alive. Nothing new starts
        until it ends, so one hung network holds one thread, not one per retry."""
        if self._inflight is not None and not self._inflight.is_alive():
            self._inflight = None
        return self._inflight is not None

    def _queued(self) -> list[_Row]:
        with self._engine.connect() as conn:
            result = conn.execute(
                select(OUTBOX).where(OUTBOX.c.status == "queued").order_by(OUTBOX.c.id)
            )
            return [_Row.of(m) for m in result.mappings()]

    def _claim(self, row_id: int, now: dt.datetime) -> _Row | None:
        """queued -> sending, committed before anything else happens; None if another worker
        got there first."""
        with self._engine.begin() as conn:
            taken = conn.execute(
                update(OUTBOX)
                .where(OUTBOX.c.id == row_id, OUTBOX.c.status == "queued")
                .values(status="sending", claimed_at=now, attempts=OUTBOX.c.attempts + 1)
            ).rowcount
            if taken != 1:
                return None
            m = conn.execute(select(OUTBOX).where(OUTBOX.c.id == row_id)).mappings().one()
        return _Row.of(m)

    def _deliver(self, row: _Row) -> None:
        self._pace()
        now = self._clock()
        if row.expired(now):  # it ran out during the pacing wait
            with self._engine.begin() as conn:
                if self._mark(conn, row.id, "sending", "skipped", "expired before delivery"):
                    self._notes_for(conn, [row], now)
            return
        try:
            text = self._render(row, now)
        except Exception as exc:  # a payload the formatter cannot read: never retried
            self._settle(row, now, SendResult(False, "permanent", None, None, _unrenderable(exc)))
            return
        self._last_send_at = now
        result = self._call(text, row.silent)
        self._settle(row, self._clock(), result)

    def _pace(self) -> None:
        """The only sleep: keep SPACING_S between the starts of two requests."""
        if self._last_send_at is None:
            return
        wait = SPACING_S - (self._clock() - self._last_send_at).total_seconds()
        if wait > 0:
            self._sleep(wait)

    def _render(self, row: _Row, now: dt.datetime) -> str:
        payload = row.payload
        if row.kind == "alert":
            text = format_alert(payload, now)
        elif row.kind == "exit":
            text = format_exit(payload, now)
        elif row.kind == "critical":
            text = format_critical(payload, now)
        elif row.kind == "missed":
            text = format_missed(payload, now)
        elif row.kind == "digest":
            text = format_digest(payload, now)
        else:
            text = format_note(payload, now)
        if row.resend and row.kind not in _NO_MARKER:
            text = f"{RESEND_MARKER}{SEP}{text}"
        if len(text) > TELEGRAM_MAX_CHARS:  # cannot happen with the caps above; never send it
            raise ValueError(f"message of {len(text)} characters")
        return text

    def _call(self, text: str, silent: bool) -> SendResult:
        """client.send_message with a total deadline: the request runs on a helper thread and
        a request that outlives the deadline is a transient failure (it may still arrive:
        at-least-once). The helper only talks HTTP; it never touches the database. It is the
        only one: drain_once starts no request while a timed-out helper is still alive."""
        box: list[SendResult] = []

        def work() -> None:
            try:
                box.append(self._client.send_message(text, silent=silent))
            except Exception as exc:  # the client promises not to raise; do not depend on it
                box.append(SendResult(False, "transient", None, None, _client_error(exc)))

        helper = threading.Thread(target=work, name="telegram-send", daemon=True)
        helper.start()
        helper.join(self._deadline_s)
        if box:
            return box[0]
        self._inflight = helper  # drain_once claims nothing until it ends
        detail = f"no response within {self._deadline_s:g} s"
        return SendResult(False, "transient", None, None, detail)

    def _settle(self, row: _Row, now: dt.datetime, result: SendResult) -> None:
        """Record the outcome of the request for a claimed row."""
        values: dict[str, Any]
        if result.ok:
            values = {
                "status": "sent",
                "sent_at": now,
                "message_id": result.message_id,
                "latency_ms": latency_ms(now, row.alert_created_at),
                "last_error": None,
            }
        elif result.kind == "permanent":
            values = {"status": "failed", "last_error": result.detail}
            log.warning("telegram outbox: %s row %d failed: %s", row.kind, row.id, result.detail)
        elif result.kind == "rate_limited":
            due = now + dt.timedelta(seconds=_retry_after_s(result.retry_after))
            with self._state_lock:
                self._paused_until = due
            values = {"status": "queued", "next_attempt_at": due, "last_error": result.detail}
            log.info("telegram outbox: rate limited until %s", due.isoformat())
        else:  # transient (and "unavailable", which drain_once never gets to)
            due = now + dt.timedelta(seconds=_backoff_s(row.attempts))
            values = {"status": "queued", "next_attempt_at": due, "last_error": result.detail}
            log.info("telegram outbox: %s row %d will retry: %s", row.kind, row.id, result.detail)
        with self._state_lock:  # the current error: a confirmed send clears it
            self._last_error = None if result.ok else result.detail
        with self._engine.begin() as conn:
            conn.execute(
                update(OUTBOX)
                .where(OUTBOX.c.id == row.id, OUTBOX.c.status == "sending")
                .values(claimed_at=None, **values)
            )

    # ── housekeeping ───────────────────────────────────────────────────────
    @staticmethod
    def _mark(conn: Connection, row_id: int, was: str, status: str, note: str) -> bool:
        done = conn.execute(
            update(OUTBOX)
            .where(OUTBOX.c.id == row_id, OUTBOX.c.status == was)
            .values(status=status, last_error=note, claimed_at=None)
        ).rowcount
        return bool(done == 1)

    def _settle_expired(self, queued: list[_Row], now: dt.datetime) -> bool:
        """Every queued row past its expires_at, due or not, is skipped; its alert gets a
        missed note (one digest when more than DIGEST_THRESHOLD expire together)."""
        expired = [r for r in queued if r.expired(now)]
        if not expired:
            return False
        with self._engine.begin() as conn:
            settled = [
                r
                for r in expired
                if self._mark(conn, r.id, "queued", "skipped", "expired before delivery")
            ]
            self._notes_for(conn, settled, now)
        log.info("telegram outbox: %d row(s) expired before delivery", len(settled))
        return True

    def _notes_for(self, conn: Connection, rows: list[_Row], now: dt.datetime) -> None:
        alerts = [r for r in rows if r.kind == "alert"]
        if len(alerts) > DIGEST_THRESHOLD:
            self._insert_digest(conn, "expired", alerts, now)
            return
        for r in alerts:
            self._insert_note(
                conn,
                dedup_key=f"missed:{r.dedup_key}",
                kind="missed",
                alert_id=r.alert_id,
                payload=r.payload,
                silent=r.silent,
                now=now,
            )

    def _fold_burst(self, queued: list[_Row], now: dt.datetime) -> bool:
        """More than BURST_CAP queued SIGNAL alerts: keep the best BURST_CAP, fold the rest
        into one digest (they end skipped). EXIT and CRITICAL are never folded."""
        alerts = sorted((r for r in queued if r.kind == "alert"), key=_Row.priority)
        if len(alerts) <= BURST_CAP:
            return False
        with self._engine.begin() as conn:
            folded = [
                r
                for r in alerts[BURST_CAP:]
                if self._mark(conn, r.id, "queued", "skipped", "folded into a digest")
            ]
            if folded:
                self._insert_digest(conn, "burst", folded, now)
        log.info("telegram outbox: %d lower-confidence alert(s) folded into a digest", len(folded))
        return True

    def _insert_digest(
        self, conn: Connection, reason: str, rows: list[_Row], now: dt.datetime
    ) -> None:
        ranked = sorted(rows, key=_Row.priority)
        self._insert_note(
            conn,
            dedup_key=f"digest:{reason}:{min(r.id for r in rows)}",  # a row is folded once
            kind="digest",
            alert_id=None,
            payload={"reason": reason, "alerts": [r.payload for r in ranked]},
            silent=all(r.silent for r in rows),
            now=now,
        )

    @staticmethod
    def _insert_note(
        conn: Connection,
        *,
        dedup_key: str,
        kind: str,
        alert_id: str | None,
        payload: Mapping[str, Any],
        silent: bool,
        now: dt.datetime,
    ) -> None:
        """A note made by the worker: due at once, never expires; its latency is counted
        from when it was made (it is its own message, not the alert it reports)."""
        conn.execute(
            sqlite_insert(OUTBOX)
            .values(
                dedup_key=dedup_key,
                kind=kind,
                alert_id=alert_id,
                payload=dict(payload),
                status="queued",
                attempts=0,
                created_at=now,
                alert_created_at=now,
                expires_at=None,
                next_attempt_at=now,
                silent=silent,
                confidence=None,
                resend=False,
            )
            .on_conflict_do_nothing()
        )

    # ── reading ────────────────────────────────────────────────────────────
    def exit_sequences(self) -> dict[str, int]:
        """The highest EXIT message number per position ('{position_id}#n' alert ids), so a
        restarted channel continues the numbering instead of colliding with old rows."""
        with self._engine.connect() as conn:
            ids = conn.execute(
                select(OUTBOX.c.alert_id).where(
                    OUTBOX.c.kind == "exit", OUTBOX.c.alert_id.is_not(None)
                )
            ).scalars()
            highest: dict[str, int] = {}
            for alert_id in ids:
                position, sep, number = str(alert_id).rpartition("#")
                if sep and number.isascii() and number.isdigit():
                    highest[position] = max(highest.get(position, 0), int(number))
        return highest

    def status(self) -> dict[str, Any]:
        """configured, last_ok_at (the last confirmed send of any kind), queue_depth,
        last_error (scrubbed by the client), p95_latency_ms (nearest rank over the last
        P95_WINDOW sent rows of SLO_KINDS), enqueued (rows ever queued), sent, failed,
        skipped, rate_limited_until. Plain JSON."""
        with self._engine.connect() as conn:
            by_status = conn.execute(
                select(OUTBOX.c.status, func.count(), func.max(OUTBOX.c.sent_at)).group_by(
                    OUTBOX.c.status
                )
            ).all()
            latencies = conn.execute(
                select(OUTBOX.c.latency_ms)
                .where(OUTBOX.c.status == "sent", OUTBOX.c.kind.in_(SLO_KINDS))
                .order_by(OUTBOX.c.sent_at.desc(), OUTBOX.c.id.desc())
                .limit(P95_WINDOW)
            ).scalars()
            p95 = p95_nearest_rank(int(ms) for ms in latencies if ms is not None)
        counts = {str(status): int(n) for status, n, _ in by_status}
        # max() keeps the column's TZDateTime type, so it comes back aware like the column.
        last_ok = next((at for status, _, at in by_status if status == "sent"), None)
        with self._state_lock:
            paused, last_error = self._paused_until, self._last_error
        return {
            "configured": self._client.available,
            "last_ok_at": to_ist(last_ok).isoformat() if last_ok is not None else None,
            "queue_depth": counts.get("queued", 0) + counts.get("sending", 0),
            "last_error": last_error,
            "p95_latency_ms": p95,
            "enqueued": sum(counts.values()),
            "sent": counts.get("sent", 0),
            "failed": counts.get("failed", 0),
            "skipped": counts.get("skipped", 0),
            "rate_limited_until": to_ist(paused).isoformat() if paused is not None else None,
        }


def _unrenderable(exc: Exception) -> str:
    return f"cannot format the message: {type(exc).__name__}"


def _client_error(exc: Exception) -> str:
    return f"client error: {type(exc).__name__}"  # the class only: the text may hold the URL
