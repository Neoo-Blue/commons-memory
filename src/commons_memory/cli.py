"""commons CLI: init, serve, index, search, export-graph, register-agent, secret-exec, stats."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from . import __version__

SNIPPET = {
    "claude_desktop (claude_desktop_config.json)": {"mcpServers": {"commons": {
        "command": "commons", "args": ["serve"], "env": {"COMMONS_AGENT": "claude-desktop", "COMMONS_DB": "{db}"}}}},
    "cursor (.cursor/mcp.json)": {"mcpServers": {"commons": {
        "command": "commons", "args": ["serve"], "env": {"COMMONS_AGENT": "cursor", "COMMONS_DB": "{db}"}}}},
    "claude code": "claude mcp add commons -e COMMONS_AGENT=claude-code -e COMMONS_DB={db} -- commons serve",
    "http clients (ChatGPT connectors, custom agents)": "commons serve --transport http --port 8765  ->  http://127.0.0.1:8765/mcp",
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="commons", description="Unified cross-AI memory hub (MCP).")
    p.add_argument("--db", default=None, help="SQLite path (default $COMMONS_DB or ~/.commons/commons.db)")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create the database and print client config snippets")
    s = sub.add_parser("serve", help="run the MCP server")
    s.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--no-auth", action="store_true", help="HTTP: allow writes without api keys (local dev only)")
    s.add_argument("--dashboard", action="store_true", help="also serve the web dashboard (default port 8766)")
    s.add_argument("--dashboard-port", type=int, default=8766)
    d = sub.add_parser("dashboard", help="local web dashboard")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=8766)
    d.add_argument("--token", default=None, help="require this token (or COMMONS_DASHBOARD_TOKEN)")
    sk = sub.add_parser("install-skill", help="install Commons skill/rules/config for an agent platform")
    sk.add_argument("platform", help="claude-code|claude-desktop|cursor|codex|agents-md|chatgpt|grok|muse|generic|all")
    sk.add_argument("--dir", default=".", help="project directory (default: cwd)")
    sk.add_argument("--scope", choices=["project", "user"], default="project")
    sk.add_argument("--agent")
    cn = sub.add_parser("connect", help="register this agent (api key + capability profile) and install its skill")
    cn.add_argument("agent")
    cn.add_argument("--platform", default="generic")
    cn.add_argument("--vendor", default="")
    cn.add_argument("--model", default="")
    cn.add_argument("--expertise", default="", help="e.g. code:0.9,review:0.7,research:0.5")
    cn.add_argument("--strengths", default="")
    cn.add_argument("--cost", type=float, default=3)
    cn.add_argument("--speed", type=float, default=3)
    cn.add_argument("--max-concurrent", type=int, default=2)
    cn.add_argument("--dir", default=".")
    cn.add_argument("--no-install", action="store_true")
    w = sub.add_parser("worker", help="poll for subtasks and execute them with a CLI agent")
    w.add_argument("--agent", required=True)
    w.add_argument("--cmd", dest="worker_cmd", help="shell command; prompt on stdin and in {prompt_file}")
    w.add_argument("--preset", help="claude|codex|cursor|gemini|echo")
    w.add_argument("--model", default=None, help="model name for cost estimates")
    w.add_argument("--url", help="remote hub MCP url (http://host:8765/mcp); default: local DB")
    w.add_argument("--api-key", default=os.environ.get("COMMONS_API_KEY"))
    w.add_argument("--once", action="store_true")
    w.add_argument("--max-tasks", type=int, default=0)
    w.add_argument("--interval", type=float, default=5)
    w.add_argument("--idle-exit", type=float, default=0, help="exit after N idle seconds")
    w.add_argument("--lease", type=int, default=900)
    w.add_argument("--timeout", type=int, default=3600)
    sc = sub.add_parser("scheduler", help="run due recurring tasks (loop, or --once from system cron)")
    sc.add_argument("--once", action="store_true")
    sc.add_argument("--interval", type=float, default=60)
    sb = sub.add_parser("submit", help="submit a task for multi-agent orchestration")
    sb.add_argument("task")
    sb.add_argument("--pattern", default="auto")
    sb.add_argument("--project")
    sb.add_argument("--n", type=int, default=3)
    sb.add_argument("--max-tokens", type=int)
    sb.add_argument("--max-cost", type=float)
    st = sub.add_parser("status", help="job status (or recent jobs)")
    st.add_argument("job_id", nargs="?")
    pk = sub.add_parser("pickup", help="what should an agent do next")
    pk.add_argument("--agent", required=True)
    pk.add_argument("--project")
    sch = sub.add_parser("schedule", help="define a recurring task")
    sch.add_argument("name")
    sch.add_argument("cron")
    sch.add_argument("task")
    sch.add_argument("--pattern", default="auto")
    sch.add_argument("--project")
    pr = sub.add_parser("prefs", help="view / add / confirm / forget learned user preferences")
    pr.add_argument("action", choices=["list", "add", "confirm", "forget", "forget-all", "export"])
    pr.add_argument("arg", nargs="?")
    pr.add_argument("--project")
    pa = sub.add_parser("pause", help="kill switch: pause all orchestration")
    pa.add_argument("--reason", default="manual kill switch")
    sub.add_parser("resume-all", help="resume orchestration after pause")
    bk = sub.add_parser("backup", help="consistent online backup of the database")
    bk.add_argument("out")
    ex = sub.add_parser("export-memory", help="export all memories/decisions/preferences as JSONL (portable)")
    ex.add_argument("out")
    gr = sub.add_parser("graph", help="graph maintenance and scoped views")
    gr.add_argument("action", choices=["rebuild", "view"])
    gr.add_argument("--scope", default="project", choices=["project", "job", "workflow", "schedule", "node"])
    gr.add_argument("--id", default="")
    gr.add_argument("--depth", type=int, default=2)
    gr.add_argument("--format", default="mermaid", choices=["mermaid", "graphml", "json", "html"])
    gr.add_argument("--out", "-o")
    kbp = sub.add_parser("kb", help="knowledge base: build | get | list | search | export")
    kbp.add_argument("action", choices=["build", "get", "list", "search", "export"])
    kbp.add_argument("arg", nargs="?", default="", help="project/bucket, search query, or export dir")
    kbp.add_argument("--section", default="")
    kbp.add_argument("--max-tokens", type=int, default=4000)
    us = sub.add_parser("usage", help="cost/usage summary")
    us.add_argument("--by", default="agent", choices=["agent", "project", "job", "model", "day"])
    i = sub.add_parser("index", help="index a code repo into the graph")
    i.add_argument("path")
    i.add_argument("--project")
    q = sub.add_parser("search", help="search memory")
    q.add_argument("query", nargs="?", default="")
    q.add_argument("--project")
    q.add_argument("--kind", action="append")
    q.add_argument("--tag", action="append")
    q.add_argument("--mode", default="hybrid")
    q.add_argument("--max-tokens", type=int, default=800)
    q.add_argument("--json", action="store_true")
    r = sub.add_parser("remember", help="store a memory from the command line")
    r.add_argument("content")
    r.add_argument("--project")
    r.add_argument("--tag", action="append")
    r.add_argument("--agent", default=os.environ.get("COMMONS_AGENT", "cli"))
    e = sub.add_parser("export-graph", help="export graph (mermaid|graphml|json|html)")
    e.add_argument("--format", default="html", choices=["mermaid", "graphml", "json", "html"])
    e.add_argument("--project")
    e.add_argument("--no-items", action="store_true", help="code/entities only")
    e.add_argument("--out", "-o")
    a = sub.add_parser("register-agent", help="register an agent and print its api key")
    a.add_argument("name")
    a.add_argument("--vendor", default="")
    a.add_argument("--model", default="")
    a.add_argument("--a2a-url", default="")
    x = sub.add_parser("secret-exec", help="run a command with a leased secret injected as an env var")
    x.add_argument("lease_id")
    x.add_argument("--as", dest="var", required=True)
    x.add_argument("command", nargs="*")
    sub.add_parser("stats")
    args = p.parse_args(argv)

    if args.cmd == "serve":
        from .server import run
        req = None
        if args.transport == "http":
            req = not args.no_auth and os.environ.get("COMMONS_REQUIRE_KEYS", "1") == "1"
        if args.dashboard:
            from .dashboard import serve_dashboard
            from .store import Store as _S
            serve_dashboard(_S(args.db), port=args.dashboard_port, background=True)
            print(f"dashboard: http://127.0.0.1:{args.dashboard_port}/", file=sys.stderr)
        run(args.transport, args.host, args.port, args.db, req)
        return 0
    if args.cmd == "install-skill":
        from .skills import PLATFORMS, install
        plats = [p for p in PLATFORMS if p not in ("agents-md", "generic")] if args.platform == "all" else [args.platform]
        for pl in plats:
            for line in install(pl, project_dir=args.dir, scope=args.scope, agent=args.agent, db=args.db):
                print(f"[{pl}] {line}")
        return 0
    if args.cmd == "worker":
        from .worker import PRESETS, LocalClient, McpClient, run_worker
        cmd = getattr(args, "worker_cmd", None) or PRESETS.get(args.preset or "", None)
        if not cmd:
            p.error("--cmd or --preset required")
        client = McpClient(args.url, args.api_key) if args.url else LocalClient(args.db, args.agent)
        if args.model and not args.url:
            orig = client.complete
            client.complete = lambda sid, res, **kw: orig(sid, res, **kw, model=args.model)
        n = run_worker(client, cmd, once=args.once, max_tasks=args.max_tasks, interval=args.interval,
                       idle_exit=args.idle_exit, lease=args.lease, timeout=args.timeout,
                       log=lambda m: print(m, file=sys.stderr, flush=True))
        print(json.dumps({"completed": n}))
        return 0

    from .store import Store
    store = Store(args.db)
    if args.cmd == "init":
        print(f"Commons database ready at {store.path} (embedder: {store.stats()['embedder']})\n")
        for name, snip in SNIPPET.items():
            print(f"# {name}")
            txt = json.dumps(snip, indent=2) if isinstance(snip, dict) else snip
            print(txt.replace("{db}", store.path) + "\n")
    elif args.cmd == "index":
        from .codeindex import index_repo
        print(json.dumps(index_repo(store, args.path, args.project, store.ensure_agent("cli")), indent=2))
    elif args.cmd == "search":
        res = store.search(args.query, mode=args.mode, project=args.project, kinds=args.kind, tags=args.tag,
                           max_tokens=args.max_tokens)
        print(json.dumps(res, indent=2) if args.json else res["text"])
    elif args.cmd == "remember":
        print(json.dumps(store.add_item("memory", args.content, project=args.project, tags=args.tag,
                                        agent_id=store.ensure_agent(args.agent))))
    elif args.cmd == "export-graph":
        from .graphexport import export
        out = export(store.export_graph(args.project, not args.no_items), args.format)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(out)
            print(f"wrote {args.out} ({len(out)} bytes)")
        else:
            sys.stdout.write(out)
    elif args.cmd == "register-agent":
        print(json.dumps(store.register_agent(args.name, args.vendor, args.model, a2a_url=args.a2a_url), indent=2))
    elif args.cmd == "secret-exec":
        from .secrets import Broker
        cmd = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not cmd:
            p.error("command required after --")
        value = Broker(store).resolve(args.lease_id)
        return subprocess.call(cmd, env={**os.environ, args.var: value})
    elif args.cmd == "dashboard":
        from .dashboard import serve_dashboard
        serve_dashboard(store, args.host, args.port, args.token)
    elif args.cmd == "connect":
        from .orchestrator import Profiles
        from .skills import install, save_key
        reg = store.register_agent(args.agent, args.vendor or args.platform, args.model,
                                   capabilities=[c for c in args.strengths.split(",") if c])
        exp = {k: float(v) for k, v in (x.split(":") for x in args.expertise.split(",") if ":" in x)}
        Profiles(store).set(reg["id"], expertise=exp or None, strengths=[c for c in args.strengths.split(",") if c],
                            cost=args.cost, speed=args.speed, availability="online",
                            max_concurrent=args.max_concurrent, platform=args.platform)
        path = save_key(args.agent, reg["api_key"])
        print(f"registered {args.agent} ({reg['id']}); api key saved to {path} (chmod 600)")
        if not args.no_install and args.platform in ("claude-code", "claude-desktop", "cursor", "codex", "chatgpt",
                                                     "grok", "muse", "generic"):
            for line in install(args.platform, project_dir=args.dir, agent=args.agent, key=reg["api_key"], db=args.db):
                print(f"[{args.platform}] {line}")
    elif args.cmd == "scheduler":
        import time
        from .orchestrator import Orchestrator
        o = Orchestrator(store)
        while True:
            for r in o.run_due():
                print(json.dumps(r), flush=True)
            o.reap()
            from .kb import KB
            rebuilt = KB(store).refresh_dirty()
            if rebuilt:
                print(json.dumps({"kb_rebuilt": rebuilt}), flush=True)
            if args.once:
                break
            time.sleep(args.interval)
    elif args.cmd == "submit":
        from .orchestrator import Orchestrator
        budget = {k: v for k, v in (("tokens", args.max_tokens), ("cost", args.max_cost)) if v}
        r = Orchestrator(store).submit(args.task, pattern=args.pattern, project=args.project, n=args.n,
                                       budget=budget, agent_id=store.ensure_agent(os.environ.get("COMMONS_AGENT", "cli")))
        print(r["text"])
    elif args.cmd == "status":
        from .orchestrator import Orchestrator
        o = Orchestrator(store)
        if args.job_id:
            st_ = o.status(args.job_id)
            print(st_["text"] + ("\n\n" + st_["result"] if st_.get("result") else ""))
        else:
            for j in o.jobs(limit=10):
                print(j["text"] + "\n")
    elif args.cmd == "pickup":
        from .orchestrator import Orchestrator
        print(Orchestrator(store).pickup(store.ensure_agent(args.agent), args.project)["text"])
    elif args.cmd == "schedule":
        from .orchestrator import Orchestrator
        print(json.dumps(Orchestrator(store).schedule(args.name, args.cron, args.task, pattern=args.pattern,
                                                      project=args.project)))
    elif args.cmd == "prefs":
        from .prefs import Preferences
        pf = Preferences(store)
        if args.action == "list":
            for x in pf.get(include_all=True, max_tokens=10**6)["preferences"]:
                print(f"{x['id']}  {x['confidence']:.2f}  [{x['scope']}]  {x['statement']}  ({x['source']})")
        elif args.action == "add":
            print(json.dumps(pf.observe(args.arg, project=args.project, source="user", confidence=0.95)))
        elif args.action == "confirm":
            print(json.dumps(pf.confirm(args.arg)))
        elif args.action == "forget":
            print(json.dumps(pf.forget(args.arg)))
        elif args.action == "forget-all":
            print(json.dumps(pf.forget(everything=True)))
        else:
            print(json.dumps(pf.get(include_all=True, max_tokens=10**6)["preferences"], indent=1))
    elif args.cmd == "pause":
        from .orchestrator import Orchestrator
        print(json.dumps(Orchestrator(store).set_paused(True, args.reason)))
    elif args.cmd == "resume-all":
        from .orchestrator import Orchestrator
        print(json.dumps(Orchestrator(store).set_paused(False)))
    elif args.cmd == "backup":
        import sqlite3
        dst = sqlite3.connect(args.out)
        with store.lock:
            store.db.backup(dst)
        dst.close()
        print(f"backup written to {args.out}")
    elif args.cmd == "export-memory":
        n = 0
        with open(args.out, "w", encoding="utf-8") as f:
            for r in store.q("SELECT id FROM items ORDER BY created_at"):
                f.write(json.dumps(store.get_item(r["id"]), default=str) + "\n"); n += 1
            for r in store.q("SELECT * FROM preferences"):
                f.write(json.dumps({"kind": "preference", **dict(r)}, default=str) + "\n"); n += 1
            for r in store.q("SELECT id,type,name,path,line FROM entities WHERE type NOT IN ('file','function','class','method','module')"):
                f.write(json.dumps({"kind": "entity", **dict(r)}) + "\n"); n += 1
        print(f"exported {n} records to {args.out}")
    elif args.cmd == "graph":
        from .graphexport import export
        from .graphlinks import rebuild, subgraph
        if args.action == "rebuild":
            print(json.dumps(rebuild(store)))
        else:
            out = export(subgraph(store, args.scope, args.id, args.depth), args.format)
            if args.out:
                open(args.out, "w", encoding="utf-8").write(out)
                print(f"wrote {args.out}")
            else:
                sys.stdout.write(out)
    elif args.cmd == "kb":
        from .kb import KB
        kb = KB(store)
        if args.action == "build":
            print(json.dumps(kb.build(args.arg) if args.arg else kb.build_all()))
        elif args.action == "get":
            print(kb.get(args.arg or "misc", args.section, args.max_tokens)["text"])
        elif args.action == "list":
            for b in kb.list():
                print(f"{b['bucket']:<30} built {b['built_at'] or '-':<22} {'DIRTY' if b['dirty'] else 'fresh':<6} "
                      f"{b['citations']} citations")
        elif args.action == "search":
            print(kb.search(args.arg)["text"])
        else:
            print(json.dumps(kb.export_markdown(args.arg or "commons-kb")))
    elif args.cmd == "usage":
        from .usage import Usage
        for r in Usage(store).summary(args.by):
            print(f"{r['label'][:30]:<30} in {r['tokens_in']:>9} out {r['tokens_out']:>9}  ${r['cost']:.4f}  {r['seconds']}s")
    elif args.cmd == "stats":
        print(json.dumps(store.stats(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
