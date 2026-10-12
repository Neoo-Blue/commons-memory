"""A2A (Agent2Agent) server endpoint: external agents can hand Commons a task and follow it.

Implements the A2A JSON-RPC 2.0 binding:
  GET  /.well-known/agent-card.json   agent card (also /.well-known/agent.json for older clients)
  POST /a2a   message/send       -> creates a Commons job (orchestrated across connected agents) and returns a Task
              message/stream     -> SSE stream of Task, TaskStatusUpdateEvent and TaskArtifactUpdateEvent until final
              tasks/get          -> poll a task (state, artifacts, history)
              tasks/cancel       -> cancel the underlying job
              tasks/resubscribe  -> re-attach an SSE stream to a running task
Inbound text is redacted and treated as an untrusted task description. Message metadata may carry
{"pattern": "...", "project": "...", "budget": {...}}. Auth: served behind the same AuthMiddleware as MCP.
"""
from __future__ import annotations

import asyncio
import time
import json
import uuid
from datetime import datetime, timezone

from .ids import now
from .redact import redact
from .store import CommonsError, Store

STATE = {"running": "working", "done": "completed", "failed": "failed", "cancelled": "canceled",
         "budget_exceeded": "failed", "pending": "submitted"}
FINAL = {"completed", "failed", "canceled", "rejected"}


def agent_card(base_url: str, version: str = "") -> dict:
    from . import __version__
    return {"protocolVersion": "0.3.0", "name": "Commons", "version": version or __version__,
            "description": "Shared memory + multi-agent orchestration hub. Send a task; Commons decomposes it and "
                           "routes it across connected agents with review gates, then returns the merged result.",
            "url": base_url.rstrip("/") + "/a2a", "preferredTransport": "JSONRPC",
            "capabilities": {"streaming": True, "pushNotifications": True, "stateTransitionHistory": True},
            "defaultInputModes": ["text/plain"], "defaultOutputModes": ["text/plain"],
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}, "security": [{"bearer": []}],
            "skills": [{"id": "orchestrate", "name": "Orchestrate a task",
                        "description": "Decompose and execute a task across agents (patterns: auto, plan_execute_verify, "
                                       "cross_review, debate, consensus, parallel)", "tags": ["multi-agent", "planning"]},
                       {"id": "memory", "name": "Shared memory", "description": "Ask about what other agents did",
                        "tags": ["memory"]}]}


