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


def test_the_build_changes_when_a_page_file_changes(tmp_path, monkeypatch) -> None:
    import shutil

    import onelot.api.server as server

    ui = tmp_path / "ui"
    shutil.copytree(server.UI_DIR, ui)
    monkeypatch.setattr(server, "UI_DIR", ui)
    before = server._ui_build()
    assert server._ui_build() == before  # the same files: the same id
    page = ui / "app.js"
    page.write_text(page.read_text(encoding="utf-8") + "\n// changed\n", encoding="utf-8")
    assert server._ui_build() != before
