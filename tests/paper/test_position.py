"""The book's side of manage_step: exit rules read from the alert's `manage` block (US-002).

Review round 1, MAJOR-1: the trail uses the strategy's TRAIL ATR, ATR(trail_atr_period),
which the Desk freezes in the alert as `manage.atr_trail`; `manage.atr` (the ATR(14) the
stop is sized on) is only the fallback for an alert that predates `atr_trail`.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from onelot.paper.position import rules_from_alert


def rules(manage: dict[str, Any]):
    return rules_from_alert({"manage": manage}, tick_paise=5, resolution=1, timeframe="15m")


class TestTheTrailAtr:
    def test_the_strategys_trail_atr_is_the_one_used(self) -> None:
        assert rules({"atr": 4.0, "atr_trail": 2.5, "trail_atr_mult": 1.5}).atr == 2.5

    def test_an_alert_from_before_atr_trail_falls_back_to_its_atr(self) -> None:
        assert rules({"atr": 4.0, "trail_atr_mult": 1.5}).atr == 4.0

    @pytest.mark.parametrize("value", [None, 0.0, math.nan], ids=["none", "zero", "nan"])
    def test_an_unusable_trail_atr_turns_the_trail_off_with_no_fallback(self, value) -> None:
        # a warming-up ATR(5) is not replaced by the ATR(14): no fallback to another ATR
        assert rules({"atr": 4.0, "atr_trail": value, "trail_atr_mult": 1.5}).atr is None
