"""Minimal A2A (Agent2Agent protocol) HTTP client stub: discover an agent card and send a message.

Implements the JSON-RPC ``message/send`` call from the A2A spec. Streaming, push notifications and
task polling are left for v2.
"""
from __future__ import annotations

import json
import urllib.request
import uuid

from .redact import redact


def fetch_agent_card(base_url: str, timeout: float = 10) -> dict:
    base = base_url.rstrip("/")
    last = None
    for path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
        try:
            with urllib.request.urlopen(base + path, timeout=timeout) as r:  # noqa: S310
                return json.loads(r.read())
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"no agent card at {base}: {last}")


def send_message(url: str, text: str, context_id: str | None = None, timeout: float = 30,
                 headers: dict | None = None) -> dict:
    text, _ = redact(text)  # never ship secrets to external agents
    payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/send",
               "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": text}],
                                      "messageId": str(uuid.uuid4()),
                                      **({"contextId": context_id} if context_id else {})}}}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - user-configured endpoint
        return json.loads(r.read())
