"""Characterisation: how the Lab and the desk read a bad INDIA VIX close (US-023 review round 1).

Manager decision (a), 2026-11-02: US-023 makes only the alert economics (the paper pricing path)
fall back to the constant IV on a NaN, infinite or non-positive VIX. The Lab's `OptionPricer`
and the desk's own `Desk._premium_at` (manual-position marks, the audit) keep reading the close
as it is; whether they should use the constant or the last valid VIX is US-031's decision. These
tests pin that behaviour exactly as it was before 4ae62a4, so a change to it is deliberate.
"""

from __future__ import annotations

import datetime as dt
import math
from types import SimpleNamespace

import pandas as pd
import pytest

from onelot.clock import IST
from onelot.config import BacktestCfg
from onelot.desk.desk import Desk
from onelot.lab.execution import OptionPricer, expiry_datetime

BT = BacktestCfg()
TS = pd.Timestamp(dt.datetime(2026, 10, 29, 10, 5, 3, tzinfo=IST))
EXPIRY = expiry_datetime(dt.date(2026, 11, 24))
OPTION = {"strike": 54000.0, "opt_type": "CE", "expiry": "2026-11-24"}
NAN, INF = float("nan"), float("inf")


def lab_pricer(vix: float | None) -> OptionPricer:
    return OptionPricer(BT, None if vix is None else pd.Series([vix], index=[TS.normalize()]))


def lab_premium(vix: float | None) -> float:
    return lab_pricer(vix).premium(54000.0, 54000.0, TS, EXPIRY, "CE")


def desk_premium(vix: float | None) -> float | None:
    """Desk._premium_at on a stand-in desk whose last INDIA VIX close is `vix`."""
    desk = SimpleNamespace(_context_last=lambda key: vix, app_cfg=SimpleNamespace(backtest=BT))
    return Desk._premium_at(desk, OPTION, 54000.0, TS.to_pydatetime())  # type: ignore[arg-type]


@pytest.mark.parametrize("vix", [NAN, INF], ids=["nan", "inf"])
def test_the_lab_passes_a_non_finite_vix_close_through(vix):
    assert not math.isfinite(lab_pricer(vix).iv_for(TS))
    assert math.isnan(lab_premium(vix))


@pytest.mark.parametrize("vix", [-3.0, 0.0], ids=["-3", "zero"])
def test_the_lab_prices_a_non_positive_vix_close_at_zero(vix):
    assert lab_pricer(vix).iv_for(TS) == vix / 100.0 * BT.option_iv_mult
    assert lab_premium(vix) == 0.0


def test_the_lab_uses_the_constant_iv_only_without_a_vix_series():
    assert lab_pricer(None).iv_for(TS) == BT.option_iv_constant
    assert math.isfinite(lab_premium(None)) and lab_premium(None) > 0


@pytest.mark.parametrize("vix", [NAN, INF], ids=["nan", "inf"])
def test_the_desks_own_premium_is_not_a_number_for_a_non_finite_vix(vix):
    """NaN is truthy, so the desk builds a VIX series from it: its premium is NaN (US-025)."""
    premium = desk_premium(vix)
    assert premium is not None and math.isnan(premium)


def test_the_desks_own_premium_is_zero_for_a_negative_vix():
    assert desk_premium(-3.0) == 0.0


@pytest.mark.parametrize("vix", [None, 0.0], ids=["none", "zero"])
def test_the_desk_uses_the_constant_iv_without_a_vix_or_for_a_zero_one(vix):
    assert desk_premium(vix) == lab_premium(None)
