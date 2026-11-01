"""Notifier: fan-out with per-channel failure isolation and de-duplication."""

from __future__ import annotations

import datetime as dt
import logging
import threading
from dataclasses import dataclass, field

from ..clock import now_ist
from .base import Notification, NotificationLevel
from .dashboard import DashboardChannel
from .os_toast import build_os_channel

log = logging.getLogger(__name__)


@dataclass
class ChannelStats:
    sent: int = 0
    failed: int = 0
    last_error: str | None = None
    last_sent_at: dt.datetime | None = None
    enqueued: int = 0  # accepted by a deferred channel; "sent" only once it is confirmed


def dedup_key_for(n: Notification) -> str:
    """The key that names what `n` is about (US-004 AC4): the signal for a SIGNAL, the position
    for an EXIT, the notification itself for a CRITICAL (two criticals with the same title are
    two events and must not swallow each other); the level is always part of it."""
    level = n.level.value.lower()
    if n.level is NotificationLevel.SIGNAL:
        subject = n.signal_id or n.notification_id
    elif n.level is NotificationLevel.EXIT:
        subject = n.position_id or n.notification_id
    elif n.level is NotificationLevel.CRITICAL:
        subject = n.notification_id
    else:
        subject = n.signal_id or n.position_id or n.notification_id
    return f"{level}:{subject}"


@dataclass
class DedupWindow:
    """Prevents an alert storm. v1 had a 60s 'TTL state lock' that conflated
    signal validity with notification de-duplication; they are different
    concerns with different durations."""

    seconds: float = 20.0
    _seen: dict[str, dt.datetime] = field(default_factory=dict)

    def should_send(self, key: str, now: dt.datetime) -> bool:
        last = self._seen.get(key)
        if last is not None and (now - last).total_seconds() < self.seconds:
            return False
        self._seen[key] = now
        return True

    def reset(self, key: str) -> None:
        self._seen.pop(key, None)


