# ADR-002: Telegram alerts via a Notifier channel over the raw Bot API (outbound only)
Status: proposed (confirm at Checkpoint 1)

Context: Alerts must reach the owner's phone with a compact, actionable message. The Mac is behind home NAT (no inbound webhook). The Notifier already fans out to isolated channels
(`Channel` protocol: `name`, `available`, `send(Notification)->bool`) and logs a warning that `notify.telegram` is "not implemented".

Options:
A — python-telegram-bot / aiogram. Pros: batteries. Cons: heavy async dependency in a threaded sync app; more surface for token leakage via library logging.
B — Raw Bot API with `httpx` (already a dependency): `sendMessage`, `editMessageText`, `getUpdates` long-poll. Pros: tiny, testable with `httpx.MockTransport`, no inbound port.
C — Webhook. Cons: needs a public HTTPS endpoint/tunnel; rejected for a home Mac.

Decision: **B**, as `onelot.notify.telegram`:
- `TelegramChannel` implements `Channel`; wired where `notify.telegram` is true and credentials exist (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` in `.env`; also settable in Settings, 0600, never echoed).
- Message = HTML parse mode with strict escaping; ≤ 12 lines; fields: action (BUY/SELL, CE/PE), instrument, unit, entry (market/est. premium), SL, TGT, strategy+timeframe, confidence, TTL as an absolute IST deadline plus remaining, data source (live/delayed), IST timestamp, short alert id. No account data, balances or holdings ever leave the machine.
- Outbox table (`telegram_outbox`): every message persisted before sending; retry with exponential backoff honouring `retry_after`; ≤ 1 msg/s per chat; delivery status (queued/sent/failed) visible in the app; replay after outage drops messages whose TTL has expired (sends a short "missed" note instead).
- Security: only `TELEGRAM_CHAT_ID` is trusted (inbound updates from any other chat are ignored and logged); the token is registered with the log redactor *including URLs and exception text*; `/bot<token>/` never appears in logs or the UI.
- Phase 2 (US-013): `getUpdates` long-poll for `/status /positions /paper /mute /unmute` and inline Taken/Skip buttons that call the same `Desk.acknowledge` path; buttons never carry a price entry (fill = alert price unless the owner replies in the app).
- Naming: avoid order-verb identifiers.

Consequences: a new secret to protect; a new failure domain isolated behind the channel (failures never block other channels); phone delivery adds a measurable latency SLO (p95 ≤ 10 s) that becomes a metric (US-007).

## Amendments after refinement (2026-11-01) — decided by the Manager as tech lead
1. **Delivery is AT-LEAST-ONCE.** A duplicate phone message in the rare crash window between HTTP success and the DB update is acceptable; a lost alert is not (product metric: 0 silently dropped). Outbox statuses: `queued | sending | sent | failed | skipped`. Rows found in `sending` at restart are re-queued once and their text is prefixed "↻ resend". Dedup stays one row per `(alert_id, kind)`.
2. **What reaches the phone.** SIGNAL, EXIT and CRITICAL notifications only. INFO, WARNING and the self-test stay in-app. Implemented by an optional `Channel.accepts(n)`; `Notification` gains `kind` and `dedup_key` (set by `Notifier.send`).
3. **EXIT reminders on the phone.** First EXIT message immediately; reminders every 5 minutes, at most 3, while the position is open and the advice persists (the desk re-evaluates every poll, the phone must not get a message per minute).
4. **SLO restated.** p95 alert→sent ≤ 10 s when ≤ 8 messages are queued; the queue is ordered by confidence (high first) and paced at 1 message/s per chat; when more than 8 are queued, the top 8 go individually and the rest as one digest. The message is formatted at send time so "time left" is never stale.
5. **Notifier changes.** `add_channel` / `replace_channel` under the Notifier lock (stats entry created with the channel); `status()` merges each channel's own `status()`; channel stats must not count "enqueued" as "sent".
6. **Threading rules.** `TelegramChannel.send` is ONE short INSERT — no HTTP, no Notifier call-back, never under a long transaction. The worker never takes the desk lock, never calls the Notifier, claims a row with `UPDATE ... WHERE status='queued'`, commits, THEN makes the HTTP call, then updates. `Engine.stop()` stops and joins the worker before `db.dispose()`.
7. **Secrets and routes.** Token shape `^\d{6,12}:[A-Za-z0-9_-]{30,}$`, chat id `^-?[1-9][0-9]{0,18}$`; newlines rejected (422) because `write_env` writes raw `K=v`; `write_env` becomes atomic (temp file, 0600, `os.replace`). The Telegram token is never shown, not even the last 4 characters. Secret-writing routes (`/api/settings/dhan`, `/api/settings/telegram`) accept only loopback clients until US-005 lands real authentication (today `GET /api/session` and the page itself hand the mutation token to any LAN client).
8. **Redaction hardening.** Register three forms of the token (full, the part after the colon, the `%3A` form); put the redactor on the `httpx` logger itself and keep `httpcore` at WARNING+; `SendResult.detail` is built only from the HTTP status and Telegram's `description`, never from `str(exc)`; the token lives in a private attribute with a masked `repr`; do NOT register the chat id (it would corrupt numbers in logs).
9. **Quiet hours** come from the session clock and holiday calendar (a holiday is quiet), evaluated at send time. CRITICAL is never silent.
