"""v0.8 CLI: hooks/capture, reindex, bench, diagrams, facts, sweep, spaces."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

NOSTORE = {"hook", "hooks", "bench", "import", "quota", "instructions", "setup", "doctor", "audit-verify", "audit-key", "infra"}
CMDS = {"capture", "observations", "reindex", "diagram", "fact", "sweep", "space", "audit", "vault", "audit-export",
        "audit-chain", "graphiti", "approvals"} | NOSTORE


def add_parsers(sub) -> None:
    from .setup import LABELS
    dr = sub.add_parser("doctor", help="check this AI's whole Commons connection; --repair fixes what is broken")
    dr.add_argument("--client", required=True, help="claude-code, codex, gemini, cursor, opencode, copilot, cline, kilo, amp, factory, qwen, crush, goose, windsurf, zed, roo, warp")
    dr.add_argument("--agent", required=True)
    dr.add_argument("--url", required=True, help="the hub, e.g. http://192.168.50.111:8770")
    dr.add_argument("--repair", action="store_true")
    st = sub.add_parser("setup", help="connect an AI to Commons completely in one go (MCP, instructions, hooks, "
                                      "instant wake, model and version sync, memory hand-over, verify); "
                                      "every step on unless --no-<step>")
    st.add_argument("--client", required=True, help="claude-code, codex, gemini, cursor, opencode, copilot, cline, kilo, amp, factory, qwen, crush, goose, windsurf, zed, roo, warp")
    st.add_argument("--agent", required=True, help="this AI's Commons agent name")
    st.add_argument("--url", required=True, help="the hub, e.g. http://192.168.50.111:8770")
    for k, v in LABELS.items():
        st.add_argument(f"--no-{k}", action="store_true", help=f"skip: {v}")
    i = sub.add_parser("instructions", help="write the Commons master instructions into each AI's always-loaded "
                                            "instructions (CLAUDE.md + skill, AGENTS.md, GEMINI.md, Cursor rules)")
    i.add_argument("action", choices=["install", "uninstall", "show"])
    i.add_argument("ai", nargs="?", default="all", help="claude-code | codex | gemini | antigravity | grok | cursor | opencode | all")
    i.add_argument("--url", default=os.environ.get("COMMONS_URL"), help="hub MCP url to mention (default $COMMONS_URL)")
    i.add_argument("--agent", help="agent name to mention (omit with 'all': each AI's MCP config identifies it)")
    v = sub.add_parser("vault", help="the credential vault: store keys (value on stdin, encrypted), list, remove, "
                                     "and see the decision agent's log")
    v.add_argument("action", choices=["set", "list", "rm", "decisions"])
    v.add_argument("name", nargs="?")
    v.add_argument("--service", help="service name shown to agents and the judge, e.g. plex")
    v.add_argument("--base-url", action="append", default=[], help="URL prefix the key may be sent to (repeatable)")
    v.add_argument("--auth", default="bearer", help="bearer | basic:USER | header:NAME | header:NAME:PREFIX | query:PARAM")
    v.add_argument("--extra-header", action="append", default=[], help="NAME:VALUE sent with every call (repeatable)")
    v.add_argument("--risk", default="normal", choices=["low", "normal", "high"],
                   help="high = every write goes to the user; low = reads need no judgment")
    v.add_argument("--description", default="")
    q = sub.add_parser("quota", help="agent usage windows: sync them into a hub, install the 10-minute job, or print them")
    q.add_argument("action", choices=["sync", "status", "install"])
    q.add_argument("--url", help="Commons MCP url (http://host:8770/mcp)")
    q.add_argument("--api-key-file", help="read the hub key from this file (default $COMMONS_API_KEY)")
    q.add_argument("--aiusage-db", help="aiusage database (default ~/.aiusage/usage.db)")
    q.add_argument("--map", help="JSON file mapping aiusage provider/window to Commons agents (default "
                                 "~/.commons/quota-map.json or the built-in map)")
    x = sub.add_parser("import", help="import memories from claude-mem, mem0, Claude Code memory files or sesame "
                                      "(idempotent; locally or into a remote hub with --url)")
    x.add_argument("source", choices=["claude-mem", "mem0", "claude-memory", "sesame", "all"])
    x.add_argument("--path", help="claude-mem db / memory root / sesame registry (defaults: the usual locations)")
    x.add_argument("--mem0-url", help="mem0 server (default $MEM0_SELFHOSTED_URL)")
    x.add_argument("--user-id", help="mem0 user_id filter")
    x.add_argument("--prompts", action="store_true", help="claude-mem: also import raw user prompts (noisy; off by default)")
    x.add_argument("--url", help="remote Commons MCP url (http://host:8770/mcp); default: the local database")
    x.add_argument("--api-key-file", help="key for --url (default ~/.commons/agents/<COMMONS_AGENT>.key or $COMMONS_API_KEY)")
    x.add_argument("--batch", type=int, default=300)
    x.add_argument("--dry-run", action="store_true", help="count and show a sample (with --curate: estimated cost), write nothing")
    x.add_argument("--curate", action="store_true", help="an AI manager reviews every item first: keep/drop/merge, "
                                                         "rewritten title+summary, project, tags, preferences")
    x.add_argument("--model", help="curator model as provider/model (default: the `curator` role)")
    x.add_argument("--budget", type=float, help="USD cap for the curator; stops cleanly, a re-run continues")
    x.add_argument("--auto-approve", type=float, default=0.6, help="import keeps at or above this confidence; "
                                                                     "the rest wait in the review queue")
    x.add_argument("--workers", type=int, default=4, help="parallel curator calls")
    x.add_argument("--limit", type=int, help="only the most recent N items of each source (try a sample first)")
    x.add_argument("--batch-api", action="store_true", help="curate through the Anthropic Message Batches API "
                                                            "(half price, results within ~1 h)")
    x.add_argument("--resume-batch", help="collect the results of an already submitted curator batch")
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
    x.add_argument("suite", choices=["longmemeval", "locomo", "self", "scale", "qa-locomo", "qa-longmemeval"],
                   help="self = known-item retrieval on YOUR store: a model writes questions from sampled memories")
    x.add_argument("--data", help="dataset json (longmemeval_s_cleaned.json / locomo10.json)")
    x.add_argument("--sample", type=int, default=100, help="self: memories to sample")
    x.add_argument("--neighbors", type=int, default=0, help="qa-*: also give the reader N turns around each hit (same "
                   "session), in time order")
    x.add_argument("--target", help="scale: an empty postgresql:// DSN to benchmark Postgres instead of temp SQLite")
    x.add_argument("--sizes", help="scale: comma list of store sizes (default 1000,10000,50000)")
    x.add_argument("--judge", help="qa-*: judge model (default: same as --model)")
    x.add_argument("--k", type=int, default=20, help="qa-*: memories retrieved per question (mem0's eval: 10 per speaker)")
    x.add_argument("--model", help="self: question-writing model provider/model (default: curator role)")
    x.add_argument("--out", default="bench-results")
    x.add_argument("--embedder", default="auto")
    x.add_argument("--granularity", default="turn", choices=["turn", "session"])
    x.add_argument("--limit", type=int)
    x.add_argument("--workers", type=int, default=4)
    x.add_argument("--rerank", action="store_true")
    x.add_argument("--batch-api", action="store_true", help="qa-*: reader and judge calls through Anthropic Message "
                   "Batches (half price; results usually within the hour)")
    x = sub.add_parser("diagram", help="interactive HTML diagram (code graph or job plan)")
    x.add_argument("kind", choices=["code", "job", "workflow", "sequence", "lifecycle", "delta"])
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
    x = sub.add_parser("audit-export", help="signed, hash-chained export of the audit log (verify with audit-verify)")
    x.add_argument("--out", default="-", help="file to write (default stdout)")
    x.add_argument("--since", help="ISO time or 7d / 12h")
    x.add_argument("--until")
    sub.add_parser("audit-chain", help="check the live audit log's hash chain (edits, deletions, reordering)")
    x = sub.add_parser("audit-verify", help="check a signed audit export: chain, signature, and optionally the key")
    x.add_argument("file")
    x.add_argument("--key", help="the fingerprint you expect (from `commons audit-key` on the hub)")
    sub.add_parser("audit-key", help="this hub's audit signing key fingerprint (pin it when verifying exports)")
    x = sub.add_parser("approvals", help="the owner password: set it here, reset a forgotten one, or show the setup code")
    x.add_argument("action", choices=["code", "set-pin", "reset", "status"])
    x = sub.add_parser("graphiti", help="mirror memories into a Graphiti knowledge graph (push) or import its facts (pull)")
    x.add_argument("direction", choices=["push", "pull"])
    x.add_argument("--url", help="Graphiti server (default: the graphiti_url setting)")
    x.add_argument("--group", default="commons")
    x.add_argument("--last-n", type=int, default=50, help="pull: facts from this many latest episodes")
    x.add_argument("--project", help="pull: file the facts under this project")
    inf = sub.add_parser("infra", help="read-only infrastructure discovery (no database)")
    inf_sub = inf.add_subparsers(dest="action", required=True)
    c = inf_sub.add_parser("collect", help="snapshot machines, guests, containers, endpoints, storage and mounts")
    c.add_argument("--proxmox", nargs="?", const="pve", default=None,
                   help="ssh alias of a Proxmox node (default with no source flags: pve, plus tailscale and cloudflared)")
    c.add_argument("--docker", action="append", default=None, metavar="ALIAS",
                   help="ssh alias of a plain docker host (repeatable; sudo -n only, never a password)")
    c.add_argument("--tailscale", action="store_true", help="local tailscale peers")
    c.add_argument("--cloudflared", action="store_true", help="local cloudflared tunnels")
    c.add_argument("--out", help="write JSON to this file (default stdout)")
    c.add_argument("--submit", action="store_true", help="send it to the hub ($COMMONS_URL + agent key) for the map")
    for c_ in (c, inf_sub.add_parser("install", help="collect and submit on a schedule from this machine (launchd/systemd)")):
        c_.add_argument("--url", default=os.environ.get("COMMONS_URL"), help="the hub (default $COMMONS_URL)")
        c_.add_argument("--api-key-file", help="agent key file (default $COMMONS_API_KEY or ~/.commons/agents/$COMMONS_AGENT.key)")
    ins = inf_sub.choices["install"]
    ins.add_argument("--every", type=float, default=6, help="hours between runs (default 6)")
    ins.add_argument("--proxmox", nargs="?", const="pve", default=None)
    ins.add_argument("--docker", action="append", default=None, metavar="ALIAS")
    ins.add_argument("--tailscale", action="store_true")
    ins.add_argument("--cloudflared", action="store_true")
    x = inf_sub.add_parser("show", help="the infrastructure map as an agent sees it (this database)")
    x.add_argument("query", nargs="?", default="")
    x = inf_sub.add_parser("extract", help="build the map from memories with a small model (spends credit)")
    x.add_argument("--dry-run", action="store_true", help="count notes and estimate the cost only")
    x.add_argument("--full", action="store_true", help="every note, not just the ones since the last run")
    x.add_argument("--model", help="provider/model (default: the librarian role, else the preset small model)")


def _import(args) -> int:
    from . import importers as I
    srcs = list(I.SOURCES) if args.source == "all" else [args.source]
    total = {}
    for src in srcs:
        kw = {"claude-mem": {"path": args.path, "prompts": args.prompts}, "mem0": {"url": args.mem0_url, "user_id": args.user_id},
              "claude-memory": {"root": args.path}, "sesame": {"path": args.path}}[src]
        try:
            items = I.collect(src, **kw)
        except Exception as e:  # noqa: BLE001 - one missing source must not stop `all`
            print(f"{src}: skipped ({e})", file=sys.stderr)
            continue
        if args.limit:
            items = sorted(items, key=lambda i: float(I._epoch(i.get("created_at")) or 0))[-args.limit:]
        cli = None
        if args.url:
            from .worker import McpClient
            key = os.environ.get("COMMONS_API_KEY") or ""
            kf = args.api_key_file
            if kf and os.path.exists(os.path.expanduser(kf)):
                key = open(os.path.expanduser(kf)).read().strip()
            cli = McpClient(args.url, key)

        def sink(xs, cli=cli):
            if cli is None:
                return I.import_items(local, xs)
            out: dict = {}
            for i in range(0, len(xs), args.batch):
                r = json.loads(cli._call("import_items", {"items": xs[i:i + args.batch]}))
                for k, v in r.items():
                    if isinstance(v, int):
                        out[k] = out.get(k, 0) + v
            return out

        def known_fn(srcs, cli=cli):
            if cli is None:
                return I.known(local, srcs) | I.queued_srcs(local, srcs)
            got: set = set()
            for i in range(0, len(srcs), 2000):
                r = json.loads(cli._call("import_items", {"items": [], "check": srcs[i:i + 2000]}))
                got |= set(r["known"]) | set(r.get("queued", []))  # queued items wait for a human, not the curator
            return got

        from .store import Store
        local = Store(args.db)
        if args.curate:
            from .curate import curate
            res = curate(local, items, model=args.model, budget=args.budget, auto_approve=args.auto_approve,
                         workers=args.workers, dry_run=args.dry_run, sink=sink, known_fn=known_fn,
                         batch_api=args.batch_api, resume_batch=args.resume_batch,
                         log=lambda m: print(f"{src}: {m}", file=sys.stderr, flush=True))
        elif args.dry_run:
            print(f"{src}: {len(items)} items, e.g. {json.dumps(items[0], default=str)[:300] if items else '-'}")
            continue
        else:
            res = {}
            for i in range(0, len(items), args.batch):
                for k, v in sink(items[i:i + args.batch]).items():
                    if isinstance(v, int):
                        res[k] = res.get(k, 0) + v
                if cli is not None:
                    print(f"{src}: {min(i + args.batch, len(items))}/{len(items)} sent", file=sys.stderr, flush=True)
        total[src] = res
        print(f"{src}: {json.dumps(res)}", flush=True)
    return 0


def handle_nostore(args) -> int:
    c = args.cmd
    if c == "audit-verify":
        from .auditexport import verify
        r = verify(Path(args.file).read_text(), args.key)
        print(json.dumps(r, indent=2))
        return 0 if r["ok"] else 1
    if c == "audit-key":
        from .auditexport import public_key
        print(json.dumps(public_key(), indent=2))
        return 0
    if c == "doctor":
        from .setup import doctor
        return doctor(args.client, args.agent, args.url, args.repair)
    if c == "setup":
        from .setup import STEPS, run
        return run(args.client, args.agent, args.url, {k for k in STEPS if getattr(args, f"no_{k}")})
    if c == "instructions":
        from . import instructions as I
        if args.action == "show":
            print(I.master(args.url, args.agent))
        else:
            for r in I.install(args.ai, args.url, args.agent, uninstall=args.action == "uninstall"):
                print(json.dumps(r))
        return 0
    if c == "quota":
        from . import quota as Q
        from .store import CommonsError
        if not args.url:
            print("quota needs --url (the hub to sync into / read from)", file=sys.stderr)
            return 2
        if args.action == "install":
            return _quota_install(args)
        key = _quota_key(args)
        if key is None:
            return 2
        from .worker import McpClient
        cli = McpClient(args.url, key)
        if args.action == "status":
            print(json.dumps(json.loads(cli._call("call", {"tool": "quota_status", "args": {}})), indent=2))
            return 0
        mapping = json.load(open(args.map)) if args.map else None
        mp = Path.home() / ".commons" / "quota-map.json"
        eff = mapping or (json.loads(mp.read_text()) if mp.exists() else Q.DEFAULT_MAP)
        try:
            wins_by = Q.from_aiusage(args.aiusage_db, mapping)
        except CommonsError:  # no aiusage database on this machine: still send models and the CLI
            wins_by = {}
        models_by, clis = Q.local_models(eff.get("_models")), Q.local_clis(eff, eff.get("_cli"))

        def old_hub(exc):
            text = str(exc).lower()
            return any(s in text for s in ("unexpected keyword", "unexpected argument", "validation error",
                                           "additional propert", "extra fields not permitted",
                                           "extra inputs are not permitted"))

        def report(payload):
            """(reply, full). An old hub that rejects models/cli is retried with windows only."""
            try:
                return json.loads(cli._call("call", {"tool": "quota_report", "args": payload}) or "{}"), True
            except Exception as first:  # noqa: BLE001
                slim = {k: v for k, v in payload.items() if k not in ("models", "cli")}
                if not (("models" in payload or "cli" in payload) and slim.get("windows") and old_hub(first)):
                    raise first
                return json.loads(cli._call("call", {"tool": "quota_report", "args": slim}) or "{}"), False
        sent = {}
        for agent in sorted(set(wins_by) | set(models_by) | set(clis)):
            try:
                # the hub must list this key's agent in COMMONS_QUOTA_REPORTERS to report for others
                a = {"windows": wins_by.get(agent, []), "for_agent": agent}
                if agent in models_by:
                    a["models"] = models_by[agent]
                info = clis.get(agent)
                if info:
                    a["cli"] = {k: v for k, v in info.items() if k != "update"}
                r, full = report(a)
                line = [w["window"] for w in a["windows"]]
                if full:
                    line += ([f"{len(a['models'])} models"] if "models" in a else []) \
                        + [f"NEW: {n}" for n in (r.get("models") or {}).get("new", [])] \
                        + ([f"v{info['version']}"] if info else [])
                else:  # the hub stored windows and rejected models and cli
                    line.append("windows only")
                if full and info and (r.get("cli") or {}).get("update"):  # the hub says update now: run the updater here
                    ok, out = Q.run_update(info["update"])
                    after = Q.local_clis(eff, eff.get("_cli")).get(agent) or info
                    b = {"windows": [], "for_agent": agent, "cli": {"version": after["version"], "result": out, "ok": ok}}
                    if agent in models_by:
                        b["models"] = Q.local_models(eff.get("_models")).get(agent) or models_by[agent]
                    try:
                        r2, _full2 = report(b)
                        line += [f"updated to v{after['version']}" if ok else "update failed"] + \
                            [f"NEW: {n}" for n in (r2.get("models") or {}).get("new", [])]
                    except Exception:  # noqa: BLE001 - the update already ran; keep the windows we stored
                        line += [f"updated to v{after['version']}" if ok else "update failed", "update result not stored"]
                sent[agent] = line
            except Exception as e:  # noqa: BLE001 - one refused agent must not stop the others
                msg = " ".join(str(e).split()) or e.__class__.__name__
                if key:
                    msg = msg.replace(key, "[key]")
                sent[agent] = [f"error: {msg[:180]}"]
        print(json.dumps({"synced": sent}))
        return 0
    if c == "import":
        return _import(args)
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
    if c == "infra":
        from .infra_collect import main as infra_main
        if args.action in ("show", "extract"):  # these read the map in this database
            from . import infra as I
            from .store import Store
            st = Store(args.db)
            if args.action == "show":
                print(I.describe(st, args.query))
                return 0
            r = I.extract(st, model=args.model, since=0.0 if args.full else None, dry_run=args.dry_run,
                          log=lambda m: print(m, file=sys.stderr, flush=True))
            print(json.dumps(r, indent=2))
            return 0
        # Rebuild flags so an explicit source does not also pick up the no-flag default.
        fwd = ["collect"]
        if args.proxmox:
            fwd += ["--proxmox", args.proxmox]
        for alias in args.docker or []:
            fwd += ["--docker", alias]
        if args.tailscale:
            fwd.append("--tailscale")
        if args.cloudflared:
            fwd.append("--cloudflared")
        if getattr(args, "out", None):
            fwd += ["--out", args.out]
        if getattr(args, "submit", False):
            fwd.append("--submit")
        if args.action == "install":
            if not args.url:
                print("infra install needs --url (the hub) or COMMONS_URL", file=sys.stderr)
                return 2
            keyfile = _install_keyfile(args)
            if not keyfile:
                return 2
            fwd = [f for f in fwd if f != "--out"] + ["--submit", "--url", args.url, "--api-key-file", keyfile]
            return _schedule("infra", "CommonsInfraCollect", "infrastructure discovery (read-only)", ["infra", *fwd],
                             max(30, int(args.every * 60)))
        if getattr(args, "url", None):
            fwd += ["--url", args.url]
        if getattr(args, "api_key_file", None):
            fwd += ["--api-key-file", args.api_key_file]
        return infra_main(fwd)
    if c == "bench":
        from . import bench
        if args.suite.startswith("qa-"):
            from .store import Store
            if not (args.data and args.model):
                print("qa-* needs --data and --model provider/model (configure the provider with commons models)", file=sys.stderr)
                return 2
            s = bench.qa(args.suite[3:], args.data, args.out, Store(args.db), args.model, judge=args.judge, k=args.k,
                         limit=args.limit, workers=args.workers, embedder=args.embedder, neighbors=args.neighbors,
                         batch=args.batch_api, log=lambda m: print(m, file=sys.stderr, flush=True))
            print(json.dumps(s, indent=2))
            return 0
        if args.suite == "scale":
            s = bench.scale(sizes=tuple(int(x) for x in (args.sizes or "1000,10000,50000").split(",")),
                            embedder=args.embedder, log=lambda m: print(m, file=sys.stderr, flush=True),
                            db=args.target)
            print(json.dumps(s, indent=2))
            return 0
        if args.suite == "self":
            from .store import Store
            s = bench.selfcheck(Store(args.db), sample=args.sample, model=args.model,
                                log=lambda m: print(m, file=sys.stderr, flush=True))
            print(json.dumps(s, indent=2, default=str))
            return 0
        if not args.data:
            print("--data is required for longmemeval/locomo", file=sys.stderr)
            return 2
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
    if c == "vault":
        from . import vault as V
        if args.action == "list":
            print(json.dumps(V.listing(store), indent=2))
        elif args.action == "decisions":
            print(json.dumps(V.decisions(store), indent=2))
        elif args.action == "rm":
            print(json.dumps(V.remove(store, args.name)))
        else:
            auth = V.parse_auth(args.auth, args.extra_header)
            print(json.dumps(V.add(store, args.name, sys.stdin.read(), args.service or args.name, args.base_url, auth,
                                   args.description, args.risk)))
        return 0
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
        from .diagrams import make
        html = make(store, args.kind, args.target or "")
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
    elif c == "approvals":
        from . import approvals as AP
        if args.action == "code":
            print(AP.setup_code(store) if not AP.has_pin(store) else "an owner password is already set: "
                  "`commons approvals reset` here forgets it")
        elif args.action == "set-pin":
            import getpass
            pin = getpass.getpass("New owner password (6+ characters): ")
            if pin != getpass.getpass("Again: "):
                print("the two passwords differ", file=sys.stderr)
                return 1
            print(json.dumps(AP.set_pin(store, pin, by="host")))
        elif args.action == "reset":
            out = AP.reset(store)
            print(f"Owner password forgotten. Open the dashboard within {out['claim_minutes']} minutes to choose a new one.")
        else:
            print(json.dumps({k: v for k, v in AP.info(store).items() if k != "from"}))
    elif c == "graphiti":
        from . import graphiti as G
        from . import settings as _st
        url = args.url or _st.get(store, "graphiti_url")
        if not url:
            print("give --url or set graphiti_url on the Setup page", file=sys.stderr)
            return 2
        r = G.push(store, url, args.group) if args.direction == "push" else \
            G.pull(store, url, args.group, args.last_n, args.project)
        print(json.dumps(r, indent=2))
    elif c == "audit-export":
        from .auditexport import export
        out = export(store, args.since, args.until)
        if args.out == "-":
            sys.stdout.write(out)
        else:
            Path(args.out).write_text(out)
            print(f"wrote {args.out} ({out.count(chr(10)) - 2} rows)", file=sys.stderr)
    elif c == "audit-chain":
        from .auditexport import verify_live
        r = verify_live(store)
        print(json.dumps(r, indent=2))
        return 0 if r["ok"] else 1

    return 0


def _commons_argv() -> list[str]:
    """Argv that re-invokes this same commons, so the installed job calls the one that installed it."""
    import shutil
    name = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
    if name in ("commons", "commons.exe", "commons-memory", "commons-memory.exe"):
        return [str(Path(sys.argv[0]).resolve())]
    if name in ("__main__.py", "cli.py"):
        return [sys.executable, "-m", "commons_memory"]
    found = shutil.which("commons")
    return [found] if found else [sys.executable, "-m", "commons_memory"]


def _quota_key(args) -> str | None:
    """Hub key from --api-key-file, else $COMMONS_API_KEY. None means the file was missing."""
    path = getattr(args, "api_key_file", None)
    if not path:
        return os.environ.get("COMMONS_API_KEY", "")
    p = Path(path).expanduser()
    if not p.is_file():
        print(f"no api key file at {p}", file=sys.stderr)
        return None
    return p.read_text().strip()


def _install_keyfile(args) -> str | None:
    """Absolute key file for the scheduled job. A launchd or systemd job does not see this shell's environment."""
    if args.api_key_file:
        keyfile = Path(args.api_key_file).expanduser()
        if not keyfile.is_file():
            print(f"no api key file at {keyfile}", file=sys.stderr)
            return None
        return str(keyfile.resolve())
    key = os.environ.get("COMMONS_API_KEY", "").strip()
    if not key:
        print("quota install needs --api-key-file or COMMONS_API_KEY "
              "(the scheduled job does not see this shell's environment)", file=sys.stderr)
        return None
    path = Path(os.environ.get("COMMONS_HOME") or Path.home() / ".commons") / "sync.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key + "\n")
    path.chmod(0o600)
    return str(path.resolve())