class Notifier:
    def __init__(
        self, cfg, dashboard: DashboardChannel | None = None, dedup_seconds: float = 20.0, repo=None
    ):
        self.cfg = cfg
        self.repo = repo
        self.dashboard = dashboard or DashboardChannel()
        self.channels: list = [self.dashboard]  # always first, always works
        self.stats: dict[str, ChannelStats] = {self.dashboard.name: ChannelStats()}
        self._lock = threading.Lock()
        self._dedup = DedupWindow(dedup_seconds)

        if cfg.notify.os_toast:
            ch = build_os_channel()
            if ch is not None and ch.available:
                self.channels.append(ch)
                self.stats[ch.name] = ChannelStats()
                log.info("OS notification channel enabled: %s", ch.name)
            else:
                why = getattr(ch, "last_error", None) if ch else "unsupported platform"
                log.warning(
                    "OS toast channel UNAVAILABLE (%s). The dashboard channel still "
                    "works — keep the dashboard open, or you will miss alerts.",
                    why,
                )

        # notify.telegram is wired by the Engine (US-004): it needs the database and the
        # session clock, which the Notifier does not own.
        if cfg.notify.webhook:
            log.warning(
                "notify.webhook is enabled in config but is not implemented "
                "in this build; ignoring."
            )

    # ── channels ──────────────────────────────────────────────────────────
    def add_channel(self, ch) -> None:
        """Register `ch` (its stats entry with it). A channel with the same name is replaced,
        so a name never appears twice in a result."""
        with self._lock:
            self._put(ch, ch.name)

    def replace_channel(self, name: str, ch) -> None:
        """Swap the channel called `name` for `ch` in the same slot (added at the end when
        there is none); the stats entry of `ch.name` is kept or created."""
        with self._lock:
            self._put(ch, name)

    def _put(self, ch, name: str) -> None:
        """Caller holds self._lock. A new list each time: send() iterates a snapshot."""
        channels = list(self.channels)
        slot = next((i for i, c in enumerate(channels) if c.name == name), None)
        if slot is None:
            channels.append(ch)
        else:
            channels[slot] = ch
        self.channels = channels
        self.stats.setdefault(ch.name, ChannelStats())

    def _snapshot(self) -> list:
        with self._lock:
            return list(self.channels)

    # ── sending ───────────────────────────────────────────────────────────
    def send(self, n: Notification, dedup_key: str | None = None) -> dict[str, bool]:
        """Deliver to every channel that accepts `n`. One failure never blocks the others.

        `dedup_key` here is the Notifier's own short repeat window; `n.dedup_key` (set below
        when the caller left it empty) names the alert for durable channels."""
        now = now_ist()
        n.created_at = n.created_at or now
        if n.kind == "generic":
            n.kind = n.level.value.lower()
        if n.dedup_key is None:
            n.dedup_key = dedup_key_for(n)

        if dedup_key and not self._dedup.should_send(dedup_key, now):
            log.debug("suppressed duplicate notification %s", dedup_key)
            return {}

        results: dict[str, bool] = {}
        for ch in self._snapshot():
            error: str | None = None
            try:
                accepts = getattr(ch, "accepts", None)
                if accepts is not None and not accepts(n):
                    continue
                ok = bool(ch.send(n))
            except Exception as exc:
                ok = False
                # The class only: an exception's text can quote SQL, parameters or the payload.
                error = type(exc).__name__
                log.warning("notification channel %s raised %s", ch.name, error)
            results[ch.name] = ok
            with self._lock:
                st = self.stats.setdefault(ch.name, ChannelStats())
                if ok and getattr(ch, "deferred", False):
                    st.enqueued += 1  # queued only: the channel reports its own "sent"
                elif ok:
                    st.sent += 1
                    st.last_sent_at = now
                else:
                    st.failed += 1
                    st.last_error = error or getattr(ch, "last_error", None) or st.last_error

        if self.repo is not None:
            try:
                self.repo.event(
                    now,
                    n.level.value,
                    "notification",
                    n.title,
                    {"delivered": results, "body": n.body, "signal_id": n.signal_id},
                )
            except Exception:
                log.debug("could not persist notification event", exc_info=True)

        if not any(results.values()):
            log.error("NOTIFICATION DELIVERY FAILED on every channel: %s", n.title)
        return results

    # ── convenience constructors ──────────────────────────────────────────
    def signal(self, sig) -> dict[str, bool]:
        return self.send(
            Notification(
                title=(
                    f"BUY {sig.underlying} {sig.strike:g} {sig.opt_type} "
                    f"x{sig.lots} @ ~{sig.economics.entry_eff:.2f}"
                ),
                body=sig.alert_text(),
                level=NotificationLevel.SIGNAL,
                signal_id=sig.signal_id,
                ttl_seconds=int((sig.expires_at - sig.created_at).total_seconds()),
                payload=sig.as_dict(),
            ),
            dedup_key=f"signal:{sig.signal_id}",
        )

    def exit_alert(self, position, decision) -> dict[str, bool]:
        return self.send(
            Notification(
                title=f"EXIT {position.strike:g} {position.opt_type} — "
                f"{decision.reason.value if decision.reason else 'MANUAL'}",
                body=decision.message,
                level=NotificationLevel.EXIT,
                position_id=position.position_id,
                payload=decision.as_dict(),
            ),
            # Re-alert every 30s while an exit is outstanding: an EXIT you miss
            # is worse than one you see twice.
            dedup_key=f"exit:{position.position_id}:"
            f"{decision.reason.value if decision.reason else 'x'}",
        )

    def warning(self, title: str, body: str, dedup_key: str | None = None):
        return self.send(
            Notification(title=title, body=body, level=NotificationLevel.WARNING),
            dedup_key=dedup_key,
        )

    def critical(self, title: str, body: str, dedup_key: str | None = None):
        return self.send(
            Notification(title=title, body=body, level=NotificationLevel.CRITICAL),
            dedup_key=dedup_key,
        )

    def info(self, title: str, body: str = "", dedup_key: str | None = None):
        return self.send(
            Notification(title=title, body=body, level=NotificationLevel.INFO), dedup_key=dedup_key
        )

    # ── introspection ─────────────────────────────────────────────────────
    def status(self) -> dict:
        """One entry per channel: the Notifier's counters, then the channel's own status()
        merged over them (its keys win, its name stays). Channel status is read outside the
        Notifier lock: it may query a database."""
        channels = self._snapshot()
        entries = [self._entry(ch) for ch in channels]
        return {
            "channels": entries,
            "os_toast_available": any(ch.name.endswith("toast") for ch in channels),
        }

    def _entry(self, ch) -> dict:
        with self._lock:
            st = self.stats.setdefault(ch.name, ChannelStats())
            entry = {
                "name": ch.name,
                "available": bool(getattr(ch, "available", True)),
                "sent": st.sent,
                "failed": st.failed,
                "enqueued": st.enqueued,
                "last_error": st.last_error,
                "last_sent_at": st.last_sent_at.isoformat() if st.last_sent_at else None,
            }
        own_status = getattr(ch, "status", None)
        if callable(own_status):
            try:
                entry.update(own_status())
            except Exception as exc:
                log.warning(
                    "notification channel %s status failed (%s)", ch.name, type(exc).__name__
                )
            entry["name"] = ch.name
        return entry

    def self_test(self) -> dict:
        """Fire a test alert through every channel. Run this at startup so a
        broken toast is discovered before the first real signal, not during it."""
        n = Notification(
            title="OneLot notification self-test",
            body="If you can see this, alerts are working.\nThis is a test — no action required.",
            level=NotificationLevel.INFO,
        )
        results = self.send(n)
        log.info("Notification self-test: %s", results)
        return results
