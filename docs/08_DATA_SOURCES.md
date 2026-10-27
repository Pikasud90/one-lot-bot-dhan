# Data Sources: Dhan, and the free failsafe

## Where Dhan fits, and where we currently stand

Dhan is the **only proprietary input** in this system and the **only source of
option data**. Everything that decides a trade — the option chain, greeks,
bid/ask, open interest, live option quotes — comes from Dhan or does not come
at all.

**Current status, stated honestly:** the adapter (`src/onelot/broker/dhan.py`) is
written against Dhan's documented v2 HTTP API and is **not yet verified against
a live account**, because no credentials exist in this repository. Endpoint
paths and payload shapes follow the published contract, and response parsing is
deliberately tolerant of key-name variation, but *unverified means unverified*.

`python -m onelot.preflight` is what closes that gap. Run it first.

### Why direct HTTP rather than the SDK

Request/response calls go through `httpx` against the documented endpoints, not
through the `dhanhq` SDK. The SDK's method signatures changed materially between
1.x and 2.x — renamed kwargs, a new context object, reshuffled envelopes. Being
pinned to those means a routine `pip install -U` can break a live session. The
HTTP contract is far more stable than the Python wrapper.

The SDK is used for one thing only: the **WebSocket market feed**, where
reimplementing a binary protocol would be a genuine liability. If it is missing
or its API moved, the adapter degrades to REST polling and reports `DEGRADED`
rather than dying.

### What you need

1. A **Dhan account** with API access enabled (Profile → DhanHQ APIs).
2. A **Data API subscription**. This is a **paid add-on, separate from the free
   trading API**, and it is what unlocks the live feed and the option chain.
   Without it, `preflight` reports `NO_DATA_API` and names the endpoint.
3. `client-id` and `access-token` in `.env`.

```bash
cp .env.example .env      # then fill in DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN
python -m onelot.preflight
```

Preflight exercises the scrip master, the expiry list and the option chain, and
tells you exactly which succeeded. It distinguishes three failures that look
identical from inside a running engine:

| Result | Meaning |
|---|---|
| `TOKEN_EXPIRED` | HTTP 401/403 — regenerate the token |
| `NO_DATA_API` | authenticated, but not entitled to market data |
| anything else | network, endpoint or parsing problem, with detail |

### Token expiry

Dhan access tokens expire. The engine detects a 401, transitions to `DEGRADED`,
fires a distinct alert, and shows a token-age countdown. **It cannot renew the
token for you** — that is a manual step, and the system is designed to tell you
loudly rather than silently stop working.

### Rate limits

The option chain endpoint permits **1 request per 3 seconds**. Every broker call
in this codebase passes through one shared token-bucket limiter with
per-endpoint buckets and a global ceiling, so the 5-second poller, the REST
failover and any history fetch cannot collectively breach it — which is exactly
what happens without a central limiter, at the precise moment the socket drops
and failover starts competing with the poller.

---

## The free failsafe: Yahoo Finance

`data.fallback_source: yfinance` (default). No credentials, no account.

### What it can do

| Capability | Provided |
|---|---|
| Daily index history (`^NSEBANK`) | yes — regime training, without broker quota |
| 5-minute index bars | yes — indicator warm-up, ~60 days available |
| Current index level | yes, delayed — for cross-checking |
| **Option chain** | **no** |
| **Greeks** | **no** |
| **Option bid/ask, OI, volume** | **no** |
| **Live tick feed** | **no** |

### So what is it actually for

Three concrete jobs, none of which is "replace the broker":

**1. Free history.** Warm-up needs ~40 closed 5-minute bars before EMA/RSI/ADX
mean anything, and the regime engine wants a long daily series. Pulling those
from Yahoo costs no broker quota — quota you may want later in the session.

**2. An independent opinion on the index level.** This is the one that earns its
place. A feed that is **stuck** — still connected, still returning a number, but
the *same* number — passes every staleness check ever written, because data is
arriving. Only a second, independent source exposes it. The engine compares the
two every 5 minutes and raises a `CRITICAL` alert past
`fallback_divergence_pct` (0.5%).

**3. Running with no broker subscription at all.** Warm-up, the regime engine
and the entire backtest work without credentials.

### What it explicitly cannot do

**If Dhan's option chain dies, the system stops signalling.** Yahoo cannot price
an option, cannot supply a delta, cannot tell you the bid. The engine goes
`DEGRADED`, suppresses new signals, and keeps managing any open position on the
last known option quote. No fallback changes that, and pretending otherwise
would be worse than having none.

### Why polling is deliberately rare

Default 300 seconds, with a hard floor of 60 enforced by config validation.
This is a safety net, not a feed. Hammering a free public endpoint gets it
blocked and buys nothing — the broker is the low-latency path, and a 5-minute
cross-check catches a stuck feed perfectly well.

### Why not Fyers, or the NSE endpoint

- **Fyers** needs its own account, app registration and OAuth flow. That is a
  second broker integration, not a free failsafe — it does not reduce setup
  burden, which is the entire point here.
- **NSE's public option-chain endpoint** is unauthenticated but requires cookie
  priming, blocks automated clients aggressively, and its terms do not
  contemplate programmatic use. Depending on it for trading decisions would be
  building on something designed to stop you.

If you already have a Fyers account and want it as a **second option-data
source**, that is a real improvement over Yahoo and worth doing — implement
`BrokerAdapter` in `src/onelot/broker/`. The protocol is read-only by
construction, so a new adapter cannot introduce an order path.

---

## Summary

```
                 OPTION DATA                    INDEX DATA
                 (decides trades)               (context + cross-check)

  Dhan           chain, greeks, bid/ask,        spot, futures, history
  (paid)         OI, live ticks                 live ticks

  Yahoo          —  nothing  —                  daily bars, 5m bars,
  (free)                                        delayed spot

  If Dhan option data is unavailable:  NO SIGNALS. By design.
  If Dhan index data is unavailable:   warm-up and history still work.
  If the two index sources disagree:   CRITICAL alert — one feed is wrong.
```
