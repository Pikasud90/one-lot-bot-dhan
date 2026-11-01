"""Historical-data provider interface. Every provider returns bars indexed by bar START time (IST)."""

from __future__ import annotations

import datetime as dt
from typing import Protocol

import pandas as pd

BAR_COLUMNS = ["open", "high", "low", "close", "volume"]


class HistoryProvider(Protocol):
    name: str

    def supported_resolutions(self) -> list[str]: ...

    def earliest_available(self, resolution: str, today: dt.date) -> dt.date | None:
        """Earliest date this provider can serve at `resolution` (None = unlimited)."""
        ...

    def history(
        self, symbol: str, alt_symbol: str | None, resolution: str, start: dt.date, end: dt.date
    ) -> pd.DataFrame:
        """Bars for [start, end] inclusive. `symbol` is the Fyers symbol, `alt_symbol` the provider's own."""
        ...


def empty_bars() -> pd.DataFrame:
    df = pd.DataFrame(columns=BAR_COLUMNS, dtype="float64")
    df.index = pd.DatetimeIndex([], tz="Asia/Kolkata", name="ts")
    return df
