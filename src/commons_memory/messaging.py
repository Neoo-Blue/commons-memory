"""Native real-time agent messaging, presence and wake (replaces an external agent bus).

Messages
  direct (to an agent) or broadcast (to "*"); typed: message | task | question | review | fyi | handoff;
  threads (thread_id = root message id, reply_to); stored in `messages`, one row per recipient in
  `message_deliveries` (at-least-once: a message is returned by recv until acked; `lease` hides it for a while after
  each delivery so a crashed reader gets it again later). Long-poll recv. Every message is redacted and graphed
  (sender -sent-> message -to-> recipient, message -in_thread-> root).

Presence
  heartbeats (any authenticated call, explicit `heartbeat`, live SSE/WS/MCP connections, the agent daemon) ->
  online (<90 s) / idle (<15 min) / sleeping (agent said so, still wakeable) / offline. `ready(agent)` = online/idle or
  wakeable (has a wake channel or a live stream). `ensure_ready` wakes and waits, then falls back to escalation.

Wake
  events (message, task, inbox, handoff, review, escalation, schedule) fan out to the agent's channels:
  live streams (SSE/WS, in-process pub/sub), MCP sessions (notifications/message), signed webhooks (HMAC-SHA256 over
  "<ts>.<body>", retries with exponential backoff, dead-letter), ntfy, Pushover, Slack, Discord, Telegram, SMTP e-mail
  (humans) and a local command hook (spawns e.g. `claude -p` on the Commons host). Payloads carry ids + a short
  redacted preview only, plus a nonce and timestamp for replay protection. Outbound URLs go through an allowlist with
  private-address (SSRF) blocking. Per-agent wake rate limit with coalescing.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import queue
import secrets as _secrets
import shlex
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .ids import new_id, now
from .redact import redact
from .store import CommonsError, Store

MSG_TYPES = ("message", "task", "question", "review", "fyi", "handoff")
EVENTS = ("message", "task", "inbox", "handoff", "review", "escalation", "schedule", "test")
CHANNEL_KINDS = ("webhook", "ntfy", "pushover", "slack", "discord", "telegram", "email", "command")
ONLINE_S, IDLE_S = 90, 900
BACKOFF = [2, 8, 30, 120, 600, 1800]
# events that target one agent and carry mailbox work (v0.9.3 instant wake adds assignment, lease, debate and mentions)
FETCH_EVENTS = ("message", "task", "review", "handoff", "task_assigned", "lease_expired", "debate_turn", "board_mention",
                "escalation")  # seconds between webhook attempts; then dead-letter

SCHEMA_V7 = """
CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT, ext_id TEXT, sender TEXT, recipient TEXT,
  mtype TEXT, thread_id INTEGER, reply_to INTEGER, subject TEXT, body TEXT, meta TEXT, project_id TEXT, created_at REAL);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread_id);
CREATE UNIQUE INDEX IF NOT EXISTS messages_ext ON messages(ext_id);
CREATE TABLE IF NOT EXISTS message_deliveries(msg_id INTEGER, agent_id TEXT, status TEXT, visible_at REAL,
  delivered_at REAL, acked_at REAL, attempts INTEGER DEFAULT 0, PRIMARY KEY(msg_id, agent_id));
