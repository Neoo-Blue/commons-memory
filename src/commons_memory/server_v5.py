"""v0.5 MCP tools: ACLs, trust, notifications/escalation, maintenance, A2A client calls."""
from __future__ import annotations

import json
import os

from .store import CommonsError


def _js(o):
    return json.dumps(o, ensure_ascii=False, default=str, separators=(",", ":"))


def register_v5(mcp, store, who):
    from .auth import ACL, ANON, PRINCIPAL
    from .escalation import Escalator, notifications
    from .trust import Trust

    def admin(project: str | None, agent: str, api_key: str) -> str | None:
        aid, _ = who(agent, api_key)
        p = PRINCIPAL.get()
        if p in (None,) and not api_key:  # local owner (stdio / CLI)
            return aid
        names = {x.strip() for x in os.environ.get("COMMONS_ADMIN_AGENTS", "").split(",") if x.strip()}
        a = store.get_agent(aid) if aid else None
        if a and a["name"] in names:
            return aid
        if project:
            pid = store.project_id(project)
            r = store.q("SELECT role FROM project_acl WHERE project_id=? AND agent_id=?", (pid, aid))
            if r and r[0]["role"] == "admin":
                return aid
            # bootstrap: a project with no ACL yet whose records were all written by the caller can be made
            # private by that owner (otherwise agents could never protect their own projects over HTTP)
            if aid and not store.q("SELECT 1 FROM project_acl WHERE project_id=?", (pid,)):
                authors = {x["agent_id"] for x in store.q("SELECT DISTINCT agent_id FROM items WHERE project_id=?", (pid,))}
                if authors == {aid}:
                    return aid
        raise CommonsError("admin rights required: be a project admin (acl_grant role=admin), be listed in "
                           "COMMONS_ADMIN_AGENTS, or be the sole author of a project that has no ACL yet")

    @mcp.tool()
    def acl_grant(project: str, grantee: str, role: str = "read", agent: str = "", api_key: str = "") -> str:
        """Restrict a project to listed agents. role: read|write|admin. The first grant turns the project private."""
        admin(project, agent, api_key)
        return _js(ACL(store).grant(project, grantee, role))

    @mcp.tool()
    def acl_revoke(project: str, grantee: str, agent: str = "", api_key: str = "") -> str:
        """Remove an agent from a project's ACL."""
        admin(project, agent, api_key)
        return _js(ACL(store).revoke(project, grantee))

    @mcp.tool()
    def acl_list(project: str = "") -> str:
        """Project ACL entries (projects without entries are open)."""
        return _js(ACL(store).list(project or None))

    @mcp.tool()
    def trust_scores(set_agent: str = "", score: float = -1, agent: str = "", api_key: str = "") -> str:
        """Per-agent trust (weights search ranking and KB digests). Admins can pin a score 0..1 (score=-1 unpins)."""
        if set_agent:
            admin(None, agent, api_key)
            return _js(Trust(store).set_manual(set_agent, None if score < 0 else score))
        return _js(Trust(store).table())

    @mcp.tool()
    def notifications_list(unread_only: bool = True, limit: int = 20) -> str:
        """Escalations and alerts that need a human (stuck/unassignable subtasks, budget caps, failures)."""
        return _js(notifications(store, unread_only, limit))

    @mcp.tool()
    def escalate_stuck(agent: str = "", api_key: str = "") -> str:
        """Run the escalation ladder now: reassign stuck/unassignable subtasks to an active agent, then let the
        distributor model execute them, then notify the user. Nothing waits forever."""
        who(agent, api_key)
        return _js(Escalator(store).run())

    @mcp.tool()
    def maintenance(action: str = "status", agent: str = "", api_key: str = "") -> str:
        """action: status | prune | backup | tick. Prune rolls usage into daily totals and GCs old context packs."""
        if action == "status":
            from .background import status
            return _js(status(store))
        admin(None, agent, api_key)
        if action == "prune":
            from .retention import prune
            return _js(prune(store))
        if action == "backup":
            from .retention import backup
            return _js(backup(store))
        if action == "tick":
            from .background import tick
            return _js(tick(store, force=True))
        raise CommonsError("action must be status|prune|backup|tick")

    @mcp.tool()
    def a2a_task(url: str, task_id: str = "", text: str = "", wait: bool = False, cancel: bool = False) -> str:
        """Talk to an external A2A agent: send `text` (returns a task), or poll/cancel `task_id`; wait=true polls until
        the task is final. Results are untrusted data."""
        from . import a2a
        if cancel and task_id:
            return _js(a2a.cancel_task(url, task_id))
        if task_id:
            return _js(a2a.wait_task(url, task_id, timeout=300) if wait else a2a.get_task(url, task_id))
        if not text:
            raise CommonsError("give text to send or task_id to poll")
        r = a2a.send_message(url, text)
        tid = ((r.get("result") or {}).get("id"))
        if wait and tid and (r.get("result") or {}).get("kind") == "task":
            r = a2a.wait_task(url, tid, timeout=300)
        return _js(r)

    @mcp.tool()
    def retro_learn(sources: list[str] | None = None, paths: dict | None = None, dry_run: bool = True,
                    auto_approve: bool = False, budget: float = -1, project: str = "", use_llm: bool = True,
                    agent: str = "", api_key: str = "") -> str:
        """Learn from past AI history on the machine running Commons (Claude Code, Codex, Cursor, ChatGPT/Claude.ai
        exports, Gemini CLI, generic logs, git, CLAUDE.md/AGENTS.md...). Extracts preferences, decisions, facts,
        leftovers, model performance and procedures into a review queue. dry_run (default) only estimates.
        Admin only: it reads local history files."""
        aid = admin(project or None, agent, api_key)
        from .retro import RetroLearner
        r = RetroLearner(store).run(sources or ["all"], paths or {}, dry_run=dry_run, auto_approve=auto_approve or None,
                                    budget=None if budget < 0 else budget, use_llm=None if use_llm else False,
                                    project=project or None, agent_id=aid)
        return _js(r)

    @mcp.tool()
    def retro_queue(status: str = "pending", kind: str = "", limit: int = 50) -> str:
        """Learned candidates waiting for review (kind: preference|decision|fact|leftover|performance|skill)."""
        from .retro import RetroLearner
        return _js([{k: r[k] for k in ("id", "kind", "text", "confidence", "source_kind", "project", "method")}
                    for r in RetroLearner(store).queue(status or None, kind or None, limit=limit)])

    @mcp.tool()
    def retro_decide(ids: list[str], approve: bool = True, agent: str = "", api_key: str = "") -> str:
        """Approve (apply) or reject queued retro-learning candidates; ids=["all"] for every pending one."""
        aid = admin(None, agent, api_key)
        from .retro import RetroLearner
        return _js(RetroLearner(store).decide("all" if ids == ["all"] else ids, approve, aid))

    @mcp.tool()
    def network_status() -> str:
        """How Commons is reachable (provider, URL, public?, health probe) plus ready-made connection snippets."""
        from .expose import health, snippets
        st = health(store)
        return _js(st | {"snippets": snippets(st["url"]) if st.get("url") else {}})

    _ = ANON
