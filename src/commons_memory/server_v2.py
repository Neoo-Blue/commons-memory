"""v2 MCP tools: profiles, orchestration, leftovers/recurring, preferences, usage, kill switch."""
from __future__ import annotations

import json

from .orchestrator import Orchestrator
from .server_v5 import make_admin
from .store import CommonsError
from .usage import Usage


def _js(o):
    return json.dumps(o, ensure_ascii=False, default=str, separators=(",", ":"))


def register_v2(mcp, store, who):
    orch = Orchestrator(store)
    usage = Usage(store)
    mcp.orch = orch

    def me(agent, api_key, write=True):
        aid, _ = who(agent, api_key, write)
        if not aid:
            raise CommonsError("identify yourself with agent or api_key")
        return aid

    # ------------------------------------------------------------ profiles
    @mcp.tool()
    def set_profile(expertise: dict | None = None, strengths: list[str] | None = None, cost: float | None = None,
                    speed: float | None = None, availability: str = "", limits: dict | None = None,
                    max_concurrent: int | None = None, platform: str = "", reach: list[str] | None = None,
                    skills: list[str] | None = None, agent: str = "", api_key: str = "") -> str:
        """Declare your capability profile. expertise: {task_type: 0..1} for research|plan|code|review|debate|judge|
        red_team|test|summarize|vote|general. cost/speed 1..5. availability: online|offline|paused. reach: what you
        can access (e.g. "LAN 192.168.50.0/24 via tailnet", "ssh pve", "web search", "GitHub CLI"). skills: your local
        skills that can't go in the pool (tied to your environment), so others can ask you via skill_help."""
        return _js(orch.profiles.set(me(agent, api_key), expertise=expertise, strengths=strengths, cost=cost, speed=speed,
                                     availability=availability or None, limits=limits, max_concurrent=max_concurrent,
                                     platform=platform or None, reach=reach, skills=skills))

    @mcp.tool()
    def get_profile(agent_name: str) -> str:
        """An agent's declared profile + learned success rates per task type + current load."""
        return _js(orch.profiles.get(agent_name))

    @mcp.tool()
    def rate_agent(agent_name: str, task_type: str, success: bool, rating: float | None = None, note: str = "",
                   agent: str = "", api_key: str = "") -> str:
        """Record an outcome (success, optional 1-5 rating) for an agent on a task type; improves future routing."""
        me(agent, api_key)
        a = store.get_agent(agent_name)
        if not a:
            raise CommonsError("unknown agent")
        return _js(orch.profiles.record_outcome(a["id"], task_type, success, rating, note=note))

    # ------------------------------------------------------------ orchestration
    @mcp.tool()
    def submit_task(task: str, pattern: str = "auto", project: str = "", n: int = 3, budget: dict | None = None,
                    dag: list[dict] | None = None, context_pack: str = "", dedupe: bool = True, agent: str = "",
                    api_key: str = "") -> str:
        """Split a task across the agents. pattern=auto: the plan chain (a model, else a live agent, else rules) splits it
        to fit who is live, what each is good at and their usage left; or plan_execute_verify|cross_review|debate|
        consensus|parallel. Or a dag: [{key,type,title,body,deps,gates,agent}] (agent pins a step). budget:
        {tokens,cost,seconds}. context_pack: a context_save id the parts get as a prefix."""
        return _js(orch.submit(task, pattern=pattern, project=project or None, agent_id=me(agent, api_key), n=n,
                               budget=budget, dag=dag, context_pack=context_pack or None,
                               dedupe=dedupe))

    @mcp.tool()
    def job_status(job_id: str = "", status: str = "", format: str = "text") -> str:
        """DAG status for a job (or recent jobs filtered by status)."""
        if job_id:
            st = orch.status(job_id)
            return st["text"] if format == "text" else _js(st)
        js = orch.jobs(status or None, 20)
        return "\n\n".join(j["text"] for j in js) if format == "text" else _js(js)

    @mcp.tool()
    def claim_subtask(subtask_id: str = "", lease_seconds: int = 900, agent: str = "", api_key: str = "") -> str:
        """Claim the next ready subtask assigned to you (or a specific one). Returns a work packet including dependency
        results and user preferences. Heartbeat before the lease expires; returns null when nothing is ready."""
        return _js(orch.claim(me(agent, api_key), subtask_id or None, lease_seconds))

    @mcp.tool()
    def split_subtask(subtask_id: str, parts: list, agent: str = "", api_key: str = "") -> str:
        """A subtask you claimed is too big for one sitting, or has parts other agents do better: hand those parts out.
        parts: [{title, body, type?, deps?: [keys of other parts]}] (2-12, or plain strings). Each part goes to the
        agent best at it with usage to spare; you are woken to merge their results (they come with your claim)."""
        return _js(orch.split(subtask_id, me(agent, api_key), parts))

    @mcp.tool()
    def heartbeat(subtask_id: str, lease_seconds: int = 900, agent: str = "", api_key: str = "") -> str:
        """Extend the lease on a subtask you are working on."""
        return _js(orch.heartbeat(subtask_id, me(agent, api_key), lease_seconds))

    @mcp.tool()
    def complete_subtask(subtask_id: str, result: str, verdict: str = "", success: bool = True,
                         tokens: int | None = None, tokens_in: int | None = None, cost: float | None = None,
                         model: str = "", leftovers: list[str] | None = None, preferences: list[str] | None = None,
                         repo: str = "", branch: str = "", commit: str = "", diff: str = "",
                         agent: str = "", api_key: str = "") -> str:
        """Finish a claimed subtask. Changed code? pass repo (owner/name), branch, commit and/or diff: they land in the
        job's workspace (/jobs/<job>/<key>/) with your result. Gate tasks need verdict approve|reject (or a 'VERDICT:' line in result).
        leftovers: unfinished items to carry forward. preferences: user preferences you learned. success=false only when you
        could not do the work (it is retried); a review or test that finds problems is success=true with the findings."""
        return _js(orch.complete(subtask_id, me(agent, api_key), result, verdict=verdict or None, tokens=tokens,
                                 tokens_in=tokens_in, cost=cost, model=model or None, leftovers=leftovers,
                                 preferences=preferences, success=success,
                                 changes={"repo": repo, "branch": branch, "commit": commit, "diff": diff}
                                 if (commit or diff) else None))

    @mcp.tool()
    def cancel_job(job_id: str, reason: str = "cancelled", agent: str = "", api_key: str = "") -> str:
        """Cancel a job and all of its unfinished subtasks."""
        return _js(orch.cancel(job_id, reason, me(agent, api_key)))

    @mcp.tool()
    def kill_switch(paused: bool, reason: str = "manual kill switch", agent: str = "", api_key: str = "") -> str:
        """Pause (true) or resume (false) ALL orchestration: no new claims or submissions while paused. Any agent can
        pause; resuming takes an admin, so an agent can't undo the owner's stop."""
        me(agent, api_key)
        if not paused:
            make_admin(store, who)(None, agent, api_key)
        return _js(orch.set_paused(paused, reason))

    # ------------------------------------------------------------ continuity
    @mcp.tool()
    def pickup(project: str = "", max_tokens: int = 900, agent: str = "", api_key: str = "") -> str:
        """Start-of-session check-in: ready subtasks, inbox, leftovers ranked by your expertise, plus user preferences."""
        from .server import playbook_once
        aid = me(agent, api_key, write=False)
        from .handover import nudge
        from .releases import nudge as rnudge
        return (playbook_once(store, aid) + rnudge(store, aid) + nudge(store, aid)
                + orch.pickup(aid, project or None, max_tokens)["text"])

    @mcp.tool()
    def add_leftover(text: str, task_type: str = "general", priority: int = 1, project: str = "", source_id: str = "",
                     agent: str = "", api_key: str = "") -> str:
        """Record an unfinished item so another agent (or the next recurring run) picks it up."""
        return _js(orch.add_leftover(text, task_type=task_type, priority=priority, project=project or None,
                                     source_id=source_id or None, agent_id=me(agent, api_key)))

    @mcp.tool()
    def resolve_leftover(leftover_id: str, note: str = "", agent: str = "", api_key: str = "") -> str:
        """Mark a leftover done."""
        return _js(orch.resolve_leftover(leftover_id, note, me(agent, api_key)))

    @mcp.tool()
    def list_leftovers(project: str = "", status: str = "open") -> str:
        """Leftover queue."""
        return _js(orch.leftovers(project or None, status))

    @mcp.tool()
    def schedule_recurring(name: str, cron: str, task: str, pattern: str = "auto", project: str = "",
                           budget: dict | None = None, agent: str = "", api_key: str = "") -> str:
        """Define a recurring task (5-field cron or @daily/@hourly/@weekly). Each run carries forward open leftovers
        from previous runs. Runs are started by `commons scheduler` (or run_recurring)."""
        return _js(orch.schedule(name, cron, task, pattern=pattern, project=project or None, budget=budget,
                                 agent_id=me(agent, api_key)))

    @mcp.tool()
    def run_recurring(name: str = "", agent: str = "", api_key: str = "") -> str:
        """Start all due recurring runs now (or force one by name)."""
        me(agent, api_key)
        return _js(orch.run_due(force=name or None))

    @mcp.tool()
    def list_recurring() -> str:
        """Recurring task definitions with next run times."""
        return _js(orch.list_recurring())

    # ------------------------------------------------------------ preferences
    @mcp.tool()
    def observe_preference(statement: str, project: str = "", task_type: str = "", category: str = "",
                           confidence: float = 0.7, evidence: str = "", agent: str = "", api_key: str = "") -> str:
        """Record a user preference/taste/convention (e.g. 'Use pnpm, not npm'). Repeats reinforce confidence."""
        aid = me(agent, api_key)
        return _js(orch.prefs.observe(statement, project=project or None, task_type=task_type or None,
                                      category=category, confidence=confidence, evidence=evidence,
                                      source=f"agent:{store.label(aid)[1]}"))

    @mcp.tool()
    def extract_preferences(text: str, project: str = "", task_type: str = "", agent: str = "", api_key: str = "") -> str:
        """Extract candidate preferences from a user correction/feedback message and store them."""
        me(agent, api_key)
        return _js(orch.prefs.extract(text, project=project or None, task_type=task_type or None))

    @mcp.tool()
    def get_preferences(project: str = "", task_type: str = "", query: str = "", max_tokens: int = 300) -> str:
        """Relevant user preferences (decayed confidence, scoped by project/task type) within max_tokens."""
        r = orch.prefs.get(project or None, task_type or None, query, max_tokens)
        return r["text"] or "(no preferences recorded)"

    @mcp.tool()
    def confirm_preference(pref_id: str, statement: str = "") -> str:
        """User confirms (optionally rewrites) a preference: confidence -> 1.0."""
        return _js(orch.prefs.confirm(pref_id, statement or None))

    @mcp.tool()
    def forget_preference(pref_id: str = "", everything: bool = False) -> str:
        """Delete one preference, or everything=true to wipe the user profile."""
        return _js(orch.prefs.forget(pref_id or None, everything))

    # ------------------------------------------------------------ knowledge base & graph views
    from .graphexport import export as _export
    from .graphlinks import rebuild as _rebuild
    from .graphlinks import subgraph as _subgraph
    from .kb import KB
    kb = KB(store)

    @mcp.tool()
    def kb_get(project: str = "", section: str = "", max_tokens: int = 800) -> str:
        """Auto-maintained knowledge base for a project ('misc' or 'misc/<topic>' for unassigned memories).
        section: overview|decisions|architecture|glossary|references|howtos|people|open_questions|timeline|preferences
        (empty = all, within max_tokens). Every statement cites record ids [mem_…] you can expand()."""
        return kb.get(project or "misc", section, max_tokens)["text"]

    @mcp.tool()
    def kb_search(query: str, project: str = "", limit: int = 15, max_tokens: int = 600) -> str:
        """Search knowledge-base statements (with citations) across all projects or one."""
        return kb.search(query, project, limit, max_tokens)["text"]

    @mcp.tool()
    def kb_list() -> str:
        """Knowledge-base buckets (projects, misc, misc/<topic>) with freshness and citation counts."""
        return _js(kb.list())

    @mcp.tool()
    def kb_refresh(project: str = "") -> str:
        """Rebuild the KB for one project (or all) now."""
        return _js(kb.build(project) if project else kb.build_all())

    @mcp.tool()
    def graph_rebuild() -> str:
        """Backfill graph links for every record (schedules, runs, decisions, preferences, workflows, verdicts,
        usage, leases, secret refs). Idempotent."""
        return _js(_rebuild(store))

    @mcp.tool()
    def graph_view(scope: str = "project", id: str = "", depth: int = 2, format: str = "mermaid", limit: int = 300) -> str:
        """Scoped graph: scope=project (name) | job (job id) | workflow (pattern name) | schedule (rec id) | node (any id).
        format: mermaid|graphml|json|html."""
        return _export(_subgraph(store, scope, id, depth, limit), format)

    # ------------------------------------------------------------ usage
    @mcp.tool()
    def report_usage(tokens_in: int = 0, tokens_out: int = 0, model: str = "", cost: float | None = None,
                     seconds: float = 0.0, project: str = "", job_id: str = "", subtask_id: str = "", note: str = "",
                     agent: str = "", api_key: str = "") -> str:
        """Log tokens/cost/time you spent (cost estimated from the price table when omitted)."""
        return _js(usage.report(me(agent, api_key), tokens_in=tokens_in, tokens_out=tokens_out, model=model or None,
                                cost=cost, seconds=seconds, project=project or None, job_id=job_id or None,
                                subtask_id=subtask_id or None, note=note))

    @mcp.tool()
    def usage_summary(by: str = "agent") -> str:
        """Spend by agent|project|job|model|day."""
        return _js(usage.summary(by))