def _quota_install(args) -> int:
    """Install a job that runs quota sync every 10 minutes, and run it once now."""
    keyfile = _install_keyfile(args)
    if not keyfile:
        return 2
    return _schedule("sync", "CommonsQuotaSync", "quota sync (models, tool version, usage windows)",
                     ["quota", "sync", "--url", args.url, "--api-key-file", keyfile], 10)


def _schedule(name: str, win_task: str, what: str, cmd: list[str], minutes: int) -> int:
    """A user job that runs `commons <cmd>` now and then every `minutes`: launchd, a systemd timer, or schtasks."""
    import html
    import shlex
    from .cli_v7 import _install_unit
    argv = [*_commons_argv(), *cmd]
    log = str(Path.home() / ".commons" / "logs" / f"{name}.log")
    Path(log).parent.mkdir(parents=True, exist_ok=True)
    every = f"every {minutes // 60} hours" if minutes >= 120 and minutes % 60 == 0 else f"every {minutes} minutes"
    if sys.platform == "darwin":
        # RunAtLoad is the run-once-now; StartInterval is the period after that
        args_xml = "".join(f"<string>{html.escape(a)}</string>" for a in argv)
        unit = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>dev.commons.{name}</string>
  <key>ProgramArguments</key><array>{args_xml}</array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>{minutes * 60}</integer>
  <key>StandardOutPath</key><string>{html.escape(log)}</string>
  <key>StandardErrorPath</key><string>{html.escape(log)}</string>
