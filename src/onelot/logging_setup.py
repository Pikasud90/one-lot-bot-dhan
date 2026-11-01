"""Logging with mandatory secret redaction and rotation.

INVARIANT I6: an access token must never reach a log file, a console, or a
traceback. Tokens leak through exception dumps and request logging far more
often than through deliberate log statements, so redaction is applied as a
filter on the *formatted record* — it catches messages, args, and exception
text alike, no matter which library emitted them.

v1 defect E2/E4: no redaction, no rotation.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path

_REDACTION = "***REDACTED***"

# Token-shaped strings: long opaque runs, and JWTs (Dhan access tokens are JWTs).
_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT
    re.compile(
        r"(?i)\b(access[_-]?token|auth[_-]?token|api[_-]?key|bearer|password|secret)"
        r"\b\s*[:=]\s*[\"']?([A-Za-z0-9_\-\.]{8,})[\"']?"
    ),
]


class SecretRedactor(logging.Filter):
    """Scrubs known secret literals and token-shaped strings from every record."""

    def __init__(self, secrets: list[str] | None = None):
        super().__init__()
        self._literals: list[str] = []
        for s in secrets or []:
            self.add_secret(s)

    def add_secret(self, value: str) -> None:
        if value and len(value) >= 6 and value not in self._literals:
            self._literals.append(value)

    def scrub(self, text: str) -> str:
        for lit in self._literals:
            if lit in text:
                text = text.replace(lit, _REDACTION)
        for pat in _PATTERNS:
            if pat.groups >= 2:
                text = pat.sub(lambda m: f"{m.group(1)}={_REDACTION}", text)
            else:
                text = pat.sub(_REDACTION, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:  # pragma: no cover - malformed record
            rendered = str(record.msg)
        scrubbed = self.scrub(rendered)
        if scrubbed != rendered:
            record.msg = scrubbed
            record.args = ()
        if record.exc_info:
            # Materialise and scrub the traceback rather than let the formatter
            # render the raw exception text later.
            import traceback

            record.exc_text = self.scrub("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        return True


_redactor = SecretRedactor()


def get_redactor() -> SecretRedactor:
    return _redactor


def register_secrets(values: list[str]) -> None:
    for v in values:
        _redactor.add_secret(v)


_FMT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S%z"


def setup_logging(
    level: str = "INFO",
    log_dir: str | Path | None = None,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 10,
    secrets: list[str] | None = None,
    component: str = "engine",
) -> logging.Logger:
    """Idempotent root-logger setup. Safe to call more than once."""
    register_secrets(secrets or [])

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(_FMT, datefmt=_DATEFMT)

    console = logging.StreamHandler(stream=sys.stdout)
    console.setFormatter(fmt)
    console.addFilter(_redactor)
    root.addHandler(console)

    if log_dir:
        d = Path(log_dir)
        d.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            d / f"{component}.log",
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        fh.addFilter(_redactor)
        root.addHandler(fh)

    # Third-party noise floors
    for noisy in (
        "urllib3",
        "websockets",
        "httpx",
        "httpcore",
        "asyncio",
        "matplotlib",
        "yfinance",
        "peewee",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return logging.getLogger("onelot")


def get_logger(name: str) -> logging.Logger:
    """Namespaced logger (`onelot.<name>`) for the ported Lab/data modules."""
    if not name.startswith("onelot"):
        name = f"onelot.{name}"
    return logging.getLogger(name)
