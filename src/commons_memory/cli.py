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
        run(args.transport, args.host, args.port, args.db, req)
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
    elif args.cmd == "stats":
        print(json.dumps(store.stats(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