def _ts(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


class A2AService:
    def __init__(self, store: Store):
        self.s = store
        from .orchestrator import Orchestrator
        self.o = Orchestrator(store)

    def _row(self, tid: str) -> dict:
        r = self.s.q("SELECT * FROM a2a_tasks WHERE id=?", (tid,))
        if not r:
            raise KeyError(tid)
        return dict(r[0])

    def own(self, tid: str, caller: str | None) -> None:
        """Only the agent that created a task may read, cancel, resubscribe or redirect its push notifications. A
        foreign task looks exactly like a missing one (no id probing)."""
        r = self._row(tid)
        if caller is not None and r.get("caller") not in (None, caller):
            raise KeyError(tid)

    def task(self, tid: str, history_length: int | None = None) -> dict:
        t = self._row(tid)
        st = self.o.status(t["job_id"])
        state = STATE.get(st["status"], "working")
        msg = json.loads(t["message"] or "{}")
        task = {"kind": "task", "id": tid, "contextId": t["context_id"],
                "status": {"state": state, "timestamp": _ts(now())}, "history": [msg][: history_length or 1],
                "metadata": {"commons_job_id": t["job_id"], "subtasks": [
                    {"key": s["key"], "type": s["type"], "status": s["status"], "agent": s["agent"]} for s in st["subtasks"]]}}
        if state in FINAL and st.get("result"):
            task["artifacts"] = [{"artifactId": f"{tid}-result", "name": "result",
                                  "parts": [{"kind": "text", "text": st["result"]}]}]
        if state != "submitted":
            done = sum(1 for s in st["subtasks"] if s["status"] == "done")
            task["status"]["message"] = {"kind": "message", "role": "agent", "messageId": str(uuid.uuid4()),
                                         "parts": [{"kind": "text", "text": f"{done}/{len(st['subtasks'])} subtasks done"}]}
        return task

    def send(self, params: dict, caller: str | None) -> dict:
        m = params.get("message") or {}
        text = "\n".join(p.get("text", "") for p in m.get("parts", []) if p.get("kind", "text") == "text").strip()
        if not text:
            raise CommonsError("message has no text parts")
        text, _ = redact(text)
        meta = m.get("metadata") or params.get("metadata") or {}
        ctx = m.get("contextId") or str(uuid.uuid4())
        agent = caller or self.s.ensure_agent("a2a-client")
        job = self.o.submit(text, pattern=meta.get("pattern", "auto"), project=meta.get("project"),
                            agent_id=agent, budget=meta.get("budget"), title=meta.get("title", "")[:120] or "")
        tid = str(uuid.uuid4())
        self.s.x("INSERT INTO a2a_tasks VALUES(?,?,?,?,?,?,?,?)",
                 (tid, ctx, job["job_id"], "submitted", agent, json.dumps(m | {"parts": [{"kind": "text", "text": text}]}),
                  now(), now()))
        self.s.relate(job["job_id"], agent, "requested_by")
        self.s.audit(agent, "a2a.message.send", tid, {"job": job["job_id"]})
        return self.task(tid)

    def cancel(self, tid: str) -> dict:
        t = self._row(tid)
        self.o.cancel(t["job_id"], "cancelled via A2A")
        return self.task(tid)


def _rpc_err(id_, code, msg):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": msg}}


def build_app(store: Store, base_url: str = "http://127.0.0.1:8765", poll: float = 0.5):
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, StreamingResponse
    from starlette.routing import Route

    svc = A2AService(store)

    async def card(_req: Request):
        return JSONResponse(agent_card(base_url))

    async def stream(tid: str, first: dict):
        last = None
        yield f"data: {json.dumps({'jsonrpc': '2.0', 'id': first['rid'], 'result': first['task']})}\n\n"
        while True:
            await asyncio.sleep(poll)
            t = await asyncio.to_thread(svc.task, tid)
            key = (t["status"]["state"], json.dumps(t["metadata"]["subtasks"]))
            final = t["status"]["state"] in FINAL
            if key != last:
                last = key
                ev = {"kind": "status-update", "taskId": tid, "contextId": t["contextId"], "status": t["status"],
                      "final": final and not t.get("artifacts")}
                yield f"data: {json.dumps({'jsonrpc': '2.0', 'id': first['rid'], 'result': ev})}\n\n"
            if final:
                for a in t.get("artifacts", []):
                    ev = {"kind": "artifact-update", "taskId": tid, "contextId": t["contextId"], "artifact": a,
                          "lastChunk": True}
                    yield f"data: {json.dumps({'jsonrpc': '2.0', 'id': first['rid'], 'result': ev})}\n\n"
                if t.get("artifacts"):
                    ev = {"kind": "status-update", "taskId": tid, "contextId": t["contextId"], "status": t["status"],
                          "final": True}
                    yield f"data: {json.dumps({'jsonrpc': '2.0', 'id': first['rid'], 'result': ev})}\n\n"
                return

    async def rpc(req: Request):
        try:
            body = await req.json()
        except Exception:  # noqa: BLE001
            return JSONResponse(_rpc_err(None, -32700, "parse error"))
        rid, method, params = body.get("id"), body.get("method"), body.get("params") or {}
        from .auth import ANON, PRINCIPAL
        caller = PRINCIPAL.get()
        caller = None if caller in (None, ANON) else caller
        try:
            if method == "message/send":
                t = await asyncio.to_thread(svc.send, params, caller)
                if (params.get("configuration") or {}).get("blocking"):
                    for _ in range(int(120 / poll)):
                        if t["status"]["state"] in FINAL:
                            break
                        await asyncio.sleep(poll)
                        t = await asyncio.to_thread(svc.task, t["id"])
                return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": t})
            if method in ("message/stream", "tasks/resubscribe"):
                if method == "tasks/resubscribe":
                    svc.own(params["id"], caller)
                t = await asyncio.to_thread(svc.send, params, caller) if method == "message/stream" \
                    else await asyncio.to_thread(svc.task, params["id"])
                return StreamingResponse(stream(t["id"], {"rid": rid, "task": t}), media_type="text/event-stream")
            if method in ("tasks/get", "tasks/cancel", "tasks/pushNotificationConfig/set",
                          "tasks/pushNotificationConfig/get"):
                svc.own(params.get("id") or params.get("taskId"), caller)
            if method == "tasks/get":
                return JSONResponse({"jsonrpc": "2.0", "id": rid,
                                     "result": await asyncio.to_thread(svc.task, params["id"], params.get("historyLength"))})
            if method == "tasks/cancel":
                return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": await asyncio.to_thread(svc.cancel, params["id"])})
            if method == "tasks/pushNotificationConfig/set":
                from .messaging import check_url
                tid = params.get("taskId") or params.get("id")
                cfg = params.get("pushNotificationConfig") or {}
                await asyncio.to_thread(svc.task, tid)  # KeyError -> task not found
                check_url(cfg.get("url", ""))  # allowlist + SSRF checks (raises CommonsError)
                store.x("INSERT OR REPLACE INTO a2a_push(task_id,url,token,last_state,caller,updated_at) VALUES(?,?,?,?,?,?)",
                        (tid, cfg["url"], cfg.get("token"), None, caller, time.time()))
                return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": {"taskId": tid, "pushNotificationConfig":
                                                                             {"url": cfg["url"]}}})
            if method == "tasks/pushNotificationConfig/get":
                r = store.q("SELECT url FROM a2a_push WHERE task_id=?", (params.get("id") or params.get("taskId"),))
                if not r:
                    raise KeyError("no config")
                return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": {"taskId": params.get("id") or params.get("taskId"),
                                                                             "pushNotificationConfig": {"url": r[0]["url"]}}})
            return JSONResponse(_rpc_err(rid, -32601, f"method not found: {method}"))
        except KeyError:
            return JSONResponse(_rpc_err(rid, -32001, "task not found"))
        except CommonsError as e:
            return JSONResponse(_rpc_err(rid, -32602, str(e)))

    return Starlette(routes=[Route("/.well-known/agent-card.json", card), Route("/.well-known/agent.json", card),
                             Route("/a2a", rpc, methods=["POST"])])