CREATE INDEX IF NOT EXISTS message_deliveries_agent ON message_deliveries(agent_id, acked_at);
CREATE TABLE IF NOT EXISTS agent_presence(agent_id TEXT PRIMARY KEY, state TEXT, status_text TEXT, last_heartbeat REAL,
  sleeping INTEGER DEFAULT 0, live INTEGER DEFAULT 0, client TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS wake_channels(id TEXT PRIMARY KEY, agent_id TEXT, kind TEXT, config TEXT, secret TEXT,
  enabled INTEGER DEFAULT 1, created_at REAL, last_ok REAL, last_error TEXT, failures INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS wake_events(id TEXT PRIMARY KEY, agent_id TEXT, event TEXT, ref TEXT, preview TEXT,
  payload TEXT, status TEXT, created_at REAL);
CREATE INDEX IF NOT EXISTS wake_events_agent ON wake_events(agent_id, created_at);
CREATE TABLE IF NOT EXISTS wake_deliveries(id TEXT PRIMARY KEY, event_id TEXT, channel_id TEXT, status TEXT,
  attempts INTEGER DEFAULT 0, next_at REAL, last_error TEXT, updated_at REAL);
CREATE INDEX IF NOT EXISTS wake_deliveries_due ON wake_deliveries(status, next_at);
CREATE TABLE IF NOT EXISTS a2a_push(task_id TEXT PRIMARY KEY, url TEXT, token TEXT, last_state TEXT, caller TEXT,
  updated_at REAL);
"""


# ------------------------------------------------------------------ in-process pub/sub (SSE / WS / long-poll wakeups)
class Hub:
    """Listeners per agent id. Each listener is a queue.Queue; async code polls it via asyncio.to_thread or get_nowait."""

    def __init__(self):
        self.lock = threading.Lock()
        self.listeners: dict[str, set] = {}
        self.pollers: dict[str, int] = {}  # agents blocked in a long-poll recv (`commons agent daemon`): instantly wakeable
        self.cond = threading.Condition()
        self.seq = 0

    def subscribe(self, agent_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self.lock:
            self.listeners.setdefault(agent_id, set()).add(q)
        return q

    def unsubscribe(self, agent_id: str, q) -> None:
        with self.lock:
            self.listeners.get(agent_id, set()).discard(q)

    def live(self, agent_id: str) -> int:
        with self.lock:
            return len(self.listeners.get(agent_id, ())) + self.pollers.get(agent_id, 0)

    def streams(self, agent_id: str) -> int:
        """Open SSE/WebSocket listeners only (the 'instant' wake mode)."""
        with self.lock:
            return len(self.listeners.get(agent_id, ()))

    def polling(self, agent_id: str, delta: int) -> None:
        with self.lock:
            self.pollers[agent_id] = max(0, self.pollers.get(agent_id, 0) + delta)

    def publish(self, agent_id: str, payload: dict) -> int:
        with self.lock:
            qs = list(self.listeners.get(agent_id, ()))
        for q in qs:
            try:
                q.put_nowait(payload)
            except queue.Full:
                pass
        with self.cond:
            self.seq += 1
            self.cond.notify_all()
        return len(qs)

    def wait(self, seq: int, timeout: float) -> int:
        with self.cond:
            if self.seq == seq:
                self.cond.wait(timeout)
            return self.seq


HUB = Hub()
MCP_SESSIONS: dict[str, list] = {}  # agent_id -> [(session, loop)]
_MCP_LOCK = threading.Lock()


def register_mcp_session(agent_id: str, session, loop) -> None:
    with _MCP_LOCK:
        lst = MCP_SESSIONS.setdefault(agent_id, [])
        if not any(s is session for s, _ in lst):
            lst.append((session, loop))
            del lst[:-5]


def _mcp_notify(agent_id: str, payload: dict) -> int:
    import asyncio
    n = 0
    with _MCP_LOCK:
        lst = list(MCP_SESSIONS.get(agent_id, []))
    for sess, loop in lst:
        try:
            if loop.is_closed():
                raise RuntimeError("closed")
            asyncio.run_coroutine_threadsafe(sess.send_log_message(level="notice", data=payload, logger="commons.wake"), loop)
            n += 1
        except Exception:  # noqa: BLE001 - dead session: forget it
            with _MCP_LOCK:
                MCP_SESSIONS[agent_id] = [x for x in MCP_SESSIONS.get(agent_id, []) if x[0] is not sess]
    return n


# ------------------------------------------------------------------ SSRF-safe outbound HTTP
FIXED_HOSTS = {"ntfy.sh", "api.pushover.net", "hooks.slack.com", "discord.com", "discordapp.com", "api.telegram.org"}


def _allowlist() -> list[str]:
    return [x.strip().lower() for x in os.environ.get("COMMONS_WEBHOOK_ALLOWLIST", "").split(",") if x.strip()]


def _host_allowed(host: str, entries: list[str]) -> bool:
    host = host.lower()
    for e in entries:
        if "/" in e:
            try:
                if ipaddress.ip_address(host) in ipaddress.ip_network(e, strict=False):
                    return True
            except ValueError:
                continue
        elif e.startswith("*.") and (host.endswith(e[1:]) or host == e[2:]):
            return True
        elif host == e:
            return True
    return False


def check_url(url: str, extra_hosts: set[str] | None = None) -> str:
    """Validate an outbound URL. Rules: http(s) only; https unless the host is allowlisted; if
    COMMONS_WEBHOOK_ALLOWLIST is set the host must match it (or be a fixed adapter host); every resolved address must
    be public unless the host itself is explicitly allowlisted (blocks loopback, RFC1918, link-local/metadata, ULA)."""
    u = urllib.parse.urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise CommonsError("webhook URL must be http(s)://host/...")
    host = u.hostname.lower()
    al = _allowlist()
    explicit = _host_allowed(host, al)
    fixed = host in FIXED_HOSTS | (extra_hosts or set())
    if al and not (explicit or fixed):
        raise CommonsError(f"{host} is not in COMMONS_WEBHOOK_ALLOWLIST")
    if u.scheme != "https" and not explicit:
        raise CommonsError("plain-http webhooks are only allowed for hosts listed in COMMONS_WEBHOOK_ALLOWLIST")
    if not explicit:
        try:
            infos = socket.getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
        except socket.gaierror as e:
            raise CommonsError(f"cannot resolve {host}") from e
        for info in infos:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
            if not ip.is_global or ip.is_multicast:
                raise CommonsError(f"{host} resolves to non-public address {ip}; allowlist it explicitly to permit")
    return url


def http_post(url: str, body: bytes, headers: dict, timeout: float = 10, extra_hosts: set[str] | None = None) -> int:
    check_url(url, extra_hosts)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):  # a redirect could point at an internal address
            return None
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, data=body, method="POST", headers={"User-Agent": "commons-wake/1"} | headers)
    try:
        with opener.open(req, timeout=timeout) as r:  # noqa: S310 - validated above
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


# ------------------------------------------------------------------ webhook signing
def sign(secret: str, ts: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()


class ReplayGuard:
    """Receiver-side check: valid HMAC, timestamp within `window` seconds, delivery id never seen before."""

    def __init__(self, secret: str, window: int = 300):
        self.secret, self.window, self.seen = secret, window, {}

    def verify(self, headers: dict, body: bytes) -> bool:
        h = {k.lower(): v for k, v in headers.items()}
        ts, sig, did = h.get("x-commons-timestamp", ""), h.get("x-commons-signature", ""), h.get("x-commons-delivery", "")
        try:
            if abs(time.time() - float(ts)) > self.window:
                return False
        except ValueError:
            return False
        if not did or did in self.seen or not hmac.compare_digest(sign(self.secret, ts, body), sig):
            return False
        t = time.time()
        self.seen = {k: v for k, v in self.seen.items() if t - v < self.window * 2}
        self.seen[did] = t
        return True


# ------------------------------------------------------------------ main service
class Messaging:
    def __init__(self, s: Store):
        self.s = s

    # ---- helpers
    def _aid(self, name_or_id: str) -> str:
        if name_or_id in ("*", "all", "broadcast"):
            return "*"
        a = self.s.get_agent(name_or_id)
        return a["id"] if a else self.s.ensure_agent(name_or_id)

    def _name(self, aid: str | None) -> str | None:
        if not aid:
            return None
        if aid == "*":
            return "*"
        r = self.s.q("SELECT name FROM agents WHERE id=?", (aid,))
        return r[0]["name"] if r else aid

    # ---- send
    def send(self, sender: str | None, to: str, body: str, *, mtype: str = "message", subject: str = "",
             thread: int | None = None, reply_to: int | None = None, meta: dict | None = None,
             project: str | None = None, ext_id: str | None = None, created_at: float | None = None,
             wake: bool = True, event: str | None = None) -> dict:
        if mtype not in MSG_TYPES:
            raise CommonsError(f"type must be one of {', '.join(MSG_TYPES)}")
        body, found = redact(body or "")
        subject = redact(subject or "")[0]
        if not body.strip():
            raise CommonsError("empty message")
        sid = self._aid(sender) if sender else None
        rid = self._aid(to)
        if reply_to and not thread:
            r = self.s.q("SELECT thread_id, id FROM messages WHERE id=?", (reply_to,))
            thread = (r[0]["thread_id"] or r[0]["id"]) if r else None
        if ext_id and self.s.q("SELECT 1 FROM messages WHERE ext_id=?", (ext_id,)):
            return {"id": self.s.q("SELECT id FROM messages WHERE ext_id=?", (ext_id,))[0]["id"], "duplicate": True}
        ts = created_at or now()
        with self.s.lock:
            cur = self.s.db.execute(
                "INSERT INTO messages(ext_id,sender,recipient,mtype,thread_id,reply_to,subject,body,meta,project_id,created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (ext_id, sid, rid, mtype, thread, reply_to, subject, body, json.dumps(meta or {}),
                 self.s.project_id(project) if project else None, ts))
            mid = cur.lastrowid
        if not thread:
            self.s.x("UPDATE messages SET thread_id=? WHERE id=?", (mid, mid))
        targets = [r["id"] for r in self.s.q("SELECT id FROM agents WHERE id<>? AND name NOT IN ('user','cli')", (sid or "",))] \
            if rid == "*" else [rid]
        for t in targets:
            self.s.x("INSERT INTO message_deliveries(msg_id,agent_id,status,visible_at,attempts) VALUES(?,?,?,?,0) "
                     "ON CONFLICT(msg_id, agent_id) DO NOTHING", (mid, t, "pending", 0))
        ref = f"msg:{mid}"
        try:
            if sid:
                self.s.relate(sid, ref, "sent")
            if rid != "*":
                self.s.relate(ref, rid, "to")
            if thread and thread != mid:
                self.s.relate(ref, f"msg:{thread}", "in_thread")
        except Exception:  # noqa: BLE001 - graph links are best effort
            pass
        if wake:
            ev = event or {"task": "task", "review": "review", "handoff": "handoff"}.get(mtype, "message")
            for t in targets:
                emit(self.s, t, ev, ref=str(mid), preview=(subject or body), sender=self._name(sid), mtype=mtype)
        out = {"id": mid, "thread": thread or mid, "to": self._name(rid), "recipients": len(targets)}
        return out | ({"redacted": found} if found else {})

    # ---- receive / ack
    def _pending(self, aid: str, limit: int, include_broadcast: bool = True) -> list:
        return self.s.q("SELECT m.*, d.attempts, d.delivered_at FROM message_deliveries d JOIN messages m ON m.id=d.msg_id "
                        "WHERE d.agent_id=? AND d.acked_at IS NULL AND d.visible_at<=? ORDER BY m.id LIMIT ?",
                        (aid, now(), limit))

    def fmt(self, r) -> dict:
        return {"id": r["id"], "from": self._name(r["sender"]), "to": self._name(r["recipient"]), "type": r["mtype"],
                "thread": r["thread_id"], "reply_to": r["reply_to"], "subject": r["subject"] or "", "text": r["body"],
                "ts": r["created_at"], "meta": json.loads(r["meta"] or "{}")} | (
                    {"attempts": r["attempts"]} if "attempts" in r.keys() else {})

    def recv(self, agent: str, *, wait: float = 0, limit: int = 50, lease: float = 30, ack: bool = False,
             types: list[str] | None = None) -> list[dict]:
        """At-least-once: returns unacked messages; each delivery hides it for `lease` seconds (0 = always visible
        until acked, like the agent bus). wait = long-poll seconds (max 60)."""
        aid = self._aid(agent)
        heartbeat(self.s, aid, client="recv")
        deadline = time.time() + min(max(wait, 0), 60)
        seq = HUB.seq
        HUB.polling(aid, 1) if wait > 0 else None
        try:
            while True:
                rows = [r for r in self._pending(aid, limit) if not types or r["mtype"] in types]
                if rows or time.time() >= deadline:
                    break
                seq = HUB.wait(seq, min(1.0, max(0.05, deadline - time.time())))  # wake on publish, else poll the DB
        finally:
            HUB.polling(aid, -1) if wait > 0 else None
        t = now()
        out = []
        for r in rows:
            with self.s.lock:  # claim it: two concurrent receivers (daemon + chat) must not both get one message
                won = self.s.db.execute(
                    "UPDATE message_deliveries SET status=?, delivered_at=COALESCE(delivered_at, ?), visible_at=?, "
                    "attempts=attempts+1, acked_at=? WHERE msg_id=? AND agent_id=? AND acked_at IS NULL AND visible_at<=?",
                    ("acked" if ack else "delivered", t, t + lease, t if ack else None, r["id"], aid, t)).rowcount == 1
            if won:
                out.append(self.fmt(r))
        return out

    def ack(self, agent: str, ids: list) -> dict:
        aid = self._aid(agent)
        n = 0
        for i in ids:
            try:
                i = int(str(i).removeprefix("msg:"))
            except ValueError:
                continue
            r = self.s.q("SELECT 1 FROM message_deliveries WHERE msg_id=? AND agent_id=? AND acked_at IS NULL", (i, aid))
            if r:
                self.s.x("UPDATE message_deliveries SET status='acked', acked_at=? WHERE msg_id=? AND agent_id=?", (now(), i, aid))
                n += 1
        return {"acked": n}

    def history(self, agent: str | None = None, *, peer: str | None = None, thread: int | None = None,
                limit: int = 100, before: int | None = None, preview: bool = False) -> list[dict]:
        sql, a = "SELECT * FROM messages WHERE 1=1", []
        if agent:
            aid = self._aid(agent)
            sql += " AND (sender=? OR recipient=? OR (recipient='*' AND EXISTS(SELECT 1 FROM message_deliveries d WHERE d.msg_id=messages.id AND d.agent_id=?)))"
            a += [aid, aid, aid]
        if peer:
            pid = self._aid(peer)
            sql += " AND (sender=? OR recipient=?)"
            a += [pid, pid]
        if thread:
            sql += " AND thread_id=?"
            a.append(thread)
        if before:
            sql += " AND id<?"
            a.append(before)
        rows = self.s.q(sql + " ORDER BY id DESC LIMIT ?", (*a, limit))
        out = [self.fmt(r) for r in reversed(rows)]
        if preview:
            out = [{"id": m["id"], "from": m["from"], "to": m["to"], "ts": m["ts"], "type": m["type"],
                    "preview": m["text"][:200]} for m in out]
        return out

    def unread(self, agent: str) -> int:
        return self.s.q("SELECT COUNT(*) n FROM message_deliveries WHERE agent_id=? AND acked_at IS NULL",
                        (self._aid(agent),))[0]["n"]


# ------------------------------------------------------------------ presence
def heartbeat(s: Store, agent_id: str, *, status: str | None = None, sleeping: bool | None = None,
              client: str | None = None) -> dict:
    t = now()
    r = s.q("SELECT * FROM agent_presence WHERE agent_id=?", (agent_id,))
    if r:
        sets, args = ["last_heartbeat=?", "updated_at=?"], [t, t]
        if status is not None:
            sets.append("status_text=?")
            args.append(redact(status)[0][:200])
        if sleeping is not None:
            sets.append("sleeping=?")
            args.append(1 if sleeping else 0)
        elif client not in ("recv", "wake"):
            sets.append("sleeping=0")  # activity ends sleep
        if client:
            sets.append("client=?")
            args.append(client[:60])
        s.x(f"UPDATE agent_presence SET {', '.join(sets)} WHERE agent_id=?", (*args, agent_id))
    else:
        # ON CONFLICT: two threads (daemon recv + a tool call) may both see no row and race to insert it
        s.x("INSERT INTO agent_presence(agent_id,state,status_text,last_heartbeat,sleeping,live,client,updated_at) "
            "VALUES(?,?,?,?,?,0,?,?) ON CONFLICT(agent_id) DO UPDATE SET last_heartbeat=excluded.last_heartbeat, "
            "updated_at=excluded.updated_at", (agent_id, "online", redact(status or "")[0][:200], t, 1 if sleeping else 0, client, t))
    return presence(s, agent_id)


def presence(s: Store, agent_id: str) -> dict:
    r = s.q("SELECT p.*, a.name, a.last_seen AS agent_seen FROM agents a LEFT JOIN agent_presence p "
            "ON p.agent_id=a.id WHERE a.id=?", (agent_id,))
    if not r:
        raise CommonsError("unknown agent")
    r = dict(r[0])
    live = HUB.live(agent_id) + len(MCP_SESSIONS.get(agent_id, []))
    age = now() - (r.get("last_heartbeat") or 0)
    if r.get("sleeping") and not live:  # an agent that said "going to sleep" is asleep until it acts again
        state = "sleeping"
    elif live or age < ONLINE_S:
        state = "online"
    elif age < IDLE_S:
        state = "idle"
    else:
        state = "offline"
    chans = [c["kind"] for c in s.q("SELECT kind FROM wake_channels WHERE agent_id=? AND enabled=1", (agent_id,))]
    wakeable = bool(chans) or live > 0
    seen = bool(r.get("agent_seen"))
    streams = HUB.streams(agent_id)
    if streams:
        mode = "instant"  # holds an SSE/WebSocket wake stream open: woken within a second
    elif HUB.pollers.get(agent_id) or (r.get("client") == "daemon" and age < ONLINE_S):
        mode = "long-poll"
    elif chans:
        mode = "push"  # webhook / ntfy / command channel
    elif age < IDLE_S:
        mode = "polling"  # seen recently but nothing to push to: finds work only when it next checks
    else:
        mode = "offline"
    return {"wake_mode": mode, "streams": streams, "agent": r["name"], "id": agent_id, "state": state, "status": r.get("status_text") or "",
            "last_heartbeat": r.get("last_heartbeat"), "live_connections": live, "channels": chans,
            "wakeable": wakeable, "ready": state in ("online", "idle") or wakeable, "client": r.get("client"),
            "seen": seen, "last_seen": r.get("agent_seen") or None}


def board(s: Store) -> list[dict]:
    rows = s.q("SELECT id FROM agents WHERE name NOT IN ('cli') ORDER BY name")
    out = [presence(s, r["id"]) | {"unread": Messaging(s).unread(r["id"])} for r in rows]
    order = {"online": 0, "idle": 1, "sleeping": 2, "offline": 3}
    return sorted(out, key=lambda p: (order[p["state"]], p["agent"]))


SUMMON_SKIP = ("cli", "user", "librarian", "commons")


def summon(s: Store, sender: str | None, agents: list[str] | None = None, reason: str = "", wait: float = 10,
           project: str | None = None) -> dict:
    """Wake agents now and report who is ready. Each target gets a mailbox task (so a `commons agent daemon` or a
    live stream fires within a second, and chat agents see it on their next msg_recv) plus its wake channels; then
    we wait up to `wait` seconds for the rest to come online."""
    me = sender or ""
    if agents:
        ids = [Messaging(s)._aid(a) for a in agents]
    else:
        ids = [r["id"] for r in s.q("SELECT id, name FROM agents WHERE id<>? ORDER BY name", (me,))
               if r["name"] not in SUMMON_SKIP and not r["name"].startswith(("provider:", "cf:"))]
    t0 = now()
    live0 = {aid: presence(s, aid)["live_connections"] for aid in ids}  # listening right now = receives it instantly
    text = reason or "You are summoned: check in with pickup() and msg_recv(), then claim your work."
    for aid in ids:
        Messaging(s).send(sender, aid, text, mtype="task", subject="summon", project=project, meta={"summon": True})
    deadline = time.time() + max(0.0, min(wait, 60))
    while True:
        out = []
        for aid in ids:
            p = presence(s, aid)
            fresh = (p["last_heartbeat"] or 0) >= t0
            ready_now = live0[aid] > 0 or p["live_connections"] > 0 or fresh
            how = ("daemon/stream (instant)" if live0[aid] or p["live_connections"] else
                   f"wake channel: {', '.join(p['channels'])}" if p["channels"] else "none")
            row = {"agent": p["agent"], "ready": ready_now, "state": p["state"], "wake": how}
            if not ready_now and how == "none":
                row["fix"] = (f"no way to wake {p['agent']} while idle: run `commons agent daemon --agent {p['agent']} "
                              "--cmd '<its CLI>'` on its machine, or add a wake channel (commons wake add); chat-only "
                              "AIs see the task the next time they call msg_recv")
            out.append(row)
        if all(r["ready"] for r in out) or time.time() >= deadline:
            break
        time.sleep(0.5)
    return {"summoned": len(out), "ready": sum(r["ready"] for r in out), "agents": out,
            "seconds": round(now() - t0, 1)}


def ready(s: Store, agent_id: str) -> bool:
    return presence(s, agent_id)["ready"]


def ensure_ready(s: Store, agent_id: str, *, reason: str = "work waiting", timeout: float = 60,
                 ref: str = "", poll: float = 1.0) -> dict:
    """Wake-on-demand: if the agent isn't online, wake it via its channels and wait up to `timeout` s for a heartbeat.
    Returns {"state", "woken", "came_online"}; callers fall back to the escalation ladder when it stays away."""
    p = presence(s, agent_id)
    if p["state"] == "online":
        return {"state": "online", "woken": False, "came_online": True}
    before = p["last_heartbeat"] or 0
    ev = emit(s, agent_id, "task", ref=ref, preview=reason)
    deadline = time.time() + timeout
    while time.time() < deadline:
        q = presence(s, agent_id)
        if (q["last_heartbeat"] or 0) > before and q["state"] == "online":
            return {"state": "online", "woken": True, "came_online": True, "event": ev}
        time.sleep(poll)
    return {"state": presence(s, agent_id)["state"], "woken": True, "came_online": False, "event": ev,
            "fallback": "escalation ladder (reassign -> distributor model -> notify user)"}


# ------------------------------------------------------------------ wake channels
class Channels:
    def __init__(self, s: Store):
        self.s = s

    def add(self, agent: str, kind: str, config: dict, *, local_owner: bool = False) -> dict:
        if kind not in CHANNEL_KINDS:
            raise CommonsError(f"kind must be one of {', '.join(CHANNEL_KINDS)}")
        aid = Messaging(self.s)._aid(agent)
        cfg = dict(config or {})
        secret = None
        if kind == "webhook":
            check_url(cfg.get("url", ""))
            secret = cfg.pop("secret", None) or _secrets.token_urlsafe(32)
        elif kind == "command":
            if not local_owner:
                raise CommonsError("command hooks run programs on the Commons host: only the local owner (CLI) may add them")
            if not cfg.get("cmd"):
                raise CommonsError("command channel needs cmd")
        elif kind in ("slack", "discord"):
            if not (cfg.get("url") or cfg.get("url_ref")):
                raise CommonsError(f"{kind} needs url or url_ref (secret reference)")
            if cfg.get("url"):
                check_url(cfg["url"])
        elif kind == "ntfy":
            cfg.setdefault("server", "https://ntfy.sh")
            if not cfg.get("topic"):
                raise CommonsError("ntfy needs topic")
            check_url(cfg["server"].rstrip("/") + "/" + cfg["topic"])
        elif kind == "pushover":
            if not (cfg.get("user_ref") and cfg.get("token_ref")):
                raise CommonsError("pushover needs user_ref and token_ref (secret references)")
        elif kind == "telegram":
            if not (cfg.get("bot_token_ref") and cfg.get("chat_id")):
                raise CommonsError("telegram needs bot_token_ref and chat_id")
        elif kind == "email":
            if not (cfg.get("to") and cfg.get("host")):
                raise CommonsError("email needs to and host (plus user/password_ref for auth)")
            if not local_owner:  # an agent must not aim SMTP at 127.0.0.1:6379 or the metadata service
                check_url(f"https://{cfg['host']}:{int(cfg.get('port', 587))}/")
        for k, v in cfg.items():  # secrets must be references, never values
            if k.endswith("_ref") and not str(v).startswith(("env://", "vault://", "vault-dyn://", "op://", "infisical://")):
                raise CommonsError(f"{k} must be a secret reference (env://, vault://, op://, infisical://)")
        cid = new_id("wch")
        self.s.x("INSERT INTO wake_channels(id,agent_id,kind,config,secret,enabled,created_at,failures) VALUES(?,?,?,?,?,1,?,0)",
                 (cid, aid, kind, json.dumps(cfg), secret, now()))
        self.s.audit(None, "wake.channel.add", aid, {"kind": kind})
        out = {"id": cid, "agent": Messaging(self.s)._name(aid), "kind": kind, "config": cfg}
        return out | ({"secret": secret, "note": "shown once: verify X-Commons-Signature with it"} if secret else {})

    def list(self, agent: str | None = None) -> list[dict]:
        sql, a = "SELECT * FROM wake_channels", ()
        if agent:
            sql += " WHERE agent_id=?"
            a = (Messaging(self.s)._aid(agent),)
        return [{k: r[k] for k in ("id", "kind", "enabled", "last_ok", "last_error", "failures")}
                | {"agent": Messaging(self.s)._name(r["agent_id"]), "config": _safe_cfg(json.loads(r["config"] or "{}"))}
                for r in self.s.q(sql, a)]

    def remove(self, cid: str) -> dict:
        self.s.x("DELETE FROM wake_channels WHERE id=?", (cid,))
        return {"removed": cid}

    def set_enabled(self, cid: str, on: bool) -> dict:
        self.s.x("UPDATE wake_channels SET enabled=?, failures=0 WHERE id=?", (1 if on else 0, cid))
        return {"id": cid, "enabled": on}


def _safe_cfg(cfg: dict) -> dict:
    out = dict(cfg)
    for k in ("url",):
        if k in out:
            u = urllib.parse.urlparse(out[k])
            out[k] = f"{u.scheme}://{u.hostname}{(':' + str(u.port)) if u.port else ''}{u.path[:24]}…"
    return out


# ------------------------------------------------------------------ emit + dispatch
def emit(s: Store, agent_id: str, event: str, *, ref: str = "", preview: str = "", **meta) -> dict | None:
    """Record a wake event and fan it out. Never raises (wake must not break the write path)."""
    try:
        if not agent_id or agent_id == "*":
            return None
        per_min = int(os.environ.get("COMMONS_WAKE_PER_MIN", "30"))
        recent = s.q("SELECT COUNT(*) n FROM wake_events WHERE agent_id=? AND created_at>? AND status<>'coalesced'",
                     (agent_id, now() - 60))[0]["n"]
        name = Messaging(s)._name(agent_id)
        eid = new_id("wev")
        nonce = _secrets.token_hex(12)
        payload = {"event": event, "agent": name, "event_id": eid, "ref": ref,
                   "preview": redact(preview or "")[0][:140], "ts": round(time.time(), 3), "nonce": nonce,
                   "fetch": "/bus/recv?for=" + urllib.parse.quote(name or "") if event in FETCH_EVENTS else None}
        payload |= {k: v for k, v in meta.items() if k in ("sender", "mtype", "job", "subtask", "level") and v}
        status = "coalesced" if recent >= per_min else "pending"
        s.x("INSERT INTO wake_events(id,agent_id,event,ref,preview,payload,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (eid, agent_id, event, ref, payload["preview"], json.dumps(payload), status, now()))
        live = HUB.publish(agent_id, payload) + _mcp_notify(agent_id, payload)  # live streams always get it
        if status == "coalesced":
            return {"event_id": eid, "status": "coalesced", "live": live}
        p = presence(s, agent_id)
        skip_push = live and os.environ.get("COMMONS_WAKE_PUSH_WHEN_LIVE", "0") != "1"
        n = 0
        debounce = float(os.environ.get("COMMONS_WAKE_DEBOUNCE", "2"))
        debounced = 0
        for c in s.q("SELECT id, kind FROM wake_channels WHERE agent_id=? AND enabled=1", (agent_id,)):
            if skip_push:
                continue
            if debounce > 0 and s.q("SELECT 1 FROM wake_deliveries WHERE channel_id=? AND status IN ('pending','retry','sending') "
                                    "AND updated_at>?", (c["id"], now() - debounce)):
                debounced += 1  # a burst: the wake already queued for this channel covers this event too
                continue
            if c["kind"] == "command" and p["state"] == "online" and HUB.live(agent_id):
                continue  # already running and listening
            s.x("INSERT INTO wake_deliveries(id,event_id,channel_id,status,attempts,next_at,updated_at) VALUES(?,?,?,?,0,?,?)",
                (new_id("wdl"), eid, c["id"], "pending", 0, now()))
            n += 1
        if n:
            Dispatcher.kick(s)
        return {"event_id": eid, "status": "pending", "live": live, "queued": n} | ({"debounced": debounced} if debounced else {})
    except Exception as e:  # noqa: BLE001
        try:
            s.audit(None, "wake.emit.error", agent_id or "", str(e)[:200])
        except Exception:  # noqa: BLE001
            pass
        return None


def _resolve(s: Store, ref: str) -> str:
    from .secrets import Broker
    return Broker(s).resolve_internal(ref, purpose="wake channel")


def deliver(s: Store, chan: dict, payload: dict) -> None:
    """Send one wake to one channel. Raises on failure (dispatcher retries)."""
    cfg = json.loads(chan["config"] or "{}")
    kind = chan["kind"]
    body = json.dumps(payload, separators=(",", ":")).encode()
    title = f"Commons: {payload['event']} for {payload['agent']}"
    text = f"{title}\n{payload.get('preview') or ''}".strip()
    if kind == "webhook":
        ts = str(int(time.time()))
        code = http_post(cfg["url"], body, {"Content-Type": "application/json", "X-Commons-Event": payload["event"],
                                            "X-Commons-Delivery": payload["delivery_id"], "X-Commons-Timestamp": ts,
                                            "X-Commons-Signature": sign(chan["secret"], ts, body)})
    elif kind == "ntfy":
        h = {"Title": title, "Tags": "bell"}
        if cfg.get("token_ref"):
            h["Authorization"] = "Bearer " + _resolve(s, cfg["token_ref"])
        code = http_post(cfg["server"].rstrip("/") + "/" + cfg["topic"], (payload.get("preview") or payload["event"]).encode(), h)
    elif kind == "pushover":
        data = urllib.parse.urlencode({"token": _resolve(s, cfg["token_ref"]), "user": _resolve(s, cfg["user_ref"]),
                                       "title": title, "message": payload.get("preview") or payload["event"]}).encode()
        code = http_post(cfg.get("api", "https://api.pushover.net/1/messages.json"), data,
                         {"Content-Type": "application/x-www-form-urlencoded"})
    elif kind in ("slack", "discord"):
        url = cfg.get("url") or _resolve(s, cfg["url_ref"])
        msg = {"text": text} if kind == "slack" else {"content": text[:1900]}
        code = http_post(url, json.dumps(msg).encode(), {"Content-Type": "application/json"})
    elif kind == "telegram":
        api = cfg.get("api", "https://api.telegram.org")
        code = http_post(f"{api}/bot{_resolve(s, cfg['bot_token_ref'])}/sendMessage",
                         json.dumps({"chat_id": cfg["chat_id"], "text": text}).encode(), {"Content-Type": "application/json"})
    elif kind == "email":
        import smtplib
        from email.message import EmailMessage
        m = EmailMessage()
        m["Subject"], m["From"], m["To"] = title, cfg.get("from", "commons@localhost"), cfg["to"]
        m.set_content(text + "\n\nOpen the Commons dashboard or your agent to respond.")
        port = int(cfg.get("port", 587))
        with (smtplib.SMTP_SSL if port == 465 else smtplib.SMTP)(cfg["host"], port, timeout=15) as smtp:
            if port != 465 and cfg.get("starttls", True):
                try:
                    smtp.starttls()
                except smtplib.SMTPNotSupportedError:
                    if cfg.get("user"):
                        raise
            if cfg.get("user"):
                smtp.login(cfg["user"], _resolve(s, cfg["password_ref"]))
            smtp.send_message(m)
        code = 250
    elif kind == "command":
        cmd = cfg["cmd"] if isinstance(cfg["cmd"], list) else shlex.split(cfg["cmd"])
        env = dict(os.environ, COMMONS_WAKE_EVENT=payload["event"], COMMONS_WAKE_PAYLOAD=json.dumps(payload),
                   COMMONS_AGENT=payload["agent"] or "")
        stdin = cfg.get("prompt", "You were woken by Commons ({event}: {preview}). Check your Commons inbox "
                        "(recv / pickup), do the work, ack the messages, then report back.").format(**{
                            "event": payload["event"], "preview": payload.get("preview") or "", "ref": payload.get("ref") or ""})
        lock = f"wake_cmd_running:{chan['id']}"
        r = s.q("SELECT value FROM meta WHERE key=?", (lock,))
        if r and now() - float(r[0]["value"]) < float(cfg.get("min_interval", 30)):
            code = 200  # debounce: a wake process was just started; it will see this message too
        else:
            s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (lock, str(now())))
            log = open(os.path.expanduser(cfg.get("log", "~/.commons/wake-command.log")), "a")  # noqa: SIM115
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, env=env,
                                 cwd=os.path.expanduser(cfg.get("cwd", "~")), start_new_session=True, text=True)
            p.stdin.write(stdin)
            p.stdin.close()
            code = 200
    else:
        raise CommonsError(kind)
    if not (200 <= code < 300) and kind != "email" and code != 250:
        raise CommonsError(f"HTTP {code}")


class Dispatcher:
    """Delivers pending wake_deliveries with exponential backoff; dead-letters after len(BACKOFF) failures."""
    _ev = threading.Event()
    _thread: threading.Thread | None = None
    _lock = threading.Lock()

    @classmethod
    def kick(cls, s: Store) -> None:
        if os.environ.get("COMMONS_WAKE_SYNC") == "1":
            cls.run_due(s)
            return
        with cls._lock:
            if cls._thread is None or not cls._thread.is_alive():
                cls._thread = threading.Thread(target=cls._loop, args=(s,), daemon=True, name="commons-wake")
                cls._thread.start()
        cls._ev.set()

    @classmethod
    def _loop(cls, s: Store) -> None:
        while True:
            try:
                cls.run_due(s)
            except Exception:  # noqa: BLE001
                pass
            cls._ev.wait(1.0)
            cls._ev.clear()

    @staticmethod
    def run_due(s: Store, limit: int = 50) -> dict:
        done = failed = dead = 0
        rows = s.q("SELECT d.*, e.payload, c.kind, c.config, c.secret, c.agent_id, c.enabled FROM wake_deliveries d "
                   "JOIN wake_events e ON e.id=d.event_id JOIN wake_channels c ON c.id=d.channel_id "
                   "WHERE d.status IN ('pending','retry') AND d.next_at<=? ORDER BY d.next_at LIMIT ?", (now(), limit))
        for d in rows:
            # claim (multi-process safe)
            with s.lock:
                cur = s.db.execute("UPDATE wake_deliveries SET status='sending', updated_at=? WHERE id=? AND status IN ('pending','retry')",
                                   (now(), d["id"]))
                if cur.rowcount == 0:
                    continue
            payload = json.loads(d["payload"]) | {"delivery_id": d["id"]}
            try:
                if not d["enabled"]:
                    raise CommonsError("channel disabled")
                deliver(s, dict(d), payload)
                s.x("UPDATE wake_deliveries SET status='ok', attempts=attempts+1, updated_at=?, last_error=NULL WHERE id=?",
                    (now(), d["id"]))
                s.x("UPDATE wake_channels SET last_ok=?, failures=0 WHERE id=?", (now(), d["channel_id"]))
                done += 1
            except Exception as e:  # noqa: BLE001
                att = d["attempts"] + 1
                err = str(e)[:300]
                if att >= len(BACKOFF) + 1 or isinstance(e, CommonsError) and "not in COMMONS_WEBHOOK_ALLOWLIST" in err:
                    s.x("UPDATE wake_deliveries SET status='dead', attempts=?, last_error=?, updated_at=? WHERE id=?",
                        (att, err, now(), d["id"]))
                    s.x("UPDATE wake_channels SET last_error=?, failures=failures+1 WHERE id=?", (err, d["channel_id"]))
                    dead += 1
                    try:
                        from .escalation import notify
                        notify(s, f"Wake delivery dead-lettered ({d['kind']})",
                               f"agent {Messaging(s)._name(d['agent_id'])}: {err}", level="warn", target=d["id"])
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    delay = BACKOFF[min(att - 1, len(BACKOFF) - 1)] * float(os.environ.get("COMMONS_WAKE_BACKOFF_SCALE", "1"))
                    s.x("UPDATE wake_deliveries SET status='retry', attempts=?, next_at=?, last_error=?, updated_at=? WHERE id=?",
                        (att, now() + delay, err, now(), d["id"]))
                    s.x("UPDATE wake_channels SET last_error=?, failures=failures+1 WHERE id=?", (err, d["channel_id"]))
                    failed += 1
        return {"ok": done, "retry": failed, "dead": dead}

    @staticmethod
    def dead_letters(s: Store, limit: int = 50) -> list[dict]:
        return [dict(r) for r in s.q("SELECT d.id, d.channel_id, d.attempts, d.last_error, d.updated_at, e.event, e.agent_id "
                                     "FROM wake_deliveries d JOIN wake_events e ON e.id=d.event_id WHERE d.status='dead' "
                                     "ORDER BY d.updated_at DESC LIMIT ?", (limit,))]

    @staticmethod
    def redrive(s: Store, delivery_id: str | None = None) -> int:
        sql = "UPDATE wake_deliveries SET status='retry', attempts=0, next_at=? WHERE status='dead'"
        a: tuple = (now(),)
        if delivery_id:
            sql += " AND id=?"
            a += (delivery_id,)
        n = s.q("SELECT COUNT(*) n FROM wake_deliveries WHERE status='dead'" + (" AND id=?" if delivery_id else ""),
                a[1:])[0]["n"]
        s.x(sql, a)
        return n


# ------------------------------------------------------------------ v0.9.3 instant wake: replay + ack
def _ack_key(agent_id: str) -> str:
    return f"wake_ack:{agent_id}"


def wake_ack(s: Store, agent_id: str, event_id: str) -> dict:
    """Remember the newest wake event this agent has handled; streams replay everything after it on reconnect."""
    r = s.q("SELECT created_at FROM wake_events WHERE id=? AND agent_id=?", (event_id, agent_id))
    if not r:
        raise CommonsError("unknown wake event for this agent")
    cur = s.q("SELECT value FROM meta WHERE key=?", (_ack_key(agent_id),))
    if cur:
        old = s.q("SELECT created_at FROM wake_events WHERE id=?", (cur[0]["value"],))
        if old and old[0]["created_at"] > r[0]["created_at"]:
            return {"acked": cur[0]["value"], "moved": False}
    s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (_ack_key(agent_id), event_id))
    return {"acked": event_id, "moved": True}


def wake_events_since(s: Store, agent_id: str, since: str | None = None, limit: int = 200) -> list[dict]:
    """Events for this agent after `since` (an event id), or after its last ack when `since` is empty or 'ack'.
    No ack and no since: nothing (a fresh listener starts from now; unread mail is still in msg_recv)."""
    if not since or since == "ack":
        r = s.q("SELECT value FROM meta WHERE key=?", (_ack_key(agent_id),))
        since = r[0]["value"] if r else None
    if not since:
        return []
    r = s.q("SELECT created_at FROM wake_events WHERE id=? AND agent_id=?", (since, agent_id))
    if not r:
        return []
    rows = s.q("SELECT id, payload, created_at FROM wake_events WHERE agent_id=? AND created_at>=? AND id<>? "
               "ORDER BY created_at, id LIMIT ?", (agent_id, r[0]["created_at"], since, limit))
    out = []
    for x in rows:
        try:
            p = json.loads(x["payload"] or "{}")
        except ValueError:
            continue
        out.append(p | {"replay": True})
    return out
