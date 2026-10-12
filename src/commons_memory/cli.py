"""commons CLI: init, serve, index, search, export-graph, register-agent, secret-exec, stats."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import __version__

def run_masked(cmd: list[str], env: dict, secret: str, mask: str = "***") -> int:
    """Run cmd, streaming stdout/stderr with every occurrence of `secret` replaced by `mask`.

    Agents usually capture tool output into their transcript (and from there into memory), so
    an accidental `echo $TOKEN` must not leak the value. Output is processed line by line."""
    import threading
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sb = secret.encode()

    def pump(src, dst):
        for line in iter(src.readline, b""):
            if len(sb) >= 4:  # masking 1-3 char values would shred ordinary output
                line = line.replace(sb, mask.encode())
            dst.write(line)
            dst.flush()
        src.close()

    ts = [threading.Thread(target=pump, args=(proc.stdout, sys.stdout.buffer), daemon=True),
          threading.Thread(target=pump, args=(proc.stderr, sys.stderr.buffer), daemon=True)]
    for t in ts:
        t.start()
    rc = proc.wait()
    for t in ts:
        t.join()
    return rc


SNIPPET = {
    "claude_desktop (claude_desktop_config.json)": {"mcpServers": {"commons": {
        "command": "commons", "args": ["serve"], "env": {"COMMONS_AGENT": "claude-desktop", "COMMONS_DB": "{db}"}}}},
    "cursor (.cursor/mcp.json)": {"mcpServers": {"commons": {
        "command": "commons", "args": ["serve"], "env": {"COMMONS_AGENT": "cursor", "COMMONS_DB": "{db}"}}}},
    "claude code": "claude mcp add commons -e COMMONS_AGENT=claude-code -e COMMONS_DB={db} -- commons serve",
    "http clients (ChatGPT connectors, custom agents)": "commons serve --transport http --port 8765  ->  http://127.0.0.1:8765/mcp",
}


def main(argv: list[str] | None = None) -> int:
    from .store import CommonsError
    try:
        return _main(argv)
    except CommonsError as e:
        print(f"commons: error: {e}", file=sys.stderr)
        return 1


def _main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="commons", description="Unified cross-AI memory hub (MCP).")
    p.add_argument("--db", default=None, help="SQLite path or postgresql:// DSN (default $COMMONS_DB or ~/.commons/commons.db)")
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
    s.add_argument("--dashboard-host", default=None,
                   help="dashboard bind address (default: same as --host; needs a token when non-loopback)")
    s.add_argument("--tls-cert", help="HTTPS certificate (PEM) for --transport http and the dashboard")
    s.add_argument("--tls-key", help="HTTPS private key (PEM)")
    s.add_argument("--tls-self-signed", action="store_true", help="generate/use a self-signed cert in ~/.commons/tls")
    d = sub.add_parser("dashboard", help="local web dashboard")
    d.add_argument("--host", default="127.0.0.1")
    d.add_argument("--port", type=int, default=8766)
    d.add_argument("--token", default=None, help="require this token (or COMMONS_DASHBOARD_TOKEN)")
    d.add_argument("--tls-cert", help="serve HTTPS with this certificate (PEM)")
    d.add_argument("--tls-key", help="private key for --tls-cert")
    d.add_argument("--tls-self-signed", action="store_true", help="generate/use a self-signed cert in ~/.commons/tls")
    du = sub.add_parser("dashboard-user", help="per-user dashboard tokens: add NAME [--role viewer|admin] | list | remove NAME")
    du.add_argument("action", choices=["add", "list", "remove"])
    du.add_argument("name", nargs="?")
    du.add_argument("--role", default="viewer")
    acl = sub.add_parser("acl", help="per-project access control: grant PROJECT AGENT [--role] | revoke PROJECT AGENT | list")
    acl.add_argument("action", choices=["grant", "revoke", "list"])
    acl.add_argument("project", nargs="?")
    acl.add_argument("agent", nargs="?")
    acl.add_argument("--role", default="read", choices=["read", "write", "admin"])
    tr = sub.add_parser("trust", help="agent trust scores: list | set AGENT SCORE | unset AGENT")
    tr.add_argument("action", choices=["list", "set", "unset"])
    tr.add_argument("agent", nargs="?")
    tr.add_argument("score", nargs="?", type=float)
    pru = sub.add_parser("prune", help="retention: roll up old usage, GC old context packs, trim logs/caches")
    pru.add_argument("--usage-days", type=float)
    pru.add_argument("--context-days", type=float)
    pru.add_argument("--audit-days", type=float)
    pru.add_argument("--dry-run", action="store_true")
    dmp = sub.add_parser("dump", help="full-fidelity JSONL dump of every table (portable SQLite <-> Postgres)")
    dmp.add_argument("out")
    imp = sub.add_parser("import-memory", help="restore a `commons dump` (or an export-memory file); idempotent")
    imp.add_argument("path")
    mpg = sub.add_parser("migrate-postgres", help="copy a SQLite Commons DB into Postgres (+pgvector)")
    mpg.add_argument("dsn")
    mpg.add_argument("--from", dest="src", default=None, help="SQLite path (default: --db / COMMONS_DB)")
    ntf = sub.add_parser("notifications", help="escalations/alerts: list | read [ID]")
    ntf.add_argument("action", nargs="?", default="list", choices=["list", "read"])
    ntf.add_argument("id", nargs="?")
    sub.add_parser("escalate", help="run the stuck/unassignable-task escalation ladder now")
    a2p = sub.add_parser("a2a", help="A2A client: card URL | send URL TEXT [--wait] | get URL TASK | cancel URL TASK | stream URL TEXT")
    a2p.add_argument("action", choices=["card", "send", "get", "cancel", "stream"])
    a2p.add_argument("url")
    a2p.add_argument("arg", nargs="?")
    a2p.add_argument("--wait", action="store_true")
    a2p.add_argument("--bearer", default=os.environ.get("COMMONS_A2A_TOKEN"))
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
    sc.add_argument("--librarian-every", type=float, default=600, help="seconds between librarian passes (0=off)")
    sc.add_argument("--models-every", type=float, default=3600, help="seconds between model-list refresh/probe (0=off)")
    skp = sub.add_parser("skill", help="shared skill pool: list|get|publish|update|review|history|rollback|fork|rate")
    skp.add_argument("action", choices=["list", "get", "publish", "update", "review", "history", "rollback", "fork", "rate"])
    skp.add_argument("name", nargs="?")
    skp.add_argument("--file", help="SKILL.md path (publish/update)")
    skp.add_argument("--version", type=int)
    skp.add_argument("--reject", action="store_true")
    skp.add_argument("--new-name")
    skp.add_argument("--tags", default="")
    skp.add_argument("--success", action="store_true")
    skp.add_argument("--agent", default=os.environ.get("COMMONS_AGENT", "cli"))
    rsp = sub.add_parser("research", help="web research stored as cited memory")
    rsp.add_argument("query")
    rsp.add_argument("--provider")
    rsp.add_argument("--project")
    rsp.add_argument("-n", type=int, default=5)
    rsp.add_argument("--configure", metavar="PROVIDER", help="set provider (brave|tavily|exa|searxng|fetch) as default")
    rsp.add_argument("--key-ref")
    rsp.add_argument("--url")
    mp = sub.add_parser("models", help="providers / models / roles / routing")
    mp.add_argument("action", choices=["add-provider", "refresh", "list", "probe", "role", "routing", "proposals",
                                       "approve", "reject", "watch", "agent"])
    mp.add_argument("args", nargs="*")
    mp.add_argument("--key-ref")
    mp.add_argument("--base-url")
    mp.add_argument("--budget", type=float)
    tp = sub.add_parser("tree", help="org-chart outline with index paths")
    tp.add_argument("root", nargs="?", default="workspace")
    tp.add_argument("--depth", type=int, default=2)
    tp.add_argument("--max-tokens", type=int, default=2000)
    tp.add_argument("--find")
    tp.add_argument("--format", choices=["outline", "mermaid", "markdown"], default="outline")
    lb = sub.add_parser("librarian", help="run | status | proposals | approve ID | reject ID | undo LOGID | config")
    lb.add_argument("action", choices=["run", "status", "proposals", "approve", "reject", "undo", "config", "delegate"])
    lb.add_argument("target", nargs="?")
    lb.add_argument("--full", action="store_true")
    lb.add_argument("--auto-approve", choices=["on", "off"])
    lb.add_argument("--budget", type=float)
    sd = sub.add_parser("skills", help="skill discovery + local import: discover|trending|popular|add|updates|import|configure|status")
    sd.add_argument("action", choices=["discover", "trending", "popular", "add", "updates", "import", "configure", "status",
                                       "restore", "backups"])
    sd.add_argument("target", nargs="?")
    sd.add_argument("--name")
    sd.add_argument("--query", default="")
    sd.add_argument("--kind", default="")
    sd.add_argument("--token-ref")
    sd.add_argument("--allow-license", action="store_true")
    sd.add_argument("--path", action="append", default=[], help="extra path to scan (import)")
    sd.add_argument("--dir", default=".")
    sd.add_argument("--dry-run", action="store_true")
    sd.add_argument("--replace-local", action="store_true", help="replace imported local skills with the bootstrap pointer")
    sd.add_argument("--max-repos", type=int, default=60)
    sd.add_argument("--agent", default=os.environ.get("COMMONS_AGENT", "cli"))
    cxp = sub.add_parser("context", help="shared context packs: list | load PACK | diff PACK [OTHER] | fit PACK MODEL | savings")
    cxp.add_argument("action", choices=["list", "load", "diff", "fit", "savings"])
    cxp.add_argument("pack", nargs="?")
    cxp.add_argument("other", nargs="?")
    cxp.add_argument("--layer", default="L1")
    cxp.add_argument("--max-tokens", type=int, default=4000)
    mh = sub.add_parser("mcp", help="MCP hub: catalog|add|add-preset|import|list|health|tools|call|policy|approvals|approve|deny|calls|remove")
    mh.add_argument("action", choices=["catalog", "add", "add-preset", "import", "list", "health", "tools", "describe", "call",
                                       "policy", "approvals", "approve", "deny", "calls", "remove", "agent-creds", "oauth",
                                       "verify-catalog"])
    mh.add_argument("--require", action="store_true", help="agent-creds: deny agents without their own credentials")
    mh.add_argument("--token-url")
    mh.add_argument("--client-id-ref")
    mh.add_argument("--client-secret-ref")
    mh.add_argument("--refresh-token-ref")
    mh.add_argument("--scope", default="")
    mh.add_argument("target", nargs="?")
    mh.add_argument("rest", nargs="*")
    mh.add_argument("--url")
    mh.add_argument("--command", dest="mcp_command")
    mh.add_argument("--arg", action="append", default=[], help="stdio arg (repeatable)")
    mh.add_argument("--env", action="append", default=[], help="ENV=secret-ref (repeatable)")
    mh.add_argument("--header", action="append", default=[], help="Header='Bearer {env://X}' (repeatable)")
    mh.add_argument("--ref", action="append", default=[], help="preset SECRET=ref (repeatable)")
    mh.add_argument("--param", action="append", default=[], help="preset param k=v (repeatable)")
    mh.add_argument("--name")
    mh.add_argument("--args-json", default="{}")
    mh.add_argument("--agent", default=os.environ.get("COMMONS_AGENT", "cli"))
    mh.add_argument("--approval")
    mh.add_argument("--dry-run", action="store_true")
    mh.add_argument("--no-discover", action="store_true")
    fd = sub.add_parser("find", help="where to find X (librarian index)")
    fd.add_argument("query")
    fd.add_argument("-n", type=int, default=5)
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
    bk = sub.add_parser("backup", help="consistent online backup (rotated; encrypted with COMMONS_BACKUP_KEY)")
    bk.add_argument("out", nargs="?", help="file path (SQLite copy) or omit for a rotated backup in ~/.commons/backups")
    bk.add_argument("--dir")
    bk.add_argument("--keep", type=int)
    bk.add_argument("--encrypt", action="store_true")
    bk.add_argument("--decrypt", metavar="ENC_FILE", help="decrypt an encrypted backup to OUT")
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
    x.add_argument("--no-mask", action="store_true",
                   help="do not redact the secret value from the command's stdout/stderr")
    x.add_argument("command", nargs="*")
    sub.add_parser("stats")
    args = p.parse_args(argv)

    if args.cmd == "serve":
        from .server import run
        req = None
        if args.transport == "http":
            req = not args.no_auth and os.environ.get("COMMONS_REQUIRE_KEYS", "1") == "1"
        if args.tls_self_signed and not args.tls_cert:
            from .dashboard import self_signed_cert
            args.tls_cert, args.tls_key = self_signed_cert(host=args.host)
        if args.tls_cert:
            os.environ["COMMONS_TLS_CERT"], os.environ["COMMONS_TLS_KEY"] = args.tls_cert, args.tls_key or ""
        if args.dashboard:
            from .dashboard import serve_dashboard
            from .store import Store as _S
            dhost = args.dashboard_host or (args.host if args.transport == "http" and args.host else "127.0.0.1")
            serve_dashboard(_S(args.db), host=dhost, port=args.dashboard_port, background=True,
                            tls_cert=args.tls_cert, tls_key=args.tls_key)
            print(f"dashboard: {'https' if args.tls_cert else 'http'}://{dhost}:{args.dashboard_port}/", file=sys.stderr)
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
        if getattr(args, "no_mask", False):
            return subprocess.call(cmd, env={**os.environ, args.var: value})
        return run_masked(cmd, {**os.environ, args.var: value}, value)
    elif args.cmd == "dashboard":
        from .dashboard import self_signed_cert, serve_dashboard
        if args.tls_self_signed and not args.tls_cert:
            args.tls_cert, args.tls_key = self_signed_cert(host=args.host)
        serve_dashboard(store, args.host, args.port, args.token, tls_cert=args.tls_cert, tls_key=args.tls_key)
    elif args.cmd == "dashboard-user":
        from .dashboard import add_dashboard_user, dashboard_users
        if args.action == "add":
            print(json.dumps(add_dashboard_user(store, args.name, args.role), indent=2))
        elif args.action == "remove":
            store.x("DELETE FROM meta WHERE key=?", (f"dash_user:{args.name}",))
            print("removed")
        else:
            print(json.dumps([{k: u[k] for k in ("name", "role")} for u in dashboard_users(store)], indent=2))
    elif args.cmd == "acl":
        from .auth import ACL
        acl_ = ACL(store)
        r = acl_.grant(args.project, args.agent, args.role) if args.action == "grant" else \
            acl_.revoke(args.project, args.agent) if args.action == "revoke" else acl_.list(args.project)
        print(json.dumps(r, indent=2, default=str))
    elif args.cmd == "trust":
        from .trust import Trust
        t = Trust(store)
        r = t.table() if args.action == "list" else t.set_manual(args.agent, args.score if args.action == "set" else None)
        print(json.dumps(r, indent=2, default=str))
    elif args.cmd == "prune":
        from .retention import prune
        print(json.dumps(prune(store, args.usage_days, args.context_days, args.audit_days, args.dry_run), indent=2))
    elif args.cmd == "dump":
        from .retention import dump
        print(json.dumps(dump(store, args.out)))
    elif args.cmd == "import-memory":
        from .retention import load
        print(json.dumps(load(store, args.path), indent=2))
    elif args.cmd == "migrate-postgres":
        from .pgcompat import migrate_sqlite_to_pg
        src = args.src or args.db or os.environ.get("COMMONS_DB") or str(Path.home() / ".commons" / "commons.db")
        print(json.dumps(migrate_sqlite_to_pg(src, args.dsn), indent=2))
        print(f"done. Point clients at it with COMMONS_DB={args.dsn}", file=sys.stderr)
    elif args.cmd == "notifications":
        from .escalation import notifications
        if args.action == "read":
            store.x("UPDATE notifications SET read=1" + (" WHERE id=?" if args.id else ""), (args.id,) if args.id else ())
        print(json.dumps(notifications(store, args.action == "list"), indent=2, default=str))
    elif args.cmd == "escalate":
        from .escalation import Escalator
        print(json.dumps(Escalator(store).run(), indent=2))
    elif args.cmd == "a2a":
        from . import a2a
        h = {"Authorization": f"Bearer {args.bearer}"} if args.bearer else None
        if args.action == "card":
            r = a2a.fetch_agent_card(args.url)
        elif args.action == "send":
            r = a2a.send_message(args.url, args.arg, headers=h)
            tid = (r.get("result") or {}).get("id")
            if args.wait and tid:
                r = a2a.wait_task(args.url, tid, headers=h)
        elif args.action == "get":
            r = a2a.get_task(args.url, args.arg, headers=h)
        elif args.action == "cancel":
            r = a2a.cancel_task(args.url, args.arg, headers=h)
        else:
            for ev in a2a.stream_message(args.url, args.arg, headers=h):
                print(json.dumps(ev), flush=True)
            return 0
        print(json.dumps(r, indent=2))
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
        from .background import tick
        if args.librarian_every is not None:
            os.environ.setdefault("COMMONS_EVERY_LIBRARIAN", str(args.librarian_every))
        if args.models_every is not None:
            os.environ.setdefault("COMMONS_EVERY_MODELS", str(args.models_every))
        while True:
            tick(store, force=args.once, log=lambda r: print(json.dumps(r, default=str), flush=True))
            if args.once:
                break
            time.sleep(min(args.interval, 30))
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
        from .retention import backup, decrypt_file, _key
        if args.decrypt:
            if not _key():
                p.error("set COMMONS_BACKUP_KEY")
            decrypt_file(args.decrypt, args.out, _key())
            print(f"decrypted to {args.out}")
        elif args.out and store.dialect == "sqlite" and not args.encrypt:
            import sqlite3
            dst = sqlite3.connect(args.out)
            with store.lock:
                store.db.backup(dst)
            dst.close()
            print(f"backup written to {args.out}")
        else:
            print(json.dumps(backup(store, args.dir, args.keep, True if args.encrypt else None)))
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
    elif args.cmd == "skill":
        from .server_v4 import seed_skills
        from .skillpool import SkillPool
        seed_skills(store)
        sp, aid = SkillPool(store), store.ensure_agent(args.agent)
        a = args.action
        if a == "list":
            print(sp.list(args.name or "", None, 4000)["text"])
        elif a == "get":
            print(sp.get(args.name, args.version, 100000, True, aid)["content"])
        elif a in ("publish", "update"):
            body = open(args.file, encoding="utf-8").read()
            tags = [t for t in args.tags.split(",") if t]
            r = sp.publish(args.name, body, tags=tags, agent_id=aid) if a == "publish" else \
                sp.update(args.name, body, tags=tags or None, agent_id=aid)
            print(json.dumps(r, indent=2, default=str))
        elif a == "review":
            print(json.dumps(sp.review(args.name, args.version, not args.reject, aid), indent=2, default=str))
        elif a == "history":
            print(json.dumps(sp.history(args.name), indent=2, default=str))
        elif a == "rollback":
            print(json.dumps(sp.rollback(args.name, args.version, aid), indent=2, default=str))
        elif a == "fork":
            print(json.dumps(sp.fork(args.name, args.new_name, aid), indent=2, default=str))
        elif a == "rate":
            print(json.dumps(sp.rate(args.name, args.success, None, aid), indent=2, default=str))
    elif args.cmd == "research":
        from .research import Research
        rs = Research(store)
        if args.configure:
            print(json.dumps(rs.configure(args.configure, args.key_ref, args.url, default=True)))
        else:
            r = rs.research(args.query, provider=args.provider, max_results=args.n, project=args.project,
                            agent_id=store.ensure_agent(os.environ.get("COMMONS_AGENT", "cli")))
            print(r["summary"])
    elif args.cmd == "models":
        from .providers import ProviderLayer
        pl, a, xs = ProviderLayer(store), args.action, args.args
        r = {"add-provider": lambda: pl.add_provider(xs[0], xs[1], args.key_ref, args.base_url),
             "refresh": lambda: pl.refresh_models(xs[0] if xs else None),
             "list": lambda: pl.models(xs[0] if xs else None),
             "probe": lambda: pl.probe(xs or None, budget=args.budget),
             "role": lambda: pl.set_role(xs[0], xs[1] if len(xs) > 1 else None) if xs else pl.roles(),
             "routing": lambda: pl.refresh_routing() if xs[:1] == ["refresh"] else pl.routing(),
             "proposals": lambda: pl.proposals(),
             "approve": lambda: pl.decide(xs[0], True),
             "reject": lambda: pl.decide(xs[0], False),
             "watch": lambda: pl.watch(),
             "agent": lambda: pl.set_agent_models(store.ensure_agent(xs[0]), xs[1:])}[a]()
        print(json.dumps(r, indent=2, default=str))
    elif args.cmd == "tree":
        from .tree import Tree
        t = Tree(store)
        if args.find:
            for h in t.find(args.find, args.root):
                print(f"{h['path']}  {' > '.join(h['breadcrumbs'])}")
        elif args.format == "mermaid":
            print(t.to_mermaid(args.root, args.depth))
        elif args.format == "markdown":
            print(t.to_markdown(args.root, args.depth))
        else:
            print(t.outline(args.root, args.depth, args.max_tokens)["text"])
    elif args.cmd == "librarian":
        from .librarian import Librarian
        lib, a = Librarian(store), args.action
        r = {"run": lambda: lib.run(full=args.full), "status": lib.status, "proposals": lib.proposals,
             "approve": lambda: lib.decide(args.target, True), "reject": lambda: lib.decide(args.target, False),
             "undo": lambda: lib.undo(args.target), "delegate": lib.delegate,
             "config": lambda: lib.configure(None if args.auto_approve is None else args.auto_approve == "on",
                                             args.budget)}[a]()
        print(json.dumps(r, indent=2, default=str))
    elif args.cmd == "skills":
        from .skilldiscovery import Discovery
        d, a = Discovery(store), args.action
        aid = store.ensure_agent(args.agent)
        if a in ("trending", "popular"):
            for r in d.list(a, args.query, args.kind, 40):
                print(f"{r['score']:>8} ★{r['stars']:<6} +{r['delta_7d']:<4} {r['kind']:9} {r['license']:12} {r['id']}")
            return 0
        if a == "import":
            from .skillimport import run as imp
            r = imp(store, args.dir, args.path, args.dry_run, args.replace_local, aid)
        elif a in ("restore", "backups"):
            from .skillimport import backups, restore
            r = restore(args.target, store) if a == "restore" else backups()
        else:
            r = {"discover": lambda: d.discover(args.max_repos), "add": lambda: d.add(args.target, args.name, aid, args.allow_license),
                 "updates": lambda: d.check_updates(aid), "configure": lambda: d.configure(args.token_ref),
                 "status": d.status}[a]()
        print(json.dumps(r, indent=2, default=str))
    elif args.cmd == "context":
        from .contextpacks import ContextStore
        cs, a = ContextStore(store), args.action
        if a == "load":
            print(cs.load(args.pack, args.layer, args.max_tokens)["text"])
        else:
            r = {"list": cs.packs, "diff": lambda: cs.diff(args.pack, args.other), "savings": cs.savings,
                 "fit": lambda: {k: v for k, v in cs.fit(args.pack, args.other).items() if k != "load"}}[a]()
            print(json.dumps(r, indent=2, default=str))
    elif args.cmd == "mcp":
        from .mcphub import Hub, load_catalog
        hub, a, t = Hub(store), args.action, args.target
        kv = lambda xs: dict(x.split("=", 1) for x in xs)  # noqa: E731
        if a == "agent-creds":  # commons mcp agent-creds SERVER AGENT --env X=ref --header H=ref [--require]
            print(json.dumps(hub.set_agent_credentials(t, args.rest[0], kv(args.env), kv(args.header),
                                                       True if args.require else None), indent=2))
            return 0
        if a == "oauth":
            print(json.dumps(hub.set_oauth(t, args.token_url, args.client_id_ref, args.client_secret_ref,
                                           args.refresh_token_ref, args.scope), indent=2))
            return 0
        if a == "verify-catalog":
            print(json.dumps(hub.verify_catalog(t), indent=2))
            return 0
        if a == "catalog":
            for k, v in sorted(load_catalog()["presets"].items()):
                print(f"{k:16} {'✓' if v.get('verified') else '?'} {v['transport']:5} {v.get('auth', ''):16} {v['title']}")
            return 0
        r = {"add": lambda: hub.add(t, command=args.mcp_command, args=args.arg, url=args.url, env_refs=kv(args.env),
                                     header_refs=kv(args.header), discover=not args.no_discover),
             "add-preset": lambda: hub.add_preset(t, args.name, kv(args.param), kv(args.ref), not args.no_discover),
             "import": lambda: hub.import_configs(([t] if t else []) + args.rest or None, args.dry_run),
             "list": hub.servers, "health": lambda: hub.health(t),
             "tools": lambda: hub.search(t or "", None, 200, 100000)["text"],
             "describe": lambda: hub.describe(t),
             "call": lambda: hub.call(t, json.loads(args.args_json), store.ensure_agent(args.agent), args.approval),
             "policy": lambda: hub.set_policy(t, *(args.rest + ["*"] * 3)[:2], agent=(args.rest + ["*"] * 3)[2])
             if t else hub.policies(),
             "approvals": hub.approvals, "approve": lambda: hub.decide(t, True), "deny": lambda: hub.decide(t, False),
             "calls": hub.calls, "remove": lambda: hub.remove(t)}[a]()
        print(r if isinstance(r, str) else json.dumps(r, indent=2, default=str))
    elif args.cmd == "find":
        from .librarian import Librarian
        print(Librarian(store).find(args.query, args.n)["text"])
    elif args.cmd == "stats":
        print(json.dumps(store.stats(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
