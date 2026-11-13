"""REVIEW O-11 (seen live on 2026-11-12): a token renewed in Settings builds a new account
adapter; the desk's bar source must read through it from the next fetch on (it kept the old
one, failed with HTTP 401 and fell back to delayed Yahoo bars). The bars held are kept."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from desk_harness import new_desk, small_cfg  # noqa: E402

from onelot.broker.base import Bar  # noqa: E402
from onelot.clock import IST  # noqa: E402
from onelot.desk.data import CompositeBars, DhanBars, YahooBars  # noqa: E402
from onelot.store import init_database  # noqa: E402


class Adapter:
    def __init__(self, name: str, expired: bool) -> None:
        self.name, self.expired, self.calls = name, expired, 0

    def historical_intraday(self, contract, interval_min, start, end):
        self.calls += 1
        if self.expired:
            raise RuntimeError("HTTP 401: token expired")
        t = dt.datetime.now(IST).replace(second=0, microsecond=0) - dt.timedelta(minutes=5)
        return [Bar(ts=t, open=100.0, high=101.0, low=99.0, close=100.5, volume=0.0)]


@pytest.fixture
def db(tmp_path):
    return init_database(f"sqlite:///{tmp_path / 'o11.db'}")


def test_a_renewed_adapter_reaches_the_bar_source_and_the_error_clears(cfg, db):
    old, new = Adapter("old", expired=True), Adapter("new", expired=False)
    current = {"adapter": old}
    desk, _, _ = new_desk(
        cfg, db, desk_cfg=small_cfg(source="auto"),
        adapter_getter=lambda: current["adapter"], data_api_active=lambda: True,
    )  # fmt: skip
    desk.source = CompositeBars(DhanBars(old, pause_s=0), YahooBars())
    specs = {k: t.series for k, t in desk.trackers.items() if k == "SBIN"}
    desk.source.primary.fetch(specs, 2)
    assert "401" in (desk.source.primary.last_error or "") and old.calls == 1
    held = desk.trackers["SBIN"].bars = desk.trackers["SBIN"].bars  # whatever is held stays
    current["adapter"] = new  # the token was renewed in Settings
    desk._maybe_switch_source()
    primary = desk.source.primary
    assert isinstance(primary, DhanBars) and primary.adapter is new
    got = primary.fetch(specs, 2)
    assert new.calls == 1 and old.calls == 1 and "SBIN" in got
    assert primary.last_error is None
    assert desk.trackers["SBIN"].bars is held  # the bars were not reset
