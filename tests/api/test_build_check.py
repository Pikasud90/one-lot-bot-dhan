"""US-122 A6: the desk's build id on GET /api/health and in the served page."""

from __future__ import annotations

import re

from .conftest import client, engine  # noqa: F401 - fixtures, found by name


def test_health_carries_a_stable_build(client) -> None:  # noqa: F811
    first = client.get("/api/health").json()["build"]
    second = client.get("/api/health").json()["build"]
    assert isinstance(first, str) and len(first) >= 8
    assert first == second


def test_the_served_page_has_the_same_build(client) -> None:  # noqa: F811
    build = client.get("/api/health").json()["build"]
    page = client.get("/").text
    assert f'window.ONELOT_BUILD = "{build}";' in page
    assert "__BUILD__" not in page
    assert re.search(r'id="build-bar"', page)