def mount(mcp_app, a2a_app):
    """Route /a2a and the agent card to the A2A app, everything else to MCP."""
    async def app(scope, receive, send):
        p = scope.get("path", "") if scope["type"] == "http" else ""
        if p == "/a2a" or p.startswith("/.well-known/agent"):
            return await a2a_app(scope, receive, send)
        return await mcp_app(scope, receive, send)
    return app


def push_due(store: Store) -> list[dict]:
    """Background job: POST the task to each registered push URL when its state changed (A2A push notifications)."""
    from .messaging import http_post
    svc = A2AService(store)
    out = []
    for r in store.q("SELECT * FROM a2a_push"):
        try:
            t = svc.task(r["task_id"])
        except KeyError:
            store.x("DELETE FROM a2a_push WHERE task_id=?", (r["task_id"],))
            continue
        st = t["status"]["state"]
        if st == r["last_state"]:
            continue
        h = {"Content-Type": "application/json"} | ({"X-A2A-Notification-Token": r["token"]} if r["token"] else {})
        try:
            code = http_post(r["url"], json.dumps(t).encode(), h)
            ok = 200 <= code < 300
        except Exception as e:  # noqa: BLE001
            ok, code = False, str(e)[:100]
        if ok:
            store.x("UPDATE a2a_push SET last_state=?, updated_at=? WHERE task_id=?", (st, time.time(), r["task_id"]))
        out.append({"task": r["task_id"], "state": st, "ok": ok, "code": code})
    return out

