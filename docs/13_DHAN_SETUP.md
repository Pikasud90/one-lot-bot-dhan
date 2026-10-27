# 13 — Dhan setup: tokens, tiers, postback, what the desk calls

## Tiers

| Tier | Endpoints the desk uses | Cost |
|---|---|---|
| **Trading API** (free with the account) | `GET /profile`, `GET /fundlimit`, `GET /holdings`, `GET /positions`, `GET /ledger?from-date&to-date`, `GET /trades/{from}/{to}/{page}`; the public scrip master CSV | ₹0 |
| **Data API** | `POST /optionchain/expirylist`, `POST /optionchain`, `POST /marketfeed/quote`, `POST /charts/historical`, `POST /charts/intraday`, `POST /charts/rollingoption` (later), the market-feed WebSocket | ₹499 + GST / month, 30-day renewal, bought on the account (web.dhan.co) |

The desk never calls an order, super-order, forever-order, EDIS or kill-switch endpoint.
`tests/test_invariants.py` fails if such an identifier appears anywhere in `src/`.

Error codes the adapter maps: `DH-901 / 807–810` → token expired; `DH-902 / 806` → Data API not
subscribed; `DH-904 / 805 / HTTP 429` → rate limited (retried through the shared limiter, then
raised). **A free-plan token gets HTTP 401 from every Data-API endpoint** with one of two
bodies — `{"data": {"806": "Data APIs not Subscribed"}, "status": "failed"}` or
`{"errorCode": "DH-902", "errorMessage": "This API requires a Data APIs Plan…"}` (captured
live 2026-10-27). The adapter reads the body before blaming the token, so this surfaces as
`NO_DATA_API`, not `TOKEN_EXPIRED`. Limits respected: option chain 1 request / 3 s, quotes 1 / s, non-trading reads 20 / s
(we use 2 / s), 100 000 data requests / day.

## Generating tokens

1. web.dhan.co → **My Profile → Access DhanHQ APIs**.
2. **Generate Access Token** — valid for **24 hours**. There is also an optional **Postback URL**
   field on that form: it is a webhook for *order updates* and is tied to the token. Since the
   desk never places orders, leave it blank. If the form insists, use
   `http://127.0.0.1:8787/api/dhan/postback` (or your LAN address); the desk records anything
   that arrives and never acts on it. A loopback URL is not reachable by Dhan, which is fine.
3. Paste the client ID and the token into **Settings → Dhan credentials** in the web app, or
   into `.env` (`DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN`). The app writes `.env` with mode 0600
   and registers the values with the log redactor; nothing is echoed back.
4. Press **Test entitlements**. Expect: trading tier all OK (verified against a live free-tier
   account on 2026-10-27: profile, funds, holdings, positions, 30-day ledger, trade book);
   `scrip_master` OK (public CSV); the remaining data checks `NO_DATA_API` until the plan is
   bought. If you see `TOKEN_EXPIRED` on the *trading* tier, the token really is bad.
5. After buying the Data API: generate a token (either slot — the entitlement is on the
   account), paste it as the **Data API token** if you want to keep it separate, test again,
   then restart without `--mock` for live data.

Daily renewal options: regenerate on the web (24 h), call `RenewToken` while the old token
is still valid, or enable TOTP under Trading APIs and use
`POST https://auth.dhan.co/app/generateAccessToken?dhanClientId=&pin=&totp=` from your own
script. The desk does not store your PIN or TOTP secret.

## Two tokens

`DHAN_ACCESS_TOKEN` is used for the free account reads. `DHAN_DATA_ACCESS_TOKEN`, when set, is
used for Data API calls and the WebSocket; otherwise the first token is used for both. There
is no technical difference between the tokens on Dhan's side once the plan is active.

## Security notes

* `.env` is gitignored. The old `.env` from the Bank Nifty project is not copied; create a new one.
* The engine binds 127.0.0.1 by default. `--lan` binds 0.0.0.0: set `ONELOT_API_TOKEN` in
  `.env` so the mutation token is stable, and remember anyone on the LAN can *read* the desk.
* Token-shaped strings (JWTs) are scrubbed from logs by pattern even if a new secret is never
  registered.

## Payload quirks seen on a real account

* `/ledger` comes back **newest first**, every number as a string (`"23101.69"`), dates as
  `Oct 27, 2026`. The Home tab sorts by voucher date and takes the running balance from the
  newest voucher, not the last row.
* `/holdings` carries `lastTradedPrice` (as of the fetch), so the Home tab marks holdings and
  shows P&L without the Data API.
* `/trades` can contain non-market rows (IPO allotment, corporate action) with
  `customSymbol: ""`, `securityId: "0"`, `tradedPrice: 0`; the trade book labels them instead
  of showing a blank symbol.
* `/positions` lists CNC sells from holdings as `positionType: SHORT` with `netQty < 0` for the
  day; they are not short positions in the F&O sense.

## Checking what works right now

```bash
.venv/bin/python -m onelot preflight
```

prints a two-tier report and `READY` / `NOT READY`. The same report is behind **Settings →
Test entitlements**.
