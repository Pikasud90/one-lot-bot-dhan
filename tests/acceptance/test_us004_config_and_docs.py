"""US-004 NFRs: configuration (Strict, model before YAML), the stale warning, and the docs.

* NotifyCfg gains telegram_levels (default SIGNAL, EXIT, CRITICAL) and
  telegram_quiet_outside_session (default true); the model is Strict, so a key in the YAML
  that the model does not know would stop the app from starting;
* the "notify.telegram is not implemented" warning is gone (webhook keeps its own);
* TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are documented in .env.example and the README,
  without a real-looking token anywhere.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from pydantic import ValidationError
from us004_helpers import REPO, bare_notifier, level_names

from onelot.config import AppConfig, NotifyCfg

TOKEN_SHAPE = re.compile(r"[0-9]{6,12}:[A-Za-z0-9_-]{30,}")


# ========================= NotifyCfg ====================================================
def test_notify_cfg_defaults_are_signal_exit_critical_and_quiet_outside_session():
    cfg = NotifyCfg()
    assert level_names(cfg.telegram_levels) == ["SIGNAL", "EXIT", "CRITICAL"]
    assert cfg.telegram_quiet_outside_session is True


def test_notify_cfg_keeps_its_existing_fields_and_defaults():
    cfg = NotifyCfg()
    assert (cfg.os_toast, cfg.dashboard, cfg.webhook) == (True, True, False)


@pytest.mark.parametrize(
    "levels", [["SIGNAL"], ["EXIT", "CRITICAL"], ["SIGNAL", "EXIT", "CRITICAL"]]
)
def test_notify_cfg_accepts_any_subset_of_the_phone_levels(levels):
    assert level_names(NotifyCfg(telegram_levels=levels).telegram_levels) == levels


def test_notify_cfg_rejects_a_misspelled_level_instead_of_ignoring_it():
    """PROPOSED (config.py: 'A misspelled option must not be silently ignored'): a typo like
    SIGNL would otherwise mean the owner silently never gets signals."""
    assert level_names(NotifyCfg(telegram_levels=["SIGNAL"]).telegram_levels) == ["SIGNAL"]
    with pytest.raises(ValidationError):
        NotifyCfg(telegram_levels=["SIGNL"])


def test_notify_cfg_is_still_strict_about_unknown_keys():
    assert NotifyCfg(telegram_quiet_outside_session=False).telegram_quiet_outside_session is False
    with pytest.raises(ValidationError):
        NotifyCfg(telegram_leveles=["SIGNAL"])


def test_the_shipped_config_yaml_loads_and_exposes_the_new_fields():
    cfg = AppConfig.load(REPO / "config" / "config.yaml", project_root=REPO)
    assert cfg.notify.telegram is True, "notify.telegram is the master switch and ships on"
    assert level_names(cfg.notify.telegram_levels) == ["SIGNAL", "EXIT", "CRITICAL"]
    assert cfg.notify.telegram_quiet_outside_session is True


def test_the_shipped_yaml_no_longer_calls_telegram_unimplemented():
    line = next(
        ln
        for ln in (REPO / "config" / "config.yaml").read_text().splitlines()
        if ln.strip().startswith("telegram:")
    )
    assert "not implemented" not in line.lower(), line


def test_the_yaml_can_set_the_new_keys(tmp_path: Path):
    """The model knows both keys, so a YAML that sets them loads (model first, then YAML)."""
    text = (REPO / "config" / "config.yaml").read_text()
    patched = re.sub(
        r"(?m)^notify:\n",
        "notify:\n  telegram_levels: [SIGNAL]\n  telegram_quiet_outside_session: false\n",
        text,
        count=1,
    )
    assert patched != text, "config.yaml has no notify: block"
    path = tmp_path / "config.yaml"
    path.write_text(patched)
    cfg = AppConfig.load(path, project_root=REPO)
    assert level_names(cfg.notify.telegram_levels) == ["SIGNAL"]
    assert cfg.notify.telegram_quiet_outside_session is False


# ========================= the stale warning ============================================
def test_enabling_notify_telegram_no_longer_logs_a_not_implemented_warning(
    caplog: pytest.LogCaptureFixture,
):
    with caplog.at_level(logging.DEBUG):
        bare_notifier(telegram=True)
    stale = [
        r.getMessage()
        for r in caplog.records
        if "telegram" in r.getMessage().lower() and "not implemented" in r.getMessage().lower()
    ]
    assert stale == [], f"the stale warning is still logged: {stale}"


def test_the_webhook_warning_is_unchanged(caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.WARNING):
        bare_notifier(webhook=True)
    assert any(
        "webhook" in r.getMessage().lower() and "not implemented" in r.getMessage().lower()
        for r in caplog.records
    )


# ========================= documentation ================================================
def test_env_example_documents_both_telegram_variables_with_empty_values():
    text = (REPO / ".env.example").read_text()
    assert re.search(r"(?m)^TELEGRAM_BOT_TOKEN=\s*$", text), "TELEGRAM_BOT_TOKEN= is missing"
    assert re.search(r"(?m)^TELEGRAM_CHAT_ID=\s*$", text), "TELEGRAM_CHAT_ID= is missing"
    assert TOKEN_SHAPE.search(text) is None, "a token-shaped value is committed in .env.example"


def test_readme_documents_both_telegram_variables():
    text = (REPO / "README.md").read_text()
    assert "TELEGRAM_BOT_TOKEN" in text and "TELEGRAM_CHAT_ID" in text
    assert TOKEN_SHAPE.search(text) is None, "a token-shaped value is committed in the README"
