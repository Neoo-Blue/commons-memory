"""v0.8 CLI: hooks/capture, reindex, bench, diagrams, facts, sweep, spaces."""
from __future__ import annotations

import json
import os
import sys

NOSTORE = {"hook", "hooks", "bench"}
CMDS = {"capture", "observations", "reindex", "diagram", "fact", "sweep", "space", "audit"} | NOSTORE


def add_parsers(sub) -> None:
    x = sub.add_parser("hook", help="(called by agent harness hooks) handle one lifecycle event from stdin; never blocks")
    x.add_argument("harness", choices=["claude-code", "codex", "gemini", "cursor", "generic"])
    x.add_argument("event")
    x = sub.add_parser("hooks", help="install/uninstall lifecycle hooks for an agent harness")
    x.add_argument("action", choices=["install", "uninstall", "show"])
    x.add_argument("harness", choices=["claude-code", "codex", "gemini", "cursor", "all"])
    x.add_argument("--path", help="config file to edit (default: the harness's user-level file)")
    x.add_argument("--command", default="commons", help="how hooks invoke commons (e.g. full path to the venv binary)")
    x = sub.add_parser("capture", help="per-project capture settings (auto-capture via hooks)")
    x.add_argument("project")
    for f in ("enabled", "tools", "prompts", "inject"):
        x.add_argument(f"--{f}", choices=["on", "off"])
    x = sub.add_parser("observations", help="latest captured observations (memory stream)")
    x.add_argument("--project")
    x.add_argument("--limit", type=int, default=30)
    x = sub.add_parser("reindex", help="compute vectors for the active embedder (after changing COMMONS_EMBEDDER/model)")
    x.add_argument("--force", action="store_true", help="re-embed everything")
    x.add_argument("--limit", type=int)
    x = sub.add_parser("bench", help="retrieval benchmarks (LongMemEval, LoCoMo)")
    x.add_argument("suite", choices=["longmemeval", "locomo"])
    x.add_argument("--data", required=True, help="dataset json (longmemeval_s_cleaned.json / locomo10.json)")
    x.add_argument("--out", default="bench-results")
    x.add_argument("--embedder", default="auto")
    x.add_argument("--granularity", default="turn", choices=["turn", "session"])
    x.add_argument("--limit", type=int)
    x.add_argument("--workers", type=int, default=4)
    x.add_argument("--rerank", action="store_true")
    x = sub.add_parser("diagram", help="interactive HTML diagram (code graph or job plan)")
    x.add_argument("kind", choices=["code", "job"])
    x.add_argument("target", nargs="?", default="", help="project (code) or job id (job)")
    x.add_argument("-o", "--out", default="")
    x = sub.add_parser("fact", help="temporal facts: set / invalidate / history / as-of")
    fs = x.add_subparsers(dest="sub", required=True)
    y = fs.add_parser("set")
    y.add_argument("subject"); y.add_argument("predicate"); y.add_argument("value")
    y.add_argument("--valid-from"); y.add_argument("--project")
    y = fs.add_parser("invalidate")
    y.add_argument("subject"); y.add_argument("predicate"); y.add_argument("--at")
    y = fs.add_parser("history")
    y.add_argument("subject"); y.add_argument("predicate", nargs="?")
    y = fs.add_parser("get")
    y.add_argument("subject"); y.add_argument("predicate", nargs="?"); y.add_argument("--as-of")
    x = sub.add_parser("sweep", help="verbatim store: keep one item per message from history files (dedup by hash)")
    x.add_argument("paths", nargs="+")
    x.add_argument("--project")
    x.add_argument("--source", default="auto")
    x.add_argument("--dry-run", action="store_true")
    x = sub.add_parser("space", help="scoped spaces (project / topic / person)")
    x.add_argument("action", choices=["list", "search"])
    x.add_argument("scope", nargs="?", default="")
    x.add_argument("query", nargs="?", default="")
    sub.add_parser("audit", help="memory organisation audit (librarian score)").add_argument("--project")


