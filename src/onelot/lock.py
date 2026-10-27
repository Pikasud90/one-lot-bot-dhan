"""Single-instance lock.

*** INVARIANT I7 / v1 defect D2 ***
Two engines against one data directory means two pollers sharing one broker
quota (straight to RL001), two Guardians managing one position, and two writers
racing the same state rows. v1 had nothing preventing this — and Streamlit's
rerun model made it likely.
"""

from __future__ import annotations

import atexit
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


class AlreadyRunningError(RuntimeError):
    pass


class SingleInstanceLock:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.acquired = False

    def _pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)          # signal 0 = existence check, no effect
        except ProcessLookupError:
            return False
        except PermissionError:
            return True              # exists but owned by another user
        except OSError:
            return False
        return True

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                pid = int(self.path.read_text().strip() or 0)
            except (ValueError, OSError):
                pid = 0
            if pid and pid != os.getpid() and self._pid_alive(pid):
                raise AlreadyRunningError(
                    f"another engine is already running (PID {pid}, lock "
                    f"{self.path}). Two engines would share one broker quota and "
                    "race the same position state. Stop the other one first, or "
                    "delete the lock file if you are certain it is stale.")
            log.warning("Removing stale lock file from PID %s", pid or "unknown")
            self.path.unlink(missing_ok=True)

        self.path.write_text(str(os.getpid()))
        self.acquired = True
        atexit.register(self.release)

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            if self.path.exists() and self.path.read_text().strip() == str(os.getpid()):
                self.path.unlink()
        except OSError:
            pass
        self.acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc):
        self.release()
        return False
