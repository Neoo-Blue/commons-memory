"""v0.4 MCP tools: skill pool, research, providers/models/routing, org-chart tree, librarian, find."""
from __future__ import annotations

import json

from .librarian import Librarian
from .providers import ProviderLayer
from .research import Research
from .skillpool import SkillPool
from .store import CommonsError
from .tree import Tree


def _js(o):
    return json.dumps(o, ensure_ascii=False, default=str, separators=(",", ":"))


def seed_skills(store) -> None:
    """Make sure the core `commons` skill is in the pool (active)."""
    pool = SkillPool(store)
    if store.q("SELECT 1 FROM skills WHERE name='commons'"):
        return
    from importlib import resources
    content = (resources.files("commons_memory") / "skills" / "SKILL.md").read_text(encoding="utf-8")
    import os
    prev = os.environ.get("COMMONS_SKILL_AUTO_APPROVE")
    os.environ["COMMONS_SKILL_AUTO_APPROVE"] = "1"  # built-in, shipped with the package
    try:
        pool.publish("commons", content, tags=["core", "memory", "coordination"], agent_id=store.ensure_agent("commons"),
                     changelog="built-in core skill")
    finally:
        if prev is None:
            os.environ.pop("COMMONS_SKILL_AUTO_APPROVE", None)
        else:
            os.environ["COMMONS_SKILL_AUTO_APPROVE"] = prev


