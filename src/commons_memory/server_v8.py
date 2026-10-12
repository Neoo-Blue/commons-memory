"""v0.8 MCP tools: observations/capture, progressive-disclosure search, temporal facts, scoped spaces, diagrams."""
from __future__ import annotations

import json

from .store import CommonsError


def _js(o):
    return json.dumps(o, ensure_ascii=False, default=str, separators=(",", ":"))


def register_v8(mcp, store, who):
    from .capture import KINDS, Capture
    cap = Capture(store)

    def me(agent, api_key):
        aid, _ = who(agent, api_key)
        if not aid:
            raise CommonsError("identify yourself: pass agent (or use an agent key)")
        return aid

    @mcp.tool()
    def observe(kind: str, text: str, project: str = "", why: str = "", agent: str = "", api_key: str = "") -> str:
        """Record a typed observation as you work: kind = decision|bugfix|discovery|security_alert|todo|change.
        decision -> reasoning log, todo -> leftover, security_alert -> notification. <private>…</private> is dropped."""
        if kind not in KINDS:
            raise CommonsError(f"kind must be one of {', '.join(KINDS)}")
        aid = me(agent, api_key)
        from .capture import strip_private
        text = strip_private(text)
        if kind == "decision":
            r = store.record_decision(text, why or "(observed)", agent_id=aid, project=project or None)
        else:
            r = store.add_item("observation", text, title=f"[{kind}] {text[:70]}", project=project or None,
                               agent_id=aid, tags=["observation", kind], meta={"source": "mcp:observe"})
            if kind == "todo":
                from .orchestrator import Orchestrator
                Orchestrator(store).add_leftover(text, project=project or None, source_id=r["id"], agent_id=aid)
        return _js({"id": r["id"], "kind": kind})

    @mcp.tool()
    def capture_settings(project: str, enabled: str = "", tools: str = "", prompts: str = "", inject: str = "",
                         agent: str = "", api_key: str = "") -> str:
        """Per-project auto-capture switches (hooks): enabled/tools/prompts/inject = on|off; empty = unchanged."""
        me(agent, api_key)
        kw = {k: (v == "on") if v else None for k, v in
              (("enabled", enabled), ("tools", tools), ("prompts", prompts), ("inject", inject))}
        return _js(cap.configure(project, **kw))

    @mcp.tool()
    def board(project: str = "", path: str = "", status: str = "open", max_tokens: int = 1500) -> str:
        """The project board: the user's asks (verbatim), targets with goal/current, expectations, actions and what was
        delivered, layer by layer (path, e.g. "ui/dashboard"). status: open|done|dropped|all. Read it before working
        and before you stop: everything open here is still owed to the user."""
        from . import board as B
        return B.view(store, project or None, path, status, max_tokens=max_tokens)["text"]

    @mcp.tool()
    def board_add(kind: str, text: str, project: str = "", path: str = "", parent: str = "", metric: str = "",
                  goal: str = "", current: str = "", status: str = "open", evidence: str = "", agent: str = "",
                  api_key: str = "") -> str:
        """Record on the project board. kind: ask (the user's request, VERBATIM; record every one before you start) |
        target (metric + goal, e.g. metric="search p50" goal="<50 ms") | expectation (acceptance criterion) | action
        (a step you will take) | delivered (what you shipped; needs evidence). parent = the ask it serves. Re-adding an
        open ask returns it with repeated=true: the user had to say it twice, so do it now."""
        from . import board as B
        aid, _ = who(agent, api_key)
        return _js(B.add(store, kind, text, project=project or None, path=path, parent=parent or None,
                         metric=metric or None, goal=goal or None, current=current or None, status=status,
                         evidence=evidence or None, agent_id=aid))

    @mcp.tool()
    def board_update(id: str, status: str = "", current: str = "", evidence: str = "", goal: str = "",
                     path: str = "", agent: str = "", api_key: str = "") -> str:
        """Move a board item: status open|doing|blocked|done|dropped, a new measured `current` for a target. done needs
        evidence (commit, file, benchmark, item id, URL); dropped needs the reason in evidence. Asks can't be edited,
        and an ask can't be done while items under it are open."""
        from . import board as B
        aid, _ = who(agent, api_key)
        return _js(B.update(store, id, status=status or None, current=current or None, evidence=evidence or None,
                            goal=goal or None, path=path or None, agent_id=aid))

    @mcp.tool()
    def import_items(items: list[dict] | None = None, check: list[str] | None = None, agent: str = "",
                     api_key: str = "") -> str:
        """Bulk-import memories (max 500 per call), idempotent by each item's `src` id. Item fields: content*, kind,
        title, summary, project, tags, created_at (epoch or ISO), src, preference (-> also a shared user preference),
        status (keep | review = hold in the review queue | drop/merge/duplicate = record only). check=[src...]
        returns which srcs are already handled. `commons import <source> [--curate] --url ...` drives this."""
        from .importers import import_items as _imp
        from .importers import known
        aid = me(agent, api_key)
        if check:
            from .importers import queued_srcs
            return _js({"known": sorted(known(store, check)), "queued": sorted(queued_srcs(store, check))})
        items = items or []
        if len(items) > 500:
            raise CommonsError("import_items takes at most 500 items per call")
        return _js(_imp(store, items, agent_id=aid))

    @mcp.tool()
    def memory_tier(id: str, tier: str, agent: str = "", api_key: str = "") -> str:
        """Pin a memory (tier=hot: stays in every agent's check-in), return it to warm, or archive it (cold: ranked
        lower, never deleted, promoted again when used)."""
        me(agent, api_key)
        from .tiers import set_tier
        return _js(set_tier(store, id, tier))

    @mcp.tool()
    def memory_stream(project: str = "", limit: int = 30) -> str:
        """Latest captured observations (prompts, tool calls, typed observations) across agents."""
        return _js(cap.stream(project or None, limit=limit))
    # ---- progressive disclosure: search_index -> timeline_around -> get_items
    @mcp.tool()
    def search_index(query: str, project: str = "", kinds: list[str] | None = None, since: str = "", until: str = "",
                     as_of: str = "", limit: int = 20, rerank: bool = False) -> str:
        """STEP 1 of 3 (cheapest). Ranked ids with a ~12-word gist each (≈15 tokens/hit). Pick promising ids, then
        STEP 2 timeline_around(id) for what happened around one, STEP 3 get_items(ids) for full content.
        as_of: reference time for 'last week'/'in May' style queries (default now)."""
        ids, total, why = store.rank(query, project=project or None, kinds=kinds, since=since or None,
                                     until=until or None, limit=max(limit, 30) if rerank else limit, as_of=as_of or None)
        if ids is None:  # unknown project / entity
            return why["text"]
        if rerank:
            from .rerank import rerank_ids
            ids = rerank_ids(store, query, ids, how="cross-encoder")[:limit]
        rows = {}
        for i in range(0, len(ids), 500):
            ch = ids[i:i + 500]
            for r in store.q(f"SELECT id, kind, summary, created_at FROM items WHERE id IN ({','.join('?' * len(ch))})", ch):
                rows[r["id"]] = r
        from .store import iso
        lines = [f"{i} {rows[i]['kind'][:4]} {iso(rows[i]['created_at'])[:10]} {' '.join((rows[i]['summary'] or '').split()[:12])}"
                 for i in ids if i in rows]
        return "\n".join(lines) + f"\n({len(lines)} of {total}; next: timeline_around(id) / get_items([ids]))"

    @mcp.tool()
    def timeline_around(anchor: str, before: int = 5, after: int = 5, same_project: bool = True) -> str:
        """STEP 2 of 3. Items immediately before/after an anchor item (same project by default): the context of a hit."""
        a = store.q("SELECT id, project_id, created_at FROM items WHERE id=?", (anchor,))
        if not a:
            raise CommonsError(f"no item {anchor}")
        a = a[0]
        store.check_ids(anchor)
        cond, args = ("AND project_id IS ?" if a["project_id"] is None else "AND project_id=?", [a["project_id"]]) if same_project else ("", [])
        denied = store.denied_projects()
        if denied and not same_project:
            cond += f" AND (project_id IS NULL OR project_id NOT IN ({','.join('?' * len(denied))}))"
            args += denied
        prev = store.q(f"SELECT id, kind, summary, created_at FROM items WHERE created_at<? {cond} AND superseded_by IS NULL "
                       f"ORDER BY created_at DESC LIMIT ?", [a["created_at"]] + args + [before])
        nxt = store.q(f"SELECT id, kind, summary, created_at FROM items WHERE created_at>? {cond} AND superseded_by IS NULL "
                      f"ORDER BY created_at LIMIT ?", [a["created_at"]] + args + [after])
        me_ = store.q("SELECT id, kind, summary, created_at FROM items WHERE id=?", (anchor,))
        from .store import iso
        rows = list(reversed(prev)) + me_ + nxt
        return "\n".join(f"{'>' if r['id'] == anchor else ' '} {r['id']} {iso(r['created_at'])[:16]} [{r['kind']}] "
                         f"{(r['summary'] or '')[:140]}" for r in rows)

    @mcp.tool()
    def get_items(ids: list[str], max_tokens: int = 3000) -> str:
        """STEP 3 of 3. Full content for several ids at once (token-budgeted, in the order given)."""
        from .store import estimate_tokens
        out, used = [], 0
        for i in ids[:50]:
            try:
                d = store.expand(i, max_tokens=max(200, max_tokens - used))
            except Exception as e:  # noqa: BLE001
                d = {"id": i, "error": str(e)}
            t = estimate_tokens(_js(d))
            if used + t > max_tokens and out:
                out.append({"id": i, "truncated": "budget exhausted; request fewer ids"})
                break
            used += t
            out.append(d)
        return _js(out)

    # ---- temporal facts
    from .facts import Facts
    facts = Facts(store)

    @mcp.tool()
    def fact_set(subject: str, predicate: str, value: str, valid_from: str = "", project: str = "",
                 agent: str = "", api_key: str = "") -> str:
        """Assert subject.predicate = value from valid_from (default now). The previous value is closed at exactly
        that instant (valid_to), never deleted, so fact_get(as_of=...) answers 'what was true then'."""
        return _js(facts.set(subject, predicate, value, valid_from=valid_from or None, project=project or None,
                             agent_id=me(agent, api_key)))

    @mcp.tool()
    def fact_invalidate(subject: str, predicate: str, at: str = "", project: str = "", agent: str = "",
                        api_key: str = "") -> str:
        """End the current value of subject.predicate (in `project`; empty = unscoped facts) at `at` (default now)
        without replacing it."""
        me(agent, api_key)
        return _js(facts.invalidate(subject, predicate, at=at or None, project=project or None))

    @mcp.tool()
    def fact_get(subject: str, predicate: str = "", as_of: str = "", project: str = "") -> str:
        """Facts about subject valid at as_of (default now). Omit predicate for all of them; project narrows to one
        project's timeline."""
        return _js(facts.get(subject, predicate or None, as_of=as_of or None, project=project or None))

    @mcp.tool()
    def fact_history(subject: str, predicate: str = "", project: str = "") -> str:
        """Full validity timeline of a subject's facts (valid_from -> valid_to)."""
        return _js(facts.history(subject, predicate or None, project=project or None))

    # ---- scoped spaces
    from .spaces import Spaces, audit
    sp = Spaces(store)

    @mcp.tool()
    def spaces(scope_type: str = "") -> str:
        """List memory spaces: projects, KB topics and people (scope_type: project|topic|person)."""
        return _js(sp.list(scope_type or None))

    @mcp.tool()
    def space_search(scope: str, query: str, limit: int = 10, max_tokens: int = 600) -> str:
        """Search inside one space: 'project:NAME', 'topic:NAME' or 'person:NAME' (items linked to that person)."""
        return sp.search(scope, query, limit=limit, max_tokens=max_tokens)["text"]

    @mcp.tool()
    def memory_audit(project: str = "") -> str:
        """Organisation score of the memory (orphans, untagged, duplicates, stale leftovers, conflicts) with fixes."""
        return _js(audit(store, project or None))

    @mcp.tool()
    def diagram(kind: str, target: str = "") -> str:
        """Write an interactive HTML diagram to Commons Drive: kind=code (target=project), job (target=job id) or
        delta (target=project: files and dependencies added/removed since the previous index). Returns the Drive path."""
        from .diagrams import make
        html = make(store, kind, target)
        from .drive import Drive
        path = f"/{target or 'shared'}/diagrams/{kind}-{(target or 'all').replace('/', '_')}.html".replace("//", "/")
        try:
            Drive(store).write(path, html.encode(), agent_id=None, project=None, mime="text/html")
        except Exception as e:  # noqa: BLE001
            return _js({"error": str(e), "bytes": len(html)})
        return _js({"path": path, "bytes": len(html)})
