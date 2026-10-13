"""HTTP surface for agent messaging, presence and live wake streams.

  /bus/send  /bus/recv  /bus/ack  /bus/history  /bus/health   agent-bus compatible shim (X-Bridge-Token = agent key)
  /agent/send /agent/recv /agent/ack /agent/history            native (typed messages, threads, lease)
  /agent/heartbeat  /agent/presence                            presence
  /agent/stream (SSE)  /agent/ws (WebSocket)                   live wake events for the authenticated agent
  /agent/wake/ack  /agent/wake/replay                          v0.9.3: ack handled events; replay since ack / Last-Event-ID
The caller is always the authenticated principal: you can only send as yourself and read your own mailbox.
"""
from __future__ import annotations

import asyncio
import json
import queue

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .messaging import HUB, Messaging, board, heartbeat, wake_ack, wake_events_since
from .store import CommonsError, Store


def _me(store: Store):
    from .auth import ANON, PRINCIPAL
    p = PRINCIPAL.get()
    if p in (None, ANON):
        raise PermissionError("authentication required")
    a = store.get_agent(p)
    return p, (a["name"] if a else p)


def _check_self(name: str, claimed: str | None, field: str):
    if claimed and claimed not in (name,):
        raise PermissionError(f"'{field}' must be your own agent name ({name}); you sent '{claimed}'")


