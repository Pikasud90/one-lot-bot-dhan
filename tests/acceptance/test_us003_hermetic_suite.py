"""US-003 AC6 (first half): no real network call is ever made while the suite runs.

The guard in us003_helpers (installed by an autouse fixture in every US-003 test module) makes
any socket connect or DNS lookup raise and records the attempt; each test fails afterwards if
the list is not empty, so a client that swallows the guard's exception cannot hide a call.
These tests prove the guard works and that building the objects under test needs no network.
"""

from __future__ import annotations

import socket
from typing import Any

import httpx
import pytest
from us003_helpers import (
    CHAT,
    NOW,
    TOKEN,
    NetworkBlocked,
    install_network_guard,
    stock_buy,
    telegram,
)


@pytest.fixture(autouse=True)
def _network_guard(monkeypatch: pytest.MonkeyPatch) -> Any:
    attempts = install_network_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"unexpected network attempt: {attempts}"


def test_the_guard_blocks_name_resolution_and_records_the_attempt(_network_guard):
    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo("api.telegram.org", 443)
    assert _network_guard == ["socket.getaddrinfo"]
    _network_guard.clear()


def test_the_guard_blocks_create_connection_and_raw_sockets(_network_guard):
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("api.telegram.org", 443), timeout=0.1)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as raw, pytest.raises(NetworkBlocked):
        raw.connect(("203.0.113.7", 443))  # TEST-NET-3, never routable
    assert len(_network_guard) == 2
    _network_guard.clear()


def test_the_guard_stops_an_httpx_client_that_has_no_mock_transport(_network_guard):
    with httpx.Client(timeout=1.0) as real, pytest.raises(Exception):  # noqa: B017
        real.get("https://api.telegram.org/")
    assert _network_guard, "the attempt was not recorded"
    _network_guard.clear()


def test_the_guard_leaves_local_unix_sockets_alone():
    a, b = socket.socketpair()
    try:
        a.sendall(b"ok")
        assert b.recv(2) == b"ok"
    finally:
        a.close()
        b.close()


def test_building_a_client_and_formatting_need_no_network():
    tg = telegram()
    client = tg.TelegramClient(TOKEN, CHAT)
    assert client.available is True
    assert tg.format_alert(stock_buy(), NOW)
    assert tg.format_missed(stock_buy(), NOW)
    assert not client.unavailable_reason


def test_an_unavailable_client_never_reaches_for_the_network():
    client = telegram().TelegramClient("bad", "bad")  # no http injected on purpose
    assert client.send_message("hello").kind == "unavailable"
