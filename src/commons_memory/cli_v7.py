"""v0.7 CLI: msg, presence, wake, agent daemon, bus import, route, drive, sched/mission, breakdown."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import urllib.parse
import urllib.request

CMDS = {"project-rename", "msg", "presence", "wake", "agent", "bus", "route", "drive", "sched", "mission", "breakdown"}


def add_parsers(sub) -> None:
    me = os.environ.get("COMMONS_AGENT", "user")
    m = sub.add_parser("msg", help="agent messaging (send/recv/ack/history)")
    ms = m.add_subparsers(dest="sub", required=True)
    x = ms.add_parser("send")
    x.add_argument("to")
    x.add_argument("body")
    x.add_argument("--as", dest="sender", default=me)
    x.add_argument("--type", default="message", choices=["message", "task", "question", "review", "fyi"])
    x.add_argument("--subject", default="")
    x.add_argument("--thread", type=int)
    x.add_argument("--project")
    x = ms.add_parser("recv")
    x.add_argument("--as", dest="agent", default=me)
    x.add_argument("--wait", type=float, default=0)
    x.add_argument("--ack", action="store_true")
    x = ms.add_parser("ack")
    x.add_argument("ids", nargs="+")
    x.add_argument("--as", dest="agent", default=me)
    x = ms.add_parser("history")
    x.add_argument("--agent")
    x.add_argument("--peer")
    x.add_argument("--thread", type=int)
    x.add_argument("--limit", type=int, default=50)

    x = sub.add_parser("project-rename", help="rename a project (e.g. fix an invalid name like 'commons-live?')")
    x.add_argument("old")
    x.add_argument("new")
    sub.add_parser("presence", help="presence board: online/idle/sleeping/offline per agent")

    w = sub.add_parser("wake", help="wake channels per agent")
    ws = w.add_subparsers(dest="sub", required=True)
    x = ws.add_parser("add")
    x.add_argument("agent")
    x.add_argument("kind", help="webhook|ntfy|pushover|slack|discord|telegram|email|command")
    x.add_argument("config", nargs="*", help="key=value pairs, e.g. url=https://... cmd='claude -p' topic=x")
    x = ws.add_parser("list")
    x.add_argument("agent", nargs="?")
    x = ws.add_parser("remove")
    x.add_argument("id")
    x = ws.add_parser("test")
    x.add_argument("agent")
    ws.add_parser("dead")
    x = ws.add_parser("redrive")
    x.add_argument("id", nargs="?")

    a = sub.add_parser("agent", help="agent daemon (always-on listener that wakes a local CLI agent)")
    asub = a.add_subparsers(dest="sub", required=True)
    x = asub.add_parser("daemon")
    x.add_argument("--agent", required=True)
    x.add_argument("--cmd", default="", help="command to spawn on wake (prompt on stdin), e.g. 'claude -p'")
    x.add_argument("--url", help="remote Commons base URL (http://host:8770); default local DB")
    x.add_argument("--api-key", default=os.environ.get("COMMONS_API_KEY"))
    x.add_argument("--wait", type=float, default=25)
    x.add_argument("--once", action="store_true")
    x.add_argument("--timeout", type=int, default=3600)
    x.add_argument("--print-unit", choices=["systemd", "launchd", "windows"], help="print a service unit and exit")
    asub.add_parser("list", help="registered agents with presence and tool profile")
    x = asub.add_parser("tools", help="show or set the tools an agent's client lists (core | all | core,name,...)")
    x.add_argument("agent")
    x.add_argument("profile", nargs="?", help="omit to show; '-' to clear (falls back to COMMONS_TOOLS, then core)")

    b = sub.add_parser("bus", help="agent-bus migration")
    bs = b.add_subparsers(dest="sub", required=True)
    x = bs.add_parser("import", help="pull message history from an agent-bus deployment (AGENT_BUS_URL/AGENT_BUS_TOKEN)")
    x.add_argument("--url", default=os.environ.get("AGENT_BUS_URL"))
    x.add_argument("--token-env", default="AGENT_BUS_TOKEN")
    x.add_argument("--agent", default=os.environ.get("AGENT_BUS_AGENT", ""), help="mailbox to read (?for=)")
    x.add_argument("--limit", type=int, default=1000)

    r = sub.add_parser("route", help="routing: execution targets (agent + model + effort)")
    rs = r.add_subparsers(dest="sub", required=True)
    rs.add_parser("policy")
    x = rs.add_parser("set")
    x.add_argument("task_type")
    x.add_argument("--effort")
    x.add_argument("--tier")
    x = rs.add_parser("mode")
    x.add_argument("mode", choices=["quality-first", "balanced", "cost-first"])
    x = rs.add_parser("why")
    x.add_argument("ref")
    x = rs.add_parser("decisions")
    x.add_argument("--limit", type=int, default=30)

    d = sub.add_parser("drive", help="Commons Drive: shared versioned file store")
    ds = d.add_subparsers(dest="sub", required=True)
    for n in ("ls", "cat", "put", "get", "rm", "mv", "history", "restore", "search", "mount", "sync-git", "sync-rclone",
              "usage", "gc"):
        x = ds.add_parser(n)
        x.add_argument("--project", default="")
        if n == "ls":
            x.add_argument("path", nargs="?", default="/")
        if n in ("cat", "get", "rm", "history", "restore"):
            x.add_argument("path")
        if n in ("cat", "get", "restore"):
            x.add_argument("--version", type=int)
        if n == "put":
            x.add_argument("local")
            x.add_argument("path")
            x.add_argument("--message", default="")
        if n == "get":
            x.add_argument("out")
        if n == "mv":
            x.add_argument("src")
            x.add_argument("dst")
        if n == "search":
            x.add_argument("query")
        if n == "mount":
            x.add_argument("name")
            x.add_argument("root")
            x.add_argument("--rw", action="store_true")
        if n == "sync-git":
            x.add_argument("repo")
            x.add_argument("--direction", choices=["pull", "push"], default="pull")
            x.add_argument("--prefix", default="/")
            x.add_argument("--subdir", default="")
        if n == "sync-rclone":
            x.add_argument("remote")
            x.add_argument("--direction", choices=["pull", "push"], default="push")
            x.add_argument("--prefix", default="/")

    s = sub.add_parser("sched", help="multi-project scheduling")
    ss = s.add_subparsers(dest="sub", required=True)
    x = ss.add_parser("project")
    x.add_argument("project")
    x.add_argument("--priority", type=int)
    x.add_argument("--weight", type=float)
    x.add_argument("--max-concurrent", type=int)
    x.add_argument("--budget", type=float)
    x.add_argument("--pause", action="store_true")
    x.add_argument("--unpause", action="store_true")
    x = ss.add_parser("job")
    x.add_argument("job_id")
    x.add_argument("--priority", type=int)
    x.add_argument("--after", nargs="*")
    x = ss.add_parser("preempt")
    x.add_argument("--below", type=int, default=1)
    x.add_argument("--requeue", action="store_true")
    x = ss.add_parser("resume")
    x.add_argument("job_id", nargs="?")
    sub.add_parser("mission", help="mission control: all projects/jobs/agents at once")
    x = sub.add_parser("breakdown", help="task breakdown of a job (plan, DAG, targets, timeline)")
    x.add_argument("job_id")
    x.add_argument("--json", action="store_true")


def _out(o):
    print(json.dumps(o, indent=2, default=str))


def _kv(pairs):
    return dict(p.split("=", 1) for p in pairs if "=" in p)


def daemon(args, store=None, log=lambda m: print(m, file=sys.stderr, flush=True)) -> int:
    """Long-poll the mailbox; on messages spawn the agent CLI with a prompt listing them; ack on success."""
    remote = bool(args.url)
    if not remote:
        from .messaging import Messaging, heartbeat
        msg = Messaging(store)
        aid = msg._aid(args.agent)
    hdr = {"Authorization": f"Bearer {args.api_key}"} if remote else {}

    def recv():
        if remote:
            q = urllib.parse.urlencode({"wait": args.wait, "lease": max(args.timeout, 60)})
            req = urllib.request.Request(f"{args.url.rstrip('/')}/agent/recv?{q}", headers=hdr)
            with urllib.request.urlopen(req, timeout=args.wait + 15) as r:  # noqa: S310
                return json.loads(r.read())["messages"]
        heartbeat(store, aid, status="online", client="daemon")
        return msg.recv(aid, wait=args.wait, lease=max(args.timeout, 60))

    def ack(ids):
        if remote:
            req = urllib.request.Request(f"{args.url.rstrip('/')}/agent/ack", data=json.dumps({"ids": ids}).encode(),
                                         method="POST", headers=hdr | {"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15).read()  # noqa: S310
        else:
            msg.ack(aid, ids)

    while True:
        try:
            msgs = recv()
        except Exception as e:  # noqa: BLE001
            log(f"recv failed: {e}; retrying")
            if args.once:
                return 1
            time.sleep(5)
            continue
        if msgs:
            lines = [f"- #{m['id']} [{m.get('type', 'message')}] from {m.get('from')}: "
                     f"{(m.get('text') or m.get('body') or '')[:500]}" for m in msgs]
            prompt = (f"You are agent '{args.agent}' and were woken by Commons. New messages:\n" + "\n".join(lines) +
                      "\n\nHandle them: reply with msg_send; for task/handoff messages use my_inbox / claim_subtask on the "
                      "Commons MCP server. Be concise.")
            ok = True
            if args.cmd:
                log(f"waking {args.agent}: {len(msgs)} message(s) -> {args.cmd}")
                try:
                    r = subprocess.run(shlex.split(args.cmd), input=prompt, text=True, capture_output=True, timeout=args.timeout)
                    ok = r.returncode == 0
                    log((r.stdout or r.stderr or "")[-2000:])
                except (OSError, subprocess.TimeoutExpired) as e:
                    log(f"agent command failed: {e}")
                    ok = False
            else:
                print(prompt, flush=True)
            if ok:
                ack([m["id"] for m in msgs])
        if args.once:
            return 0


def bus_import(store, url, token, agent="", limit=1000) -> dict:
    import datetime as dt

    from .messaging import Messaging
    if not url:
        raise SystemExit("set --url or AGENT_BUS_URL")
    msg = Messaging(store)
    got = new = 0
    before = None
    while got < limit:
        q = {"limit": min(200, limit - got)} | ({"for": agent} if agent else {}) | ({"before": before} if before else {})
        req = urllib.request.Request(f"{url.rstrip('/')}/bus/history?{urllib.parse.urlencode(q)}",
                                     headers={"X-Bridge-Token": token or "", "Authorization": f"Bearer {token or ''}"})
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
            data = json.loads(r.read())
        rows = data.get("messages", []) if isinstance(data, dict) else data
        if not rows:
            break
        for x in rows:
            got += 1
            ext = f"bus:{x.get('id')}"
            if store.q("SELECT 1 FROM messages WHERE ext_id=?", (ext,)):
                continue
            ts = x.get("ts") or x.get("created_at") or x.get("time")
            if isinstance(ts, str):
                try:
                    ts = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    ts = None
            msg.send(msg._aid(x.get("from") or x.get("sender") or "unknown"), x.get("to") or x.get("recipient") or "*",
                     x.get("text") or x.get("body") or x.get("message") or "", mtype=x.get("type") or "message",
                     ext_id=ext, created_at=ts, wake=False, meta={"imported_from": url})
            new += 1
        ids = [int(x["id"]) for x in rows if str(x.get("id", "")).isdigit()]
        if not ids or len(rows) < q["limit"]:
            break
        before = min(ids)
    return {"fetched": got, "imported": new}


def handle(args, store) -> int:  # noqa: C901
    c = args.cmd
    if c == "project-rename":
        from .store import PROJECT_NAME, CommonsError
        new = args.new.strip()
        if not PROJECT_NAME.fullmatch(new):
            raise CommonsError(f"invalid project name {new!r}")
        r = store.q("SELECT id FROM projects WHERE name=? OR id=?", (args.old, args.old))
        if not r:
            raise CommonsError(f"no project {args.old!r}")
        if store.q("SELECT 1 FROM projects WHERE name=?", (new,)):
            raise CommonsError(f"{new!r} exists; merge by re-filing items instead")
        store.x("UPDATE projects SET name=? WHERE id=?", (new, r[0]["id"]))
        store.x("UPDATE entities SET name=? WHERE id=?", (new, r[0]["id"]))
        print(f"renamed {args.old!r} -> {new!r} (KB bucket follows on next rebuild)")
        return 0
    if c == "msg":
        from .messaging import Messaging
        m = Messaging(store)
        if args.sub == "send":
            _out(m.send(m._aid(args.sender), args.to, args.body, mtype=args.type, subject=args.subject,
                        thread=args.thread, project=args.project))
        elif args.sub == "recv":
            _out(m.recv(args.agent, wait=args.wait, ack=args.ack))
        elif args.sub == "ack":
            _out(m.ack(args.agent, [int(i) for i in args.ids]))
        else:
            _out(m.history(args.agent, peer=args.peer, thread=args.thread, limit=args.limit))
    elif c == "presence":
        from .messaging import board
        for a in board(store):
            print(f"{a.get('agent', '?'):<20} {a.get('state', '?'):<9} seen {_ago(a.get('last_seen') or a.get('last_heartbeat')):<10} "
                  f"{'wakeable' if a.get('wakeable') else '-':<9} channels {len(a.get('channels') or [])}  {a.get('status') or ''}")
    elif c == "wake":
        from .messaging import Channels, Dispatcher, emit
        ch = Channels(store)
        if args.sub == "add":
            _out(ch.add(args.agent, args.kind, _kv(args.config), local_owner=True))
        elif args.sub == "list":
            _out(ch.list(args.agent))
        elif args.sub == "remove":
            _out(ch.remove(args.id))
        elif args.sub == "test":
            a = store.get_agent(args.agent)
            _out(emit(store, a["id"] if a else args.agent, "test", preview="test wake from `commons wake test`"))
            _out(Dispatcher.run_due(store))
        elif args.sub == "dead":
            _out(Dispatcher.dead_letters(store))
        else:
            _out({"redriven": Dispatcher.redrive(store, args.id)})
    elif c == "agent" and args.sub in ("list", "tools"):
        from .toolprofile import _agent_profile, set_agent_profile
        if args.sub == "tools":
            if args.profile:
                _out(set_agent_profile(store, args.agent, "" if args.profile == "-" else args.profile))
            else:
                a = store.get_agent(args.agent)
                _out({"agent": args.agent, "tools": _agent_profile(store, a["id"]) if a else None,
                      "default": os.environ.get("COMMONS_TOOLS") or "core"})
            return 0
        from .messaging import board
        pres = {a.get("agent"): a for a in board(store)}
        for a in store.list_agents():
            p = pres.get(a["name"], {})
            print(f"{a['name']:<20} {p.get('state', '?'):<9} seen {_ago(a.get('last_seen')):<10} "
                  f"tools {_agent_profile(store, a['id']) or '(default)':<12} {a.get('vendor') or ''} {a.get('model') or ''}")
    elif c == "agent":
        if args.print_unit:
            from .expose import service_unit
            unit = service_unit(args.print_unit, db=store.path)
            daemon_args = f"agent daemon --agent {args.agent}" + (f" --cmd {shlex.quote(args.cmd)}" if args.cmd else "") + \
                (f" --url {args.url}" if args.url else "")
            unit = unit.replace("serve --transport http --host 127.0.0.1 --port 8765", daemon_args)
            unit = unit.replace("Commons memory hub (MCP over HTTP)", f"Commons agent daemon for {args.agent}")
            print(unit.replace("commons.service", f"commons-agent-{args.agent}.service"))
            return 0
        return daemon(args, store)
    elif c == "bus":
        _out(bus_import(store, args.url, os.environ.get(args.token_env, ""), args.agent, args.limit))
    elif c == "route":
        from .routing import Router
        r = Router(store)
        if args.sub == "policy":
            _out({"mode": r.mode(), "policy": r.policy(), "tiers": r.tiers()})
        elif args.sub == "set":
            _out(r.set_policy(args.task_type, effort=args.effort, tier=args.tier))
        elif args.sub == "mode":
            print(r.set_mode(args.mode))
        elif args.sub == "why":
            _out(r.why(args.ref))
        else:
            _out(r.decisions(args.limit))
    elif c == "drive":
        from .drive import Drive
        d = Drive(store)
        p = args.project or None
        me = store.ensure_agent(os.environ.get("COMMONS_AGENT", "user"))
        sc = args.sub
        if sc == "ls":
            r = d.list(args.path, p)
            for f in r.get("files", []):
                print(f"{f['path']:<50} v{f.get('version', '-')} {f.get('size') or '':>8}  {f.get('updated_by') or ''}")
            for mnt in r.get("mounts", []):
                print(f"{mnt}/  (host mount)")
        elif sc == "cat":
            sys.stdout.write(d.read_bytes(args.path, p, args.version)[0].decode(errors="replace"))
        elif sc == "get":
            with open(args.out, "wb") as f:
                f.write(d.read_bytes(args.path, p, args.version)[0])
        elif sc == "put":
            with open(args.local, "rb") as f:
                _out(d.write(args.path, f.read(), project=p, agent_id=me, message=args.message))
        elif sc == "rm":
            _out(d.delete(args.path, p, me))
        elif sc == "mv":
            _out(d.move(args.src, args.dst, p, me))
        elif sc == "history":
            _out(d.history(args.path, p))
        elif sc == "restore":
            _out(d.restore(args.path, args.version, p, me))
        elif sc == "search":
            print(d.search(args.query, p)["text"] or "no matches")
        elif sc == "mount":
            _out(d.mount_host(args.name, args.root, read_only=not args.rw, project=p))
        elif sc == "sync-git":
            _out(d.sync_git(args.repo, direction=args.direction, prefix=args.prefix, project=p, subdir=args.subdir, agent_id=me))
        elif sc == "sync-rclone":
            _out(d.sync_rclone(args.remote, direction=args.direction, prefix=args.prefix, project=p, agent_id=me))
        elif sc == "usage":
            _out(d.usage())
        else:
            print(f"removed {d.gc()} unreferenced blobs")
    elif c == "sched":
        from .scheduler import Scheduler
        s = Scheduler(store)
        if args.sub == "project":
            _out(s.configure(args.project, priority=args.priority, weight=args.weight, max_concurrent=args.max_concurrent,
                             budget_cost=args.budget, paused=True if args.pause else (False if args.unpause else None)))
        elif args.sub == "job":
            _out(s.set_job(args.job_id, priority=args.priority, after=args.after))
        elif args.sub == "preempt":
            _out(s.preempt(args.below, args.requeue))
        else:
            _out(s.resume(args.job_id))
    elif c == "mission":
        from .scheduler import Scheduler
        mc = Scheduler(store).mission_control()
        t = mc["totals"]
        print(f"{t['running_jobs']} running jobs · {t['inflight']} subtasks in flight · {t['done_last_hour']} done in the last hour\n")
        for p in mc["projects"]:
            print(f"[{p['project'] or 'no project'}] prio {p['priority']} · in flight {p['inflight']}"
                  f"{'/' + str(p['max_concurrent']) if p['max_concurrent'] else ''} · queued {p['queued']} · "
                  f"{p['throughput_per_h']}/h · ${p['cost']}" + (" · PAUSED" if p["paused"] else ""))
            for j in p["jobs"]:
                print(f"   {j['id']} {j['status']:<9} p{j['priority'] or 0} {int(j['progress'] * 100):>3}%  {(j['title'] or '')[:60]}")
        for a in mc["agents"]:
            print(f"{a.get('agent', '?'):<18} {a.get('state', '?'):<8} load {a['load']}/{a['capacity']}  "
                  + ", ".join((w["title"] or "")[:30] for w in a["working_on"]))
    elif c == "breakdown":
        from .breakdown import breakdown
        b = breakdown(store, args.job_id)
        if args.json:
            _out(b)
            return 0
        j = b["job"]
        print(f"{j['id']} {j['status']} · {j['title']}\n{b['planning']['summary']} · total ${b['totals']['cost']} · "
              f"{b['totals']['tokens']} tokens\n")
        for n in b["nodes"]:
            deps = [e["from"] for e in b["edges"] if e["to"] == n["key"]]
            print(f"{n['key']:<14} {n['type']:<10} {n['status']:<9} {n['agent'] or '-':<12} {n['model'] or '-'} "
                  f"[{n['effort'] or '-'}]  att {n['attempts']}  ${n['cost']}" + (f"  after {','.join(deps)}" if deps else ""))
            if n["rationale"]:
                print(f"{'':<14} why: {n['rationale'][:150]}")
    return 0


def _ago(ts) -> str:
    """'3m ago' / 'never' for an epoch timestamp (None or 0 means never seen)."""
    import datetime as dt
    import time
    try:
        ts = float(ts or 0)
    except (TypeError, ValueError):
        try:
            ts = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
        except ValueError:
            return "?"
    if ts <= 0:
        return "never"
    d = max(0, time.time() - ts)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= n:
            return f"{int(d // n)}{unit} ago"
    return "just now"
