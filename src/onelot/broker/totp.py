"""Dhan access-token generation without the browser.

Dhan tokens live 24 hours. With TOTP enabled on the Dhan account, a new token can
be minted from the PIN and the authenticator secret:

    POST https://auth.dhan.co/app/generateAccessToken?dhanClientId=&pin=&totp=

The PIN and the TOTP secret are stored only in `.env` (mode 0600), never logged,
never echoed. The TOTP itself is RFC 6238 (SHA-1, 30-second step, 6 digits) and is
computed here so no extra dependency is needed.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import struct
import time

import httpx

from .base import BrokerError

AUTH_URL = "https://auth.dhan.co/app/generateAccessToken"
RENEW_URL = "https://api.dhan.co/v2/RenewToken"


def totp_now(secret_b32: str, digits: int = 6, period: int = 30, at: float | None = None) -> str:
    key = base64.b32decode(secret_b32.strip().replace(" ", "").upper() + "=" * (-len(secret_b32.strip()) % 8))
    counter = int((at if at is not None else time.time()) // period)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def generate_access_token(client_id: str, pin: str, totp_secret: str, timeout: float = 20.0) -> dict:
    """Mint a fresh 24-hour token. Returns {access_token, expiry_time, client_name}."""
    if not (client_id and pin and totp_secret):
        raise BrokerError("client id, PIN and TOTP secret are all required to generate a token")
    code = totp_now(totp_secret)
    try:
        with httpx.Client(timeout=timeout) as c:
            resp = c.post(AUTH_URL, params={"dhanClientId": client_id, "pin": pin, "totp": code})
    except httpx.HTTPError as exc:
        raise BrokerError(f"token generation: network error {type(exc).__name__}") from exc
    try:
        body = resp.json()
    except ValueError:
        body = {}
    token = body.get("accessToken") if isinstance(body, dict) else None
    if resp.status_code != 200 or not token:
        msg = ""
        if isinstance(body, dict):
            msg = str(body.get("errorMessage") or body.get("message") or body.get("error") or "")
        raise BrokerError(f"token generation failed (HTTP {resp.status_code}): {msg[:160] or 'no token in response'}. "
                          "Check that TOTP is enabled on web.dhan.co and the PIN / secret are right.")
    return {"access_token": token, "expiry_time": body.get("expiryTime"),
            "client_name": body.get("dhanClientName"), "generated_at": dt.datetime.now().isoformat()}


def renew_token(client_id: str, access_token: str, timeout: float = 20.0) -> dict:
    """Swap a still-valid web-generated token for a fresh 24-hour one."""
    try:
        with httpx.Client(timeout=timeout, headers={"access-token": access_token, "dhanClientId": client_id,
                                                    "client-id": client_id}) as c:
            resp = c.get(RENEW_URL)
    except httpx.HTTPError as exc:
        raise BrokerError(f"token renewal: network error {type(exc).__name__}") from exc
    try:
        body = resp.json()
    except ValueError:
        body = {}
    token = body.get("accessToken") if isinstance(body, dict) else None
    if resp.status_code != 200 or not token:
        msg = str(body.get("errorMessage") or body.get("message") or "") if isinstance(body, dict) else ""
        raise BrokerError(f"token renewal failed (HTTP {resp.status_code}): {msg[:160] or 'no token in response'}")
    return {"access_token": token, "expiry_time": body.get("expiryTime")}
