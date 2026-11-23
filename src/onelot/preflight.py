"""Credential and entitlement preflight: `python -m onelot.preflight`.

Answers, before you ever depend on it in a live session, exactly which parts of
the Dhan API this account can actually reach. v1's failure mode was discovering
a missing Data API subscription as an obscure error mid-session.
"""

from __future__ import annotations

import json
import sys

from .config import AppConfig, Secrets
from .logging_setup import setup_logging
from .ratelimit import RateLimiter


def history_summary(cfg: AppConfig) -> tuple[int, int]:
    """(series, bars) held in the local market-history database; (0, 0) on a fresh clone."""
    from .data.db import get_engine
    from .data.store import coverage_report

    frame = coverage_report(get_engine(cfg.db_url_resolved))
    if frame.empty:
        return 0, 0
    return int(frame["series_key"].nunique()), int(frame["bars"].sum())


def main() -> int:
    cfg = AppConfig.load()
    secrets = Secrets.load(cfg.project_root)
    setup_logging(level="INFO", secrets=secrets.secret_values(), component="preflight")

    print("=" * 72)
    print("  OneLot preflight")
    print("=" * 72)

    problems = cfg.blocking_problems()
    print(f"\n[config]  capital: Rs {cfg.account.capital_inr:,.0f}   mode: {cfg.mode}")
    for p in problems:
        print(f"   BLOCKING: {p}")

    try:
        n_series, n_bars = history_summary(cfg)
    except Exception as exc:  # noqa: BLE001 - a broken database must not hide the rest
        n_series, n_bars = 0, 0
        print(f"\n[history]  could not read the market-history database ({type(exc).__name__})")
    else:
        if n_bars:
            print(f"\n[history]  {n_bars:,} bars in {n_series} series")
        else:
            print("\n[history]  EMPTY (a fresh install has none; it is not part of the download)")
    if not n_bars:
        print("   The Backtest Lab, Simulator and Machine Learning Lab (60m and 4h) need it.")
        print("   Live Desk and Home do not. Download it (free, from Yahoo; takes a while):")
        print("       python -m onelot backfill")

    if not secrets.has_broker_creds:
        print("\n[credentials]  NOT SET (optional)")
        print("   Without Dhan credentials the app still runs: on the synthetic mock market")
        print("   (python -m onelot run --mock) or on delayed Yahoo bars.")
        print("   For your account and the live Dhan feed, either paste your client id and")
        print("   access token in the Settings tab, or copy .env.example to .env and fill")
        print("   DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN.")
        print("\n[verdict]  nothing to check against Dhan yet; see the two notes above")
        return 1

    print(f"\n[credentials]  present (client-id ends ...{secrets.client_id[-4:]}, token redacted)")

    from .broker.dhan import DhanAdapter

    adapter = DhanAdapter(secrets.client_id, secrets.access_token, RateLimiter())
    result = adapter.preflight(
        cfg.instrument.index_security_id, cfg.instrument.exchange_segment_index
    )

    print("\n[endpoints]")
    for name, r in result["checks"].items():
        mark = "OK  " if r["ok"] else "FAIL"
        print(
            f"   {mark}  {name}"
            + ("" if r["ok"] else f"   -> {r.get('error')}: {r.get('detail', '')[:150]}")
        )

    if result["notes"]:
        print("\n[notes]")
        for n in dict.fromkeys(result["notes"]):
            print(f"   - {n}")

    print(
        "\n[verdict]  "
        + ("READY" if result["ok"] and not problems else "NOT READY — fix the items above")
    )
    print("\nFull result:\n" + json.dumps(result, indent=2, default=str))
    adapter.close()
    return 0 if (result["ok"] and not problems) else 1


if __name__ == "__main__":
    sys.exit(main())