def build_app(store: Store) -> Starlette:
    m = Messaging(store)

    def guard(fn):
        async def h(req: Request):
            try:
                return await fn(req)
            except PermissionError as e:
                return JSONResponse({"ok": False, "error": str(e)}, 403)
            except CommonsError as e:
                return JSONResponse({"ok": False, "error": str(e)}, 400)
            except (ValueError, KeyError) as e:
                return JSONResponse({"ok": False, "error": f"bad request: {e}"}, 400)
        return h

    async def body(req):
        try:
            return await req.json()
        except Exception:  # noqa: BLE001
            return {}

    @guard
    async def send(req: Request):
        aid, name = _me(store)
        b = await body(req)
        _check_self(name, b.get("from"), "from")
        if not b.get("to"):
            raise ValueError("to required")
        r = await asyncio.to_thread(m.send, aid, b["to"], b.get("text") or b.get("body") or "",
                                    mtype=b.get("type", "message"), subject=b.get("subject", ""),
                                    thread=b.get("thread"), reply_to=b.get("reply_to"), project=b.get("project"))
        return JSONResponse({"ok": True} | r)

    @guard
    async def recv(req: Request):
        aid, name = _me(store)
        q = req.query_params
        _check_self(name, q.get("for"), "for")
        native = req.url.path.startswith("/agent/")
        msgs = await asyncio.to_thread(m.recv, aid, wait=float(q.get("wait", 0)), limit=int(q.get("limit", 50)),
                                       lease=float(q.get("lease", 30 if native else 0)), ack=q.get("ack", "0") in ("1", "true"),
                                       types=q.get("types").split(",") if q.get("types") else None)
        if not native:  # bus wire format
            msgs = [{"id": x["id"], "from": x["from"], "to": x["to"], "text": x["text"], "ts": x["ts"],
                     "type": x["type"], "thread": x["thread"]} for x in msgs]
        return JSONResponse({"ok": True, "messages": msgs})

    @guard
    async def ack(req: Request):
        aid, name = _me(store)
        b = await body(req)
        _check_self(name, b.get("for"), "for")
        ids = b.get("ids") or []
        if isinstance(ids, str):
            ids = ids.split(",")
        return JSONResponse({"ok": True} | m.ack(aid, ids))

    @guard
    async def history(req: Request):
        aid, name = _me(store)
        q = req.query_params
        _check_self(name, q.get("for"), "for")
        native = req.url.path.startswith("/agent/")
        h = await asyncio.to_thread(m.history, aid, peer=q.get("peer"), thread=int(q["thread"]) if q.get("thread") else None,
                                    limit=min(int(q.get("limit", 100)), 1000), before=int(q["before"]) if q.get("before") else None,
                                    preview=not native)
        return JSONResponse({"ok": True, "history": h})

    async def health(_req):
        return JSONResponse({"ok": True, "service": "commons", "bus_compatible": True})

    @guard
    async def hb(req: Request):
        aid, _ = _me(store)
        b = await body(req)
        return JSONResponse({"ok": True} | heartbeat(store, aid, status=b.get("status"), sleeping=b.get("sleeping"),
                                                      client=b.get("client") or "http"))

    @guard
    async def pres(_req):
        _me(store)
        return JSONResponse({"ok": True, "agents": board(store)})

    async def stream(req: Request):
        try:
            aid, name = _me(store)
        except PermissionError as e:
            return JSONResponse({"ok": False, "error": str(e)}, 403)
        sub = HUB.subscribe(aid)  # subscribe before reading the replay so nothing falls in between
        heartbeat(store, aid, client="sse")
        since = req.headers.get("last-event-id") or req.query_params.get("since") or ""
        replay = await asyncio.to_thread(wake_events_since, store, aid, since or "ack")

        async def gen():
            seen = set()
            try:
                yield f"event: hello\ndata: {json.dumps({'agent': name, 'unread': m.unread(aid), 'replay': len(replay)})}\n\n"
                for ev in replay:  # missed while disconnected (since Last-Event-ID / ?since= / the last ack)
                    seen.add(ev.get("event_id"))
                    yield f"event: wake\nid: {ev.get('event_id')}\ndata: {json.dumps(ev)}\n\n"
                ticks = 0
                while True:
                    try:
                        ev = sub.get_nowait()
                        if ev.get("event_id") in seen:
                            continue
                        yield f"event: wake\nid: {ev.get('event_id')}\ndata: {json.dumps(ev)}\n\n"
                        continue
                    except queue.Empty:
                        pass
                    if ticks % 4 == 0 and await req.is_disconnected():
                        break
                    await asyncio.sleep(0.05)
                    ticks += 1
                    if ticks % 300 == 0:  # every ~15 s: keepalive + presence
                        heartbeat(store, aid, client="sse")
                        yield ": keepalive\n\n"
            finally:
                HUB.unsubscribe(aid, sub)
        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    async def ws(sock: WebSocket):
        try:
            aid, name = _me(store)
        except PermissionError:
            await sock.close(code=4401)
            return
        await sock.accept()
        sub = HUB.subscribe(aid)
        heartbeat(store, aid, client="ws")
        since = sock.query_params.get("since") or sock.headers.get("last-event-id") or "ack"
        replay = await asyncio.to_thread(wake_events_since, store, aid, since)
        await sock.send_json({"type": "hello", "agent": name, "unread": m.unread(aid), "replay": len(replay)})
        for ev in replay:
            await sock.send_json({"type": "wake"} | ev)

        async def pump():
            while True:
                try:
                    ev = sub.get_nowait()
                    await sock.send_json({"type": "wake"} | ev)
                except queue.Empty:
                    await asyncio.sleep(0.05)
        task = asyncio.create_task(pump())
        try:
            while True:
                msg = await sock.receive_json()
                op = msg.get("op")
                if op == "heartbeat":
                    await sock.send_json({"type": "presence"} | heartbeat(store, aid, status=msg.get("status"), client="ws"))
                elif op == "wake_ack":
                    try:
                        await sock.send_json({"type": "wake_acked"} | wake_ack(store, aid, str(msg.get("event_id") or "")))
                    except CommonsError as e:
                        await sock.send_json({"type": "error", "error": str(e)})
                elif op == "ack":
                    await sock.send_json({"type": "acked"} | m.ack(aid, msg.get("ids") or []))
                elif op == "recv":
                    await sock.send_json({"type": "messages", "messages": await asyncio.to_thread(
                        m.recv, aid, wait=0, lease=float(msg.get("lease", 30)))})
                elif op == "send":
                    try:
                        r = await asyncio.to_thread(m.send, aid, msg["to"], msg.get("text", ""), mtype=msg.get("mtype", "message"),
                                                    thread=msg.get("thread"), reply_to=msg.get("reply_to"))
                        await sock.send_json({"type": "sent"} | r)
                    except (CommonsError, KeyError) as e:
                        await sock.send_json({"type": "error", "error": str(e)})
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            task.cancel()
            HUB.unsubscribe(aid, sub)

    @guard
    async def wack(req: Request):
        aid, _ = _me(store)
        b = await body(req)
        return JSONResponse({"ok": True} | wake_ack(store, aid, str(b.get("event_id") or "")))

    @guard
    async def wreplay(req: Request):
        aid, _ = _me(store)
        return JSONResponse({"ok": True, "events": wake_events_since(store, aid, req.query_params.get("since") or "ack")})

    @guard
    async def hook(req: Request):
        aid, _name = _me(store)
        b = await body(req)
        from .capture import Capture
        r = await asyncio.to_thread(Capture(store).event, aid, b.get("event", ""), b.get("payload") or {},
                                    b.get("harness", "generic"), b.get("project"))
        return JSONResponse(r)

    routes = [Route("/agent/hook", hook, methods=["POST"]), Route("/agent/wake/ack", wack, methods=["POST"]),
              Route("/agent/wake/replay", wreplay)]
    for prefix in ("/bus", "/agent"):
        routes += [Route(f"{prefix}/send", send, methods=["POST"]), Route(f"{prefix}/recv", recv),
                   Route(f"{prefix}/ack", ack, methods=["POST"]), Route(f"{prefix}/history", history)]
    routes += [Route("/bus/health", health), Route("/agent/heartbeat", hb, methods=["POST"]),
               Route("/agent/presence", pres), Route("/agent/stream", stream), WebSocketRoute("/agent/ws", ws)]
    return Starlette(routes=routes)


def mount(inner, agent_app):
    async def app(scope, receive, send):
        p = scope.get("path", "") if scope["type"] in ("http", "websocket") else ""
        if p.startswith(("/bus/", "/agent/")):
            return await agent_app(scope, receive, send)
        return await inner(scope, receive, send)
    return app
