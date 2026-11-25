"""The Simulator's builds, off the request (US-095 point 3; audit SIM-03, F4, F6).

One worker thread builds one job at a time; at most one more job waits. A job is keyed by
what it builds (a frame key): a request for a key that is running or waiting JOINS that job.
A newer job takes the waiting place:

* a session's build (`kind="create"`) replaces a waiting build of any kind; a replaced
  session's build ends with `Job.replaced` set (its session turns FAILED with a plain
  sentence);
* a warm-up (`prepare`, `prefetch`) never replaces a waiting session's build: it is dropped
  (`Job.dropped`) and the caller may ask again later (the page polls `prepare`).

The thread is started when a job arrives and ends after `IDLE_S` seconds without work, so ten
quick requests leave one worker and nothing orphaned. `inline=True` runs every job at once in
the caller's thread (the tests, and a platform without threads to spare).

Why a thread and not a process: measured by `scripts/sim_budget.py` (the 10 ms ticker sees
the stalls a build causes in the rest of the app); see the report of US-095.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

IDLE_S = 20.0
THREAD_NAME = "sim-build"


class Job:
    def __init__(self, key: Any, fn: Callable[[Job], Any], kind: str) -> None:
        self.key = key
        self.fn = fn
        self.kind = kind
        self.step = "reading bars"
        self.queued_at = time.monotonic()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.result: Any = None
        self.error: BaseException | None = None
        self.replaced = False
        self.dropped = False
        self.done = threading.Event()
        self._then: list[Callable[[Job], None]] = []
        self._lock = threading.Lock()

    @property
    def ok(self) -> bool:
        return self.done.is_set() and self.error is None and not self.replaced and not self.dropped

    def elapsed_ms(self) -> float:
        end = self.finished_at or time.monotonic()
        return max(0.0, (end - self.queued_at) * 1000.0)

    def set_step(self, step: str) -> None:
        self.step = step

    def then(self, fn: Callable[[Job], None]) -> None:
        """Call `fn(job)` when the job ends (at once when it already has)."""
        with self._lock:
            if not self.done.is_set():
                self._then.append(fn)
                return
        fn(self)

    def _finish(self) -> None:
        self.finished_at = time.monotonic()
        with self._lock:
            self.done.set()
            then, self._then = self._then, []
        for fn in then:
            try:
                fn(self)
            except Exception:  # noqa: BLE001 - one waiter's failure never stops the worker
                pass

    def run(self) -> None:
        self.started_at = time.monotonic()
        try:
            self.result = self.fn(self)
        except BaseException as exc:  # noqa: BLE001 - the waiter is told; the worker goes on
            self.error = exc
        self._finish()


class Builder:
    def __init__(self, inline: bool = False, idle_s: float = IDLE_S) -> None:
        self.inline = inline
        self.idle_s = idle_s
        self.cv = threading.Condition()
        self.running: Job | None = None
        self.waiting: Job | None = None
        self.thread: threading.Thread | None = None
        self.threads_started = 0

    def find(self, key: Any) -> Job | None:
        with self.cv:
            for j in (self.running, self.waiting):
                if j is not None and j.key == key:
                    return j
        return None

    def submit(self, key: Any, fn: Callable[[Job], Any], kind: str = "create") -> Job:
        if self.inline:
            job = Job(key, fn, kind)
            job.run()
            return job
        gone: Job | None = None
        with self.cv:
            for j in (self.running, self.waiting):
                if j is not None and j.key == key:
                    return j  # identical keys join one build
            job = Job(key, fn, kind)
            w = self.waiting
            if w is not None and w.kind == "create" and kind != "create":
                job.dropped = True  # a warm-up never pushes a session's build out
            else:
                if w is not None:
                    w.replaced = True
                    gone = w
                self.waiting = job
                self._ensure_thread()
                self.cv.notify_all()
        if gone is not None:
            gone._finish()
        if job.dropped:
            job._finish()
        return job

    def _ensure_thread(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.threads_started += 1
        self.thread = threading.Thread(target=self._loop, name=THREAD_NAME, daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        while True:
            with self.cv:
                deadline = time.monotonic() + self.idle_s
                while self.waiting is None:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        self.thread = None
                        return
                    self.cv.wait(left)
                job, self.waiting = self.waiting, None
                self.running = job
            try:
                job.run()
            finally:
                with self.cv:
                    self.running = None
                    self.cv.notify_all()

    def idle(self, timeout: float = 10.0) -> bool:
        """Wait until nothing runs or waits (the tests and the budget script use it)."""
        end = time.monotonic() + timeout
        with self.cv:
            while self.running is not None or self.waiting is not None:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self.cv.wait(left)
        return True
