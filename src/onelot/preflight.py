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

    if not secrets.has_broker_creds:
        print("\n[credentials]  NOT SET")
        print("   Fill DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN in .env.")
        print("   Until then the engine runs on the SYNTHETIC mock market:")
        print("       python -m onelot --mock")
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
