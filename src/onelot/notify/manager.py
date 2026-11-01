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

        if cfg.notify.telegram:
            log.warning(
                "notify.telegram is enabled in config but is not implemented "
                "in this build; ignoring."
            )
        if cfg.notify.webhook:
            log.warning(
                "notify.webhook is enabled in config but is not implemented "
                "in this build; ignoring."
            )

    # ── sending ───────────────────────────────────────────────────────────
    def send(self, n: Notification, dedup_key: str | None = None) -> dict[str, bool]:
        """Deliver to every channel. One failure never blocks the others."""
        now = now_ist()
        n.created_at = n.created_at or now

        if dedup_key and not self._dedup.should_send(dedup_key, now):
            log.debug("suppressed duplicate notification %s", dedup_key)
            return {}

        results: dict[str, bool] = {}
        for ch in self.channels:
            try:
                ok = bool(ch.send(n))
            except Exception as exc:
                ok = False
                log.warning("notification channel %s raised: %s", ch.name, exc)
                self.stats[ch.name].last_error = f"{type(exc).__name__}: {exc}"
            results[ch.name] = ok
            st = self.stats[ch.name]
            if ok:
                st.sent += 1
                st.last_sent_at = now
            else:
                st.failed += 1
                st.last_error = st.last_error or getattr(ch, "last_error", None)

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
        return {
            "channels": [
                {
                    "name": ch.name,
                    "available": bool(getattr(ch, "available", True)),
                    "sent": self.stats[ch.name].sent,
                    "failed": self.stats[ch.name].failed,
                    "last_error": self.stats[ch.name].last_error,
                    "last_sent_at": (
                        self.stats[ch.name].last_sent_at.isoformat()
                        if self.stats[ch.name].last_sent_at
                        else None
                    ),
                }
                for ch in self.channels
            ],
            "os_toast_available": any(ch.name.endswith("toast") for ch in self.channels),
        }

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