def handle_nostore(args) -> int:
    c = args.cmd
    if c == "hook":
        from .capture import run_hook
        try:
            out = run_hook(args.harness, args.event, sys.stdin.read() if not sys.stdin.isatty() else "{}",
                           store_factory=lambda: __import__("commons_memory.store", fromlist=["Store"]).Store(args.db))
        except Exception as e:  # noqa: BLE001 - a hook must never break the agent
            print(f"commons hook: {e}", file=sys.stderr)
            out = ""
        if out:
            print(out)
        return 0
    if c == "hooks":
        from .capture import HOOK_TARGETS, install_hooks
        targets = list(HOOK_TARGETS) if args.harness == "all" else [args.harness]
        for h in targets:
            if args.action == "show":
                p = os.path.expanduser(args.path or HOOK_TARGETS[h][0])
                print(f"{h}: {p} events={HOOK_TARGETS[h][1]}")
                continue
            r = install_hooks(h, command=args.command, path=args.path, uninstall=args.action == "uninstall")
            print(f"{h}: {args.action}ed hooks for {', '.join(r['events'])} in {r['path']}")
        if args.action == "install":
            print("Hooks post to $COMMONS_URL with $COMMONS_API_KEY (or ~/.commons/agents/$COMMONS_AGENT.key); "
                  "without COMMONS_URL they write to the local DB. Wrap text in <private>...</private> to exclude it.")
        return 0
    if c == "bench":
        from . import bench
        if args.suite == "longmemeval":
            s = bench.longmemeval(args.data, args.out, embedder=args.embedder, granularity=args.granularity,
                                  limit=args.limit, workers=args.workers, rerank=args.rerank)
        else:
            s = bench.locomo(args.data, args.out, embedder=args.embedder, limit=args.limit, workers=args.workers,
                             rerank=args.rerank)
        print(json.dumps(s, indent=2))
        return 0
    return 2


def handle(args, store) -> int:
    c = args.cmd
    if c == "capture":
        from .capture import Capture
        kw = {f: (getattr(args, f) == "on") if getattr(args, f) else None for f in ("enabled", "tools", "prompts", "inject")}
        print(json.dumps(Capture(store).configure(args.project, **kw)))
    elif c == "observations":
        from .capture import Capture
        from .store import iso
        for o in Capture(store).stream(args.project, limit=args.limit):
            print(f"{iso(o['ts'])[:19]} {o['agent'] or '-':<12} {o['kind']:<14} {o['text'][:140]}")
    elif c == "reindex":
        r = store.reindex(limit=args.limit, force=args.force)
        print(json.dumps(r))
    elif c == "diagram":
        from .diagrams import code_diagram, job_diagram
        html = code_diagram(store, args.target or None) if args.kind == "code" else job_diagram(store, args.target)
        out = args.out or f"commons-{args.kind}-{(args.target or 'all').replace('/', '_')}.html"
        open(out, "w").write(html)
        print(out)
    elif c == "fact":
        from .facts import Facts
        f = Facts(store)
        if args.sub == "set":
            print(json.dumps(f.set(args.subject, args.predicate, args.value, valid_from=args.valid_from, project=args.project)))
        elif args.sub == "invalidate":
            print(json.dumps(f.invalidate(args.subject, args.predicate, at=args.at)))
        elif args.sub == "history":
            for r in f.history(args.subject, args.predicate):
                print(f"{r['valid_from'] or '-':<20} -> {r['valid_to'] or 'now':<20} {r['predicate']} = {r['value']}")
        else:
            print(json.dumps(f.get(args.subject, args.predicate, as_of=args.as_of), indent=2))
    elif c == "sweep":
        from .sweep import sweep
        print(json.dumps(sweep(store, args.paths, project=args.project, source=args.source, dry_run=args.dry_run), indent=2))
    elif c == "space":
        from .spaces import Spaces
        sp = Spaces(store)
        if args.action == "list":
            for r in sp.list(args.scope or None):
                print(f"{r['type']:<8} {r['name']:<32} {r['items']:>6} items")
        else:
            print(sp.search(args.scope, args.query)["text"])
    elif c == "audit":
        from .spaces import audit
        print(json.dumps(audit(store, args.project), indent=2))
    return 0
