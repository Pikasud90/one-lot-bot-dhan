"""A session's prepared bars and signals (a `Frame`) and the cache that keeps them (US-095).

A frame is everything a past session computes from its bars before the first minute is shown:
the one-minute arrays, the strategy's candles and signals, the candle closes, the plan's ATR
and the strategy's lines. It depends only on its KEY: the series, the strategy, its timeframe,
the strategy parameters that the frame reads, the ATR timeframe of the plan, the window of
bars and the data version of every series it read. Two sessions with the same key share one
frame (read only: a session never writes into it). Windows are aligned on blocks of
`bars.BLOCK_DAYS` days, so every start inside one block shares the frame.

`FrameCache` keeps the most recently used frames under a cap of entries and of megabytes. A
build of a key that is being built by another thread waits for it (identical keys join).
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
import pandas as pd  # type: ignore[import-untyped]

from .bars import frame_nbytes

#: the most frames kept, and their cap in megabytes
FRAME_CACHE_ENTRIES = 12
FRAME_CACHE_MB = 256.0


@dataclass(frozen=True)
class Window:
    """The bars a frame is built on: minutes from `lo` (the plan's ATR history), the strategy's
    candles from `lo_strategy`, both to `hi` (None: to the end of the stored data)."""

    lo: pd.Timestamp | None
    lo_strategy: pd.Timestamp | None
    hi: pd.Timestamp | None


@dataclass
class Frame:
    key: tuple
    window: Window
    m: pd.DataFrame  # the one-minute bars
    ts: np.ndarray
    o: np.ndarray
    h: np.ndarray
    lo: np.ndarray
    c: np.ndarray
    v: np.ndarray
    day: np.ndarray
    mod: np.ndarray
    f: pd.DataFrame | None  # the strategy's frame (one row per strategy candle)
    candles: pd.DataFrame  # the strategy's candles
    cstart: np.ndarray
    cend: np.ndarray
    signals: dict[str, np.ndarray]  # le, se, lx, sx, lsc, ssc, fstart (empty without a frame)
    atr: np.ndarray
    atr_end: np.ndarray
    plots: dict[str, np.ndarray]  # plot id -> one value per strategy candle
    #: the candles of the whole window (the chart's history), when the strategy's are shorter
    view: pd.DataFrame | None = None
    vstart: np.ndarray | None = None
    vend: np.ndarray | None = None
    build_ms: float = 0.0
    nbytes: int = 0
    made_at: float = field(default_factory=time.monotonic)

    def size(self) -> int:
        n = frame_nbytes(self.m) + frame_nbytes(self.f) + frame_nbytes(self.candles)
        n += frame_nbytes(self.view) if self.view is not None else 0
        arrays = [self.ts, self.o, self.h, self.lo, self.c, self.v, self.day, self.mod]
        arrays += [self.cstart, self.cend, self.atr, self.atr_end]
        arrays += list(self.signals.values()) + list(self.plots.values())
        return n + sum(int(a.nbytes) for a in arrays)


class FrameCache:
    def __init__(self, entries: int = FRAME_CACHE_ENTRIES, cap_mb: float = FRAME_CACHE_MB) -> None:
        self.entries = entries
        self.cap_bytes = int(cap_mb * 1024 * 1024)
        self.lock = threading.Lock()
        self._frames: OrderedDict[tuple, Frame] = OrderedDict()
        self._building: dict[tuple, threading.Event] = {}
        self._bytes = 0
        self.builds = 0  # frames built (the tests count them)

    def __len__(self) -> int:
        with self.lock:
            return len(self._frames)

    def cached_bytes(self) -> int:
        with self.lock:
            return self._bytes

    def peek(self, key: tuple) -> Frame | None:
        with self.lock:
            return self._frames.get(key)

    def has(self, key: tuple) -> bool:
        return self.peek(key) is not None

    def drop_stale(self, series: str, versions: tuple) -> None:
        """Forget every frame of `series` built on another data version than `versions`."""
        with self.lock:
            for k in [k for k in self._frames if k[0] == series and k[-1] != versions]:
                self._bytes -= self._frames.pop(k).nbytes

    def get(self, key: tuple, build: Callable[[], Frame]) -> Frame:
        while True:
            with self.lock:
                hit = self._frames.get(key)
                if hit is not None:
                    self._frames.move_to_end(key)
                    return hit
                wait = self._building.get(key)
                if wait is None:
                    mine = threading.Event()
                    self._building[key] = mine
                    break
            wait.wait()  # another thread builds the same key: join it
        try:
            t0 = time.perf_counter()
            frame = build()
            frame.build_ms = (time.perf_counter() - t0) * 1000.0
            frame.nbytes = frame.size()
            with self.lock:
                self.builds += 1
                self._frames[key] = frame
                self._bytes += frame.nbytes
                while len(self._frames) > 1 and (
                    len(self._frames) > self.entries or self._bytes > self.cap_bytes
                ):
                    _, gone = self._frames.popitem(last=False)
                    self._bytes -= gone.nbytes
            return frame
        finally:
            with self.lock:
                self._building.pop(key, None)
            mine.set()
