"""Minimal A2A (Agent2Agent protocol) HTTP client stub: discover an agent card and send a message.

Implements the JSON-RPC calls from the A2A spec: ``message/send``, ``message/stream`` (SSE), ``tasks/get`` (polling)
and ``tasks/cancel``. The matching server endpoint is in a2a_server.py.
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


def _rpc(url: str, method: str, params: dict, timeout: float = 30, headers: dict | None = None) -> dict:
    payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        return json.loads(r.read())


def get_task(url: str, task_id: str, history_length: int | None = None, headers: dict | None = None) -> dict:
    return _rpc(url, "tasks/get", {"id": task_id, **({"historyLength": history_length} if history_length else {})},
                headers=headers)


def cancel_task(url: str, task_id: str, headers: dict | None = None) -> dict:
    return _rpc(url, "tasks/cancel", {"id": task_id}, headers=headers)


def wait_task(url: str, task_id: str, timeout: float = 600, interval: float = 2, headers: dict | None = None) -> dict:
    """Poll tasks/get until the task reaches a final state (completed/failed/canceled/rejected) or timeout."""
    import time
    end = time.time() + timeout
    while True:
        r = get_task(url, task_id, headers=headers)
        st = ((r.get("result") or {}).get("status") or {}).get("state")
        if st in ("completed", "failed", "canceled", "rejected", "input-required", "auth-required") or time.time() > end:
            return r
        time.sleep(interval)


def stream_message(url: str, text: str, context_id: str | None = None, timeout: float = 600,
                   headers: dict | None = None):
    """message/stream: yields JSON-RPC result events (Task, status-update, artifact-update) from the SSE stream."""
    text, _ = redact(text)
    payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": "message/stream",
               "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": text}],
                                      "messageId": str(uuid.uuid4()), **({"contextId": context_id} if context_id else {})}}}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "text/event-stream",
                                          **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        for raw in r:
            line = raw.decode().strip()
            if line.startswith("data:"):
                ev = json.loads(line[5:].strip())
                yield ev.get("result", ev)
                res = ev.get("result") or {}
                if res.get("kind") == "status-update" and res.get("final"):
                    return
