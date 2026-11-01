"""US-028 AC1 (the rule): one pure function decides whether bars are "delayed".

Public contract (src/onelot/desk/data.py)::

    def is_delayed_source(name: str | None) -> bool
        # True iff `name` starts with "yahoo" (case-insensitive).
        # "dhan", "dhan-ws", "mock", None and "" are all False.

Alerts (`data.delayed`), tracker health, the Desk header badge and the Telegram tag all
derive their label from this one function, so its table is pinned here on its own, with no
desk and no network. The functions are reached through the module (not a `from` import) so
that, until the story is implemented, every test fails on its own with a clear
AttributeError instead of the whole file failing to import.
"""

from __future__ import annotations

import json
import string

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from onelot.desk import data as desk_data


def rule(name):
    return desk_data.is_delayed_source(name)


class TestTable:
    @pytest.mark.parametrize(
        "name",
        ["yahoo", "Yahoo", "YAHOO", "yAhOo", "yahoo-2m", "yahoo+extra", "Yahoo Finance"],
    )
    def test_yahoo_served_bars_are_delayed_in_any_letter_case(self, name):
        assert rule(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "dhan",
            "DHAN",
            "dhan-ws",
            "mock",
            "fake",
            "manual",
            "dhan+yahoo",  # the composite's own name is not a provider: never "delayed" itself
            "xyahoo",  # contains yahoo but does not start with it
            "yaho",  # a strict prefix of "yahoo"
            "yah",
            "",
            None,
        ],
    )
    def test_every_other_source_is_not_delayed(self, name):
        assert rule(name) is False

    @pytest.mark.parametrize("name", ["yahoo", "dhan", "dhan-ws", "mock", "", None])
    def test_the_answer_is_a_real_bool_not_a_truthy_value(self, name):
        # the flag is serialised straight into the snapshot JSON and the alert payload
        assert type(rule(name)) is bool
        assert json.dumps(rule(name)) in ("true", "false")

    def test_the_rule_is_pure_it_gives_the_same_answer_every_time(self):
        assert [rule("yahoo") for _ in range(3)] == [True, True, True]
        assert [rule("dhan") for _ in range(3)] == [False, False, False]


class TestAgreesWithTheRealSources:
    """The rule must classify the names the real bar sources actually carry."""

    @pytest.mark.parametrize("cls_name", ["YahooBars", "DhanBars", "DhanLiveBars"])
    def test_the_rule_matches_each_source_classs_own_delayed_flag(self, cls_name):
        cls = getattr(desk_data, cls_name)
        assert rule(cls.name) is cls.delayed, (
            f"{cls_name}.name={cls.name!r} classifies as {rule(cls.name)} "
            f"but the class says delayed={cls.delayed}"
        )

    def test_a_yahoo_only_composite_is_named_so_that_it_reads_as_delayed(self):
        # With no Dhan primary the composite's own name is the fallback when `served_by`
        # has no entry for a series, so a Yahoo-only desk must still read as delayed.
        composite = desk_data.CompositeBars(None, desk_data.YahooBars())
        assert rule(composite.name) is True

    def test_a_dhan_yahoo_composite_name_is_not_itself_delayed(self):
        class DhanLike:
            name, delayed, resolution, last_error = "dhan", False, "1m", None

        composite = desk_data.CompositeBars(DhanLike(), desk_data.YahooBars())
        assert composite.name == "dhan+yahoo"
        assert rule(composite.name) is False


class TestProperties:
    @settings(max_examples=150, deadline=None)
    @given(
        flips=st.lists(st.booleans(), min_size=5, max_size=5),
        suffix=st.text(alphabet=string.printable, max_size=24),
    )
    def test_any_casing_of_the_yahoo_prefix_with_any_suffix_is_delayed(self, flips, suffix):
        prefix = "".join(c.upper() if up else c for c, up in zip("yahoo", flips, strict=True))
        assert rule(prefix + suffix) is True

    @settings(max_examples=150, deadline=None)
    @given(
        name=st.text(alphabet=string.ascii_letters + string.digits + "+-_ ", max_size=24).filter(
            lambda s: not s.lower().startswith("yahoo")
        )
    )
    def test_a_name_that_does_not_start_with_yahoo_is_never_delayed(self, name):
        assert rule(name) is False
