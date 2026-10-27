"""Notifications with independent, isolated channels.

*** v1 defect D8 ***
v1 relied on `plyer.notification` alone for the one thing in the system that
must never fail silently. On macOS plyer shells out to `osascript` and no-ops
without a word when the host app lacks notification permission; on modern
Windows builds it is unreliable. A single fragile channel for a 75-second
signal window is a design flaw, not an implementation detail.

Here:
  * the DASHBOARD channel is always on and cannot fail — it is an in-process
    queue the UI polls, so even with every OS integration broken you still see
    the alert if the browser is open;
  * OS toasts are best-effort per platform, and a failure is logged, counted,
    and surfaced on the dashboard rather than swallowed;
  * one channel failing never prevents the others from delivering.
"""

from .base import Notification, NotificationLevel  # noqa: F401
from .manager import Notifier  # noqa: F401
