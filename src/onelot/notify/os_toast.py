"""Native OS notifications, per platform, with honest availability reporting.

Each backend reports whether it can actually work BEFORE the first signal
fires, so a permissions problem surfaces during startup rather than at the one
moment it matters.
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
import sys

from .base import Notification, NotificationLevel

log = logging.getLogger(__name__)

# macOS sounds that exist on every install.
_MAC_SOUNDS = {
    NotificationLevel.SIGNAL: "Glass",
    NotificationLevel.EXIT: "Sosumi",
    NotificationLevel.CRITICAL: "Basso",
    NotificationLevel.WARNING: "Tink",
    NotificationLevel.INFO: "Pop",
}


def _escape_applescript(text: str) -> str:
    """AppleScript string literals: backslash and double-quote must be escaped,
    and newlines become literal \\n or osascript rejects the script."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "")


class MacToast:
    name = "macos_toast"

    def __init__(self):
        self.uses_terminal_notifier = shutil.which("terminal-notifier") is not None
        self.available = sys.platform == "darwin" and (
            self.uses_terminal_notifier or shutil.which("osascript") is not None
        )
        self.last_error: str | None = None

    def send(self, n: Notification) -> bool:
        if not self.available:
            return False
        sound = _MAC_SOUNDS.get(n.level, "Pop")
        # Toasts truncate hard; keep the body to the decision-critical lines.
        body = "\n".join(n.body.splitlines()[:3])
        try:
            if self.uses_terminal_notifier:
                cmd = ["terminal-notifier", "-title", n.title, "-message", body, "-group", "onelot"]
                if n.sound:
                    cmd += ["-sound", sound]
            else:
                script = (
                    f'display notification "{_escape_applescript(body)}" '
                    f'with title "{_escape_applescript(n.title)}"'
                )
                if n.sound:
                    script += f' sound name "{sound}"'
                cmd = ["osascript", "-e", script]
            r = subprocess.run(cmd, capture_output=True, timeout=6, text=True)
            if r.returncode != 0:
                self.last_error = (r.stderr or "").strip()[:200]
                log.warning("macOS toast failed: %s", self.last_error)
                return False
            return True
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("macOS toast error: %s", self.last_error)
            return False


class WindowsToast:
    name = "windows_toast"

    def __init__(self):
        self.available = sys.platform == "win32"
        self.backend = None
        self.last_error: str | None = None
        if not self.available:
            return
        try:
            from windows_toasts import Toast, WindowsToaster  # noqa: F401

            self.backend = "windows_toasts"
        except ImportError:
            try:
                from plyer import notification  # noqa: F401

                self.backend = "plyer"
            except ImportError:
                self.available = False
                self.last_error = "no toast backend — pip install windows-toasts"

    def send(self, n: Notification) -> bool:  # pragma: no cover - Windows only
        if not self.available:
            return False
        body = "\n".join(n.body.splitlines()[:3])
        try:
            if self.backend == "windows_toasts":
                from windows_toasts import Toast, WindowsToaster

                toaster = WindowsToaster("Bank Nifty Trade Assistant")
                t = Toast()
                t.text_fields = [n.title, body]
                toaster.show_toast(t)
                return True
            from plyer import notification

            notification.notify(title=n.title, message=body, app_name="OneLot", timeout=15)
            return True
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("Windows toast error: %s", self.last_error)
            return False


class LinuxToast:
    name = "linux_toast"

    def __init__(self):
        self.available = (
            sys.platform.startswith("linux") and shutil.which("notify-send") is not None
        )
        self.last_error: str | None = None

    def send(self, n: Notification) -> bool:  # pragma: no cover - Linux only
        if not self.available:
            return False
        body = "\n".join(n.body.splitlines()[:3])
        urgency = "critical" if n.level.is_urgent else "normal"
        try:
            r = subprocess.run(
                ["notify-send", "-u", urgency, "-a", "OneLot", n.title, body],
                capture_output=True,
                timeout=6,
            )
            return r.returncode == 0
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False


def build_os_channel():
    """Return the toast backend for this platform, or None."""
    system = platform.system()
    if system == "Darwin":
        return MacToast()
    if system == "Windows":
        return WindowsToast()
    if system == "Linux":
        return LinuxToast()
    return None