</dict></plist>
"""
        return _install_unit("launchd", name, unit,
                             done=f"installed ~/Library/LaunchAgents/dev.commons.{name}.plist; {what} runs {every} (log {log})")
    if sys.platform.startswith("win"):
        inner = " ".join(f'\\"{a}\\"' if any(c in a for c in ' "') else a for a in argv)
        return _install_unit("windows", name, f'schtasks /Create /SC MINUTE /MO {minutes} /TN {win_task} /TR "{inner}" /F')
    cmdline = " ".join(shlex.quote(a) for a in argv)
    service = f"""[Unit]
Description=Commons {what}

[Service]
Type=oneshot
ExecStart={cmdline}
StandardOutput=append:{log}
StandardError=append:{log}
"""
    timer = f"""[Unit]
Description=Commons {what} {every}

[Timer]
OnBootSec=2min
OnUnitActiveSec={minutes}min

[Install]
WantedBy=timers.target
"""
    folder = Path.home() / ".config" / "systemd" / "user"
    files = [(str(folder / f"commons-{name}.service"), service), (str(folder / f"commons-{name}.timer"), timer)]
    # enable --now starts the timer; start the service too so the first run does not wait for OnBootSec
    cmds = [["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", f"commons-{name}.timer"],
            ["systemctl", "--user", "start", f"commons-{name}.service"]]
    return _install_unit("systemd", f"commons-{name}", service, files=files, cmds=cmds,
                         done=f"installed {files[0][0]} and commons-{name}.timer; {what} runs {every} (log {log})")
    if sys.platform.startswith("win"):
        inner = " ".join(f'\\"{a}\\"' if any(c in a for c in ' "') else a for a in argv)
        return _install_unit("windows", "sync",
                             f'schtasks /Create /SC MINUTE /MO 10 /TN CommonsQuotaSync /TR "{inner}" /F')
    cmd = " ".join(shlex.quote(a) for a in argv)
    service = f"""[Unit]
Description=Commons quota sync (models, tool version, usage windows)

[Service]
Type=oneshot
ExecStart={cmd}
StandardOutput=append:{log}
StandardError=append:{log}
"""
    timer = """[Unit]
Description=Commons quota sync every 10 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
"""
    folder = Path.home() / ".config" / "systemd" / "user"
    files = [(str(folder / "commons-sync.service"), service), (str(folder / "commons-sync.timer"), timer)]
    # enable --now starts the timer; start the service too so the first sync does not wait for OnBootSec
    cmds = [["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", "commons-sync.timer"],
            ["systemctl", "--user", "start", "commons-sync.service"]]
    return _install_unit("systemd", "commons-sync", service, files=files, cmds=cmds,
                         done=f"installed {files[0][0]} and commons-sync.timer; "
                              f"quota sync runs every 10 minutes (log {log})")
