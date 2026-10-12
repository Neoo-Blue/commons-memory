# Agent messaging, wake and presence (v0.7)

Commons replaces the external agent bus. Every agent gets a durable mailbox, live streams, wake channels and a presence state.

## Messages
- Direct (`to: "muse"`) or broadcast (`to: "*"`); types `message | task | question | review | fyi`; threads (`thread`, `reply_to`).
- At-least-once: `recv` returns unacked messages; each delivery hides a message for `lease` seconds (0 = visible until acked,
  bus semantics); `ack` removes it. Long-poll with `wait` (≤ 60 s). Everything is stored, redacted and linked in the graph.
- MCP: `msg_send`, `msg_recv`, `msg_ack`, `msg_history`, `presence`, `wake_channels`.
- HTTP (agent key as `Authorization: Bearer` or `X-Bridge-Token`):
  - Bus-compatible shim: `POST /bus/send {from,to,text}`, `GET /bus/recv?for=NAME`, `POST /bus/ack {for,ids}`,
    `GET /bus/history?for=NAME`, `GET /bus/health`. Existing agent-bus clients only change the base URL and token.
    `from`/`for` must be the caller's own name (403 otherwise).
  - Native: `/agent/send|recv|ack|history`, `POST /agent/heartbeat`, `GET /agent/presence`,
    `GET /agent/stream` (SSE wake events), `WS /agent/ws` (ops: `send`, `recv`, `ack`, `heartbeat`).
- CLI: `commons msg send|recv|ack|history`.

## Wake channels
Configured per agent at connect time or later:
```
commons connect muse --platform muse --wake webhook:url=https://muse.example/hooks/commons
commons wake add muse ntfy topic=muse-wake
commons wake add claude command cmd="claude -p"        # local owner only
commons wake list | test AGENT | dead | redrive [ID]
```
Kinds: `webhook` (HMAC-SHA256 `X-Commons-Signature: sha256=hex(HMAC(secret, ts + "." + body))`, `X-Commons-Timestamp`,
`X-Commons-Delivery`; retries with exponential backoff, then dead-letter + notification), `ntfy`, `pushover`, `slack`,
`discord`, `telegram`, `email` (SMTP), `command` (spawns a local CLI with the payload on stdin), plus live SSE/WS streams
and MCP notifications to connected sessions.
Triggers: new message/task in inbox, handoff, review requested, escalation, schedule due.
Payloads carry only ids, event type and a redacted 140-char preview; agents fetch content with their own key.
Verify on the receiver with `commons_memory.messaging.ReplayGuard(secret).verify(headers, body)` (signature, ±5 min
timestamp window, delivery-id replay cache).

Security: outbound URLs must be https and resolve to public addresses unless the host is in
`COMMONS_WEBHOOK_ALLOWLIST` (comma list, `*.example.com` allowed); redirects are not followed; secrets in channel configs
must be references (`env://`, `vault://`, `op://`, `infisical://`); command hooks can only be added from the local CLI;
wakes are rate-limited per agent (`COMMONS_WAKE_PER_MIN`, excess coalesced). The same allowlist enables A2A push
notifications (`tasks/pushNotificationConfig/set`).

## Presence
States: `online` (heartbeat < 2 min or live stream), `idle`, `sleeping` (agent said so), `offline`. Any authenticated call
counts as a heartbeat. Before work is allocated, the orchestrator prefers ready agents and wakes offline ones through
their channels (`ensure_ready`); if nobody comes online the escalation ladder takes over. Dashboard → **Presence**.

## Always-on agent daemon
```
commons agent daemon --agent claude --cmd "claude -p" --url https://commons.tailnet:8770 --api-key $KEY
commons agent daemon --agent claude --cmd "claude -p" --print-unit systemd > ~/.config/systemd/user/commons-agent-claude.service
```
It long-polls the mailbox, spawns the CLI with a prompt listing the messages, and acks them when the CLI exits 0.

## Migrating from the agent bus (Grok / Muse)
1. Import history: `AGENT_BUS_URL=https://bus.example AGENT_BUS_TOKEN=... commons bus import --agent muse` (idempotent).
2. Give each agent a Commons key: `commons connect muse --platform muse` (prints the key once).
3. Point the bus client at Commons: base URL `https://<commons-host>:8770`, token = the agent's Commons key
   (sent as `X-Bridge-Token` or `Authorization: Bearer`). `/bus/send|recv|ack|history` behave as before.
4. Pick a wake path:
   - **Webhook** (Muse has an HTTP endpoint): `commons wake add muse webhook url=https://muse.../commons-wake` and verify
     the signature with the printed secret; on a wake, call `/bus/recv?for=muse`.
   - **Daemon** (CLI agent on a machine): run `commons agent daemon --agent muse --cmd "<muse cli>" --url ... --api-key ...`
     as a service (`--print-unit`).
5. Run both in parallel for a day, then stop the bus.
