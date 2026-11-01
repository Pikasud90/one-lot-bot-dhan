"""Hermetic notify tests (US-003 AC6): no test in this package may reach the network.

An autouse guard replaces DNS lookups, socket.create_connection and TCP/UDP connects with
a stub that records the attempt and raises. The record is checked when each test ends, so
code that swallows the guard's exception (a client turning it into a "transient" result)
still fails the test. A test that provokes an attempt on purpose requests
`network_attempts` and clears it. Local AF_UNIX sockets (socketpair) keep working.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from typing import Any

import pytest


class NetworkBlocked(RuntimeError):
    """Raised by the guard: a test in tests/notify tried to reach the network."""


def _install_guard(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    attempts: list[str] = []

    def blocked(what: str) -> Callable[..., Any]:
        def guard(*args: Any, **kwargs: Any) -> Any:
            attempts.append(what)
            raise NetworkBlocked(f"tests/notify must not use the network ({what})")

        return guard

    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "create_connection"):
        monkeypatch.setattr(socket, name, blocked(f"socket.{name}"))
    for method in ("connect", "connect_ex"):
        original = getattr(socket.socket, method)

        def connect(
            self: socket.socket,
            address: Any,
            *,
            _original: Callable[..., Any] = original,
            _what: str = f"socket.socket.{method}",
        ) -> Any:
            if self.family == getattr(socket, "AF_UNIX", object()):
                return _original(self, address)  # local IPC is not the network
            attempts.append(_what)
            raise NetworkBlocked(f"tests/notify must not use the network ({_what})")

        monkeypatch.setattr(socket.socket, method, connect)
    return attempts


@pytest.fixture(autouse=True)
def network_attempts(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """The attempts the guard blocked during this test; must be empty when it ends."""
    attempts = _install_guard(monkeypatch)
    yield attempts
    assert attempts == [], f"a test in tests/notify reached for the network: {attempts}"