def register_v4(mcp, store, who):
    pool, res, pl, lib = SkillPool(store), Research(store), ProviderLayer(store), Librarian(store)
    seed_skills(store)

    def me(agent, api_key, write=True):
        aid, _ = who(agent, api_key, write)
        if not aid:
            raise CommonsError("identify yourself with agent or api_key")
        return aid

    # ------------------------------------------------------------ skill pool
    @mcp.tool()
    def skill_list(query: str = "", tags: list[str] | None = None, max_tokens: int = 500) -> str:
        """Skills available in the shared pool (one line each, with success rates). No local install needed."""
        return pool.list(query, tags, max_tokens)["text"]

    @mcp.tool()
    def skill_search(query: str, max_tokens: int = 400) -> str:
        """Find skills relevant to a task."""
        return pool.list(query, None, max_tokens)["text"]

    @mcp.tool()
    def skill_get(name: str, version: int | None = None, max_tokens: int = 3000, include_files: bool = False,
                  agent: str = "", api_key: str = "") -> str:
        """Fetch a skill (SKILL.md + file list, or files with include_files) and follow it. Token-budgeted."""
        aid, _ = who(agent, api_key, False)
        return _js(pool.get(name, version, max_tokens, include_files, aid))

    @mcp.tool()
    def skill_publish(name: str, content: str, description: str = "", tags: list[str] | None = None,
                      deps: list[str] | None = None, files: dict | None = None, agent: str = "", api_key: str = "") -> str:
        """Share a new skill (Agent Skills SKILL.md format). It is scanned (secrets, prompt injection) and reviewed
        before it becomes active."""
        return _js(pool.publish(name, content, files=files, description=description, tags=tags, deps=deps,
                                agent_id=me(agent, api_key)))

    @mcp.tool()
    def skill_update(name: str, content: str, changelog: str = "", files: dict | None = None,
                     tags: list[str] | None = None, agent: str = "", api_key: str = "") -> str:
        """Propose a new version of a skill (goes through the same gate)."""
        return _js(pool.update(name, content, files=files, changelog=changelog, tags=tags, agent_id=me(agent, api_key)))

    @mcp.tool()
    def skill_review(name: str, version: int, approve: bool, notes: str = "", agent: str = "", api_key: str = "") -> str:
        """Approve/reject a pending skill version (you cannot review your own)."""
        return _js(pool.review(name, version, approve, me(agent, api_key), notes))

    @mcp.tool()
    def skill_history(name: str, diffs: bool = True) -> str:
        """All versions with status, reviewer, findings and unified diffs."""
        return _js(pool.history(name, diffs))

    @mcp.tool()
    def skill_rollback(name: str, version: int, agent: str = "", api_key: str = "") -> str:
        """Re-activate a previously approved version."""
        return _js(pool.rollback(name, version, me(agent, api_key)))

    @mcp.tool()
    def skill_rate(name: str, success: bool, rating: float | None = None, model: str = "", subtask_id: str = "",
                   note: str = "", agent: str = "", api_key: str = "") -> str:
        """Report how a skill worked for you (feeds per-agent/per-model success rates)."""
        return _js(pool.rate(name, success, rating, me(agent, api_key), model or None, subtask_id or None, note))

    @mcp.tool()
    def skill_fork(name: str, new_name: str, agent: str = "", api_key: str = "") -> str:
        """Copy a skill under a new name to adapt it."""
        return _js(pool.fork(name, new_name, me(agent, api_key)))

    @mcp.tool()
    def skill_catalog(mode: str = "trending", query: str = "", limit: int = 15) -> str:
        """Public skills discovered on GitHub (mode trending|popular). Metadata only; import with skill_catalog_add."""
        from .skilldiscovery import Discovery
        rows = Discovery(store).list(mode, query, limit=limit)
        return "\n".join(f"{r['id']} ★{r['stars']} (+{r['delta_7d']}/7d) [{r['kind']}, {r['license']}] {r['description'][:80]}"
                         for r in rows) or "(catalog empty — run `commons skills discover`)"

    @mcp.tool()
    def skill_catalog_add(catalog_id: str, name: str = "", agent: str = "", api_key: str = "") -> str:
        """Import a pinned version of a discovered skill into the pool (license check + scan; needs review)."""
        from .skilldiscovery import Discovery
        return _js(Discovery(store).add(catalog_id, name or None, me(agent, api_key)))

    # ------------------------------------------------------------ research
    @mcp.tool()
    def research(query: str, provider: str = "", max_results: int = 5, fetch_pages: bool = True, project: str = "",
                 refresh: bool = False, agent: str = "", api_key: str = "") -> str:
        """Web research (brave|tavily|exa|searxng|fetch). Results are cached and stored as cited `source` memories plus
        a research note that feeds the project KB. Returns the note with [source ids]."""
        r = res.research(query, provider=provider or None, max_results=max_results, fetch_pages=fetch_pages,
                         project=project or None, agent_id=me(agent, api_key), refresh=refresh)
        return _js({"note_id": r["note_id"], "cached": r["cached"], "summary": r["summary"],
                    "sources": [{k: s[k] for k in ("id", "url", "title")} for s in r["sources"]]})

    # ------------------------------------------------------------ providers & routing
    @mcp.tool()
    def provider_add(name: str, kind: str, key_ref: str = "", base_url: str = "") -> str:
        """Connect a model provider (anthropic|openai|openrouter|google|xai|ollama). key_ref is a secret reference
        like env://OPENROUTER_API_KEY — never paste a raw key."""
        return _js(pl.add_provider(name, kind, key_ref or None, base_url or None))

    @mcp.tool()
    def models_refresh(provider: str = "") -> str:
        """Pull model lists from provider APIs and flag NEW models."""
        return _js(pl.refresh_models(provider or None))

    @mcp.tool()
    def models_list(provider: str = "", new_only: bool = False) -> str:
        """Model registry: context, cost, probe scores, which agents can use each."""
        return _js(pl.models(provider or None, new_only))

    @mcp.tool()
    def agent_models(models: list[str], agent: str = "", api_key: str = "") -> str:
        """Declare which models (provider/model ids) you can run."""
        return _js(pl.set_agent_models(me(agent, api_key), models))

    @mcp.tool()
    def set_role_model(role: str, model: str = "") -> str:
        """Set the model for a role: distributor | planner | router | pref_extractor | kb_summarizer | skill_reviewer |
        grader | librarian (empty = unset; unset roles fall back to distributor, then heuristics)."""
        return _js(pl.set_role(role, model or None))

    @mcp.tool()
    def routing_table(refresh: bool = False) -> str:
        """Task type -> agent + model routing (refresh=true lets the distributor rebuild it)."""
        return _js(pl.refresh_routing() if refresh else {"table": pl.routing(), "roles": pl.roles()})

    @mcp.tool()
    def models_probe(models: list[str] | None = None, budget: float | None = None) -> str:
        """Benchmark models (default: NEW ones) on cheap probes per task type within a $ budget."""
        return _js(pl.probe(models, budget=budget))

    @mcp.tool()
    def routing_proposals(decide_id: str = "", approve: bool = True) -> str:
        """List reallocation proposals, or approve/reject one by id."""
        return _js(pl.decide(decide_id, approve) if decide_id else pl.proposals())

    # ------------------------------------------------------------ tree
    @mcp.tool()
    def tree(root: str = "workspace", depth: int = 2, max_tokens: int = 600) -> str:
        """Org-chart outline with stable index paths. root: workspace | agents | skills | an index path like '1.3' or
        'agents/2'. Drill down by passing a path; expand(<id>) opens a record."""
        o = Tree(store).outline(root, depth, max_tokens)
        return o["text"] + "\n" + o["hint"]

    @mcp.tool()
    def tree_find(query: str, root: str = "workspace", limit: int = 10) -> str:
        """Search node labels in the hierarchy; returns index paths with breadcrumbs."""
        return "\n".join(f"{h['path']}  {' > '.join(h['breadcrumbs'])}" + (f" <{h['id']}>" if h.get("id") else "")
                         for h in Tree(store).find(query, root, limit=limit)) or "(no match)"

    # ------------------------------------------------------------ MCP hub
    from .mcphub import Hub
    hub = Hub(store)

    @mcp.tool()
    def mcp_servers() -> str:
        """Upstream MCP servers proxied by Commons: health, tool counts (credentials shown as refs only)."""
        return _js([{k: s_[k] for k in ("name", "transport", "status", "tools", "last_error", "preset")} for s_ in hub.servers()])

    @mcp.tool()
    def mcp_search(query: str = "", server: str = "", limit: int = 15, max_tokens: int = 500,
                   agent: str = "", api_key: str = "") -> str:
        """Find upstream tools by keyword. Returns compact `server__tool [risk] summary` lines (cheap)."""
        aid, _ = who(agent, api_key, False)
        r = hub.search(query, server or None, limit, max_tokens, aid)
        return r["text"] + "\n" + r["hint"]

    @mcp.tool()
    def mcp_describe(tool: str) -> str:
        """Full input schema for one upstream tool (`server__tool`)."""
        return _js(hub.describe(tool))

    @mcp.tool()
    def mcp_call(tool: str, args: dict | None = None, approval_id: str = "", agent: str = "", api_key: str = "") -> str:
        """Invoke an upstream tool through Commons (policy, approvals, rate limit, audit, cache). Output is wrapped
        as untrusted data. Destructive tools return approval_required first; retry with approval_id once approved."""
        return _js(hub.call(tool, args or {}, me(agent, api_key), approval_id or None))

    # ------------------------------------------------------------ shared context store
    from .contextpacks import ContextStore
    cs = ContextStore(store)

    @mcp.tool()
    def context_save(name: str, gist: str, brief: dict | None = None, sections: dict | None = None,
                     refs: list | None = None, delta: bool = False, project: str = "", session_id: str = "",
                     agent: str = "", api_key: str = "") -> str:
        """Snapshot your working context as a layered, deduplicated pack (L0 gist, L1 brief {goal,state,decisions,
        open_threads,next_steps}, L2 sections {title: text}, L3 refs [ids/paths/hashes]). delta=true: send only changed
        sections (null removes one). Pass the returned pack_id in handoffs/submit_task instead of text."""
        return _js(cs.save(name, gist, brief, sections, refs, agent_id=me(agent, api_key), project=project or None,
                           session_id=session_id or None, delta=delta))

    @mcp.tool()
    def context_load(pack: str, layer: str = "L1", max_tokens: int = 2000, model: str = "", sections: list[str] | None = None,
                     agent: str = "", api_key: str = "") -> str:
        """Load a context pack (id or name=latest) at a layer within max_tokens (and the model's window).
        The stable `prefix` is cache-friendly; ask for L2 sections or L3 refs only when needed."""
        aid, _ = who(agent, api_key, False)
        r = cs.load(pack, layer, max_tokens, model or None, sections, aid)
        return r["text"] + f"\n\n[{r['layer']} · {r['tokens']}/{r['full_tokens']} tokens · omitted: {', '.join(r['omitted']) or '-'}]"

    @mcp.tool()
    def context_diff(pack: str, other: str = "") -> str:
        """What changed between two packs (or a pack and its parent)."""
        return _js(cs.diff(pack, other or None))

    @mcp.tool()
    def context_fit(pack: str, model: str, reserve: int = 8000) -> str:
        """Pick and load the deepest layer that fits the receiving model's context window."""
        r = cs.fit(pack, model, reserve)
        return f"[{r['layer']} fits {r['model']} ({r['context_window']} ctx, budget {r['budget']})]\n" + r["load"]["text"]

    # ------------------------------------------------------------ librarian
    @mcp.tool()
    def find(query: str, limit: int = 5, max_tokens: int = 300) -> str:
        """Cheapest way to locate anything: the librarian's index returns the best few places (tree paths + ids)."""
        return lib.find(query, limit, max_tokens)["text"]

    @mcp.tool()
    def librarian_run(full: bool = False, delegate: bool = False) -> str:
        """Run the librarian now (incremental unless full). delegate=true posts a `librarian` subtask for an agent."""
        return _js(lib.delegate() if delegate else lib.run(full=full))

    @mcp.tool()
    def librarian_status() -> str:
        """Mode, last run, pending proposals and recent changes."""
        return _js(lib.status())

    @mcp.tool()
    def librarian_proposals(status: str = "proposed") -> str:
        """Destructive/attention items queued for review (merges, stale and conflict flags)."""
        return _js(lib.proposals(status))

    @mcp.tool()
    def librarian_approve(proposal_id: str, approve: bool = True) -> str:
        """Approve (apply) or reject a librarian proposal."""
        return _js(lib.decide(proposal_id, approve))

    @mcp.tool()
    def librarian_undo(log_id: str) -> str:
        """Undo one librarian change (tags, project assignment, merge, summary)."""
        return _js(lib.undo(log_id))
