"""v0.7 MCP tools: agent messaging, presence/wake, routing (execution targets), Commons Drive, multi-project scheduling."""
from __future__ import annotations

import json

from .store import CommonsError


def _js(o):
    return json.dumps(o, ensure_ascii=False, default=str, separators=(",", ":"))


def register_v7(mcp, store, who):
    from .breakdown import breakdown
    from .drive import Drive
    from .messaging import Channels, Messaging, board, heartbeat
    from .routing import Router
    from .scheduler import Scheduler
    msg, router, sched = Messaging(store), Router(store), Scheduler(store)
    _drive: list = []

    def drive():
        if not _drive:
            _drive.append(Drive(store))
        return _drive[0]

    def me(agent, api_key):
        aid, _ = who(agent, api_key)
        if not aid:
            raise CommonsError("identify yourself: pass agent (or use an agent key)")
        return aid

    # ---- messaging
    @mcp.tool()
    def summon(agents: list[str] | None = None, reason: str = "", wait: float = 10, project: str = "",
               agent: str = "", api_key: str = "") -> str:
        """Call other agents to work NOW and see who is ready (default: every agent except you). Agents running
        `commons agent daemon` or holding a live stream start within a second; others are woken through their
        channels, and the result says how to make any unreachable one instantly wakeable. `reason` is the task text
        they receive."""
        from .messaging import summon as _summon
        return _js(_summon(store, me(agent, api_key), agents or None, reason, wait=min(max(wait, 0), 30),
                           project=project or None))

    @mcp.tool()
    def msg_send(to: str, body: str, type: str = "message", subject: str = "", thread: int = 0, reply_to: int = 0,
                 project: str = "", reroute: bool = True, agent: str = "", api_key: str = "") -> str:
        """Message one agent (to=name) or all (to='*'). type: message|task|question|review|fyi. type=task becomes a tracked job (routed to a live agent if the recipient isn't, unless reroute=false; a big task is split across the team, favouring the recipient; result returned to you). The reply also shows what Commons already knows about it."""
        from .messaging import recipient_status
        sender = me(agent, api_key)
        if type == "task" and to != "*" and store.get_agent(to):  # a hand-off is tracked work, not just mail
            from .orchestrator import Orchestrator
            d = Orchestrator(store).delegate(sender, to, body, subject, project or None, reroute=reroute)
            out = {"delegated": d}
            if not d["rerouted"] and not d.get("split"):
                out |= msg.send(sender, to, body + f"\n\n(Tracked as job {d['job_id']}: claim_subtask "
                                f"{d['subtask_id']}, then complete_subtask with the result.)", mtype=type,
                                subject=subject, thread=thread or None, reply_to=reply_to or None,
                                project=project or None)
        else:
            out = msg.send(sender, to, body, mtype=type, subject=subject, thread=thread or None,
                           reply_to=reply_to or None, project=project or None) | recipient_status(store, to, sender)
        if to != "*":  # handing work off? show what Commons already knows, so the sender can often just do it
            from .capture import Capture
            known = Capture(store).recall(f"{subject} {body}"[:2000], project or None, max_tokens=400)
            if known.strip():
                out["commons_knows"] = known.strip()
                out["hint"] = "You may not need another agent: check this first, and do it yourself if you can."
        return _js(out)

    @mcp.tool()
    def msg_recv(wait: float = 0, limit: int = 20, ack: bool = False, types: list[str] | None = None,
                 agent: str = "", api_key: str = "") -> str:
        """Your unacked messages (at-least-once). wait = long-poll seconds (<=60). Ack with msg_ack after handling."""
        from .server import playbook_once
        aid = me(agent, api_key)
        pb = playbook_once(store, aid)
        if pb:  # agents that only poll their mail get the routine here (pickup/turn deliver it too)
            msg.send(None, store.label(aid)[1], pb, mtype="fyi", subject="Commons playbook (new or updated): follow it")
        return _js(msg.recv(aid, wait=min(wait, 25), limit=limit, ack=ack, types=types))

    @mcp.tool()
    def msg_ack(ids: list[int], agent: str = "", api_key: str = "") -> str:
        """Acknowledge handled messages."""
        return _js(msg.ack(me(agent, api_key), ids))

    @mcp.tool()
    def msg_history(peer: str = "", thread: int = 0, limit: int = 50, agent: str = "", api_key: str = "") -> str:
        """Message history for you (optionally with one peer or one thread)."""
        return _js(msg.history(me(agent, api_key), peer=peer or None, thread=thread or None, limit=limit))

    @mcp.tool()
    def presence(status: str = "", sleeping: bool = False, agent: str = "", api_key: str = "") -> str:
        """Heartbeat (status: online|idle|sleeping) and return the presence board of all agents."""
        if agent or api_key:
            heartbeat(store, me(agent, api_key), status=status or None, sleeping=sleeping or None, client="mcp")
        return _js(board(store))

    @mcp.tool()
    def wake_channels(add_kind: str = "", config: dict | None = None, remove_id: str = "", agent: str = "", api_key: str = "") -> str:
        """List your wake channels; add one (kind webhook|ntfy|pushover|slack|discord|telegram|email|command + config)
        or remove one. Command hooks can only be added locally (CLI)."""
        aid = me(agent, api_key)
        ch = Channels(store)
        if add_kind:
            ch.add(aid, add_kind, config or {})
        if remove_id:
            if not any(c["id"] == remove_id for c in ch.list(aid)):
                raise CommonsError("not your channel")
            ch.remove(remove_id)
        return _js(ch.list(aid))

    # ---- routing
    @mcp.tool()
    def route_why(ref: str) -> str:
        """Why a subtask (or decision id) got its agent + model + effort: rationale, alternatives, cost, outcome."""
        return _js(router.why(ref))

    @mcp.tool()
    def route_policy(task_type: str = "", effort: str = "", tier: str = "", mode: str = "") -> str:
        """Show the routing policy table; optionally set one task type's effort (minimal|low|medium|high|max) or tier
        (cheap|standard|top|coding), or the global mode (quality-first|balanced|cost-first)."""
        if task_type and (effort or tier):
            router.set_policy(task_type, effort=effort or None, tier=tier or None)
        if mode:
            router.set_mode(mode)
        return _js({"mode": router.mode(), "policy": router.policy(), "tiers": router.tiers()})

    # ---- Commons Drive
    @mcp.tool()
    def fs_list(prefix: str = "/", project: str = "", recursive: bool = False) -> str:
        """List Commons Drive files/folders (per project; '' = shared). Host mounts appear under /mnt/<name>."""
        return _js(drive().list(prefix, project or None, recursive))

    @mcp.tool()
    def fs_read(path: str, project: str = "", version: int = 0, max_tokens: int = 2000, offset_line: int = 0) -> str:
        """Read a Drive file within a token budget; continue with offset_line=next_offset_line."""
        return _js(drive().read(path, project or None, version=version or None, max_tokens=max_tokens, offset_line=offset_line))

    @mcp.tool()
    def fs_write(path: str, content: str, project: str = "", expected_version: int = -1, message: str = "",
                 tags: list[str] | None = None, subtask_id: str = "", agent: str = "", api_key: str = "") -> str:
        """Create/overwrite a Drive file (new version). expected_version guards against overwriting others' edits
        (0 = must not exist). subtask_id links the artifact to the task that produced it. Secrets are redacted."""
        aid, _ = who(agent, api_key)
        return _js(drive().write(path, content, project=project or None, agent_id=aid,
                                 expected_version=None if expected_version < 0 else expected_version, message=message,
                                 tags=tags, link=subtask_id or None))

    @mcp.tool()
    def fs_edit(path: str, old: str, new: str, project: str = "", expected_version: int = -1, message: str = "",
                agent: str = "", api_key: str = "") -> str:
        """Replace one exact occurrence of `old` with `new` (optimistic concurrency on the current version)."""
        aid, _ = who(agent, api_key)
        return _js(drive().edit(path, old, new, project=project or None, agent_id=aid,
                                expected_version=None if expected_version < 0 else expected_version, message=message))

    @mcp.tool()
    def fs_move(src: str, dst: str, project: str = "", agent: str = "", api_key: str = "") -> str:
        """Move/rename a Drive file (history is kept)."""
        return _js(drive().move(src, dst, project or None, who(agent, api_key)[0]))

    @mcp.tool()
    def fs_delete(path: str, project: str = "", agent: str = "", api_key: str = "") -> str:
        """Delete a Drive file (soft: fs_restore brings it back)."""
        return _js(drive().delete(path, project or None, who(agent, api_key)[0]))

    @mcp.tool()
    def fs_history(path: str, project: str = "", diff_from: int = 0, diff_to: int = 0) -> str:
        """Version history of a file; with diff_from (and optional diff_to) returns a unified diff."""
        if diff_from:
            return drive().diff(path, diff_from, diff_to or None, project or None)
        return _js(drive().history(path, project or None))

    @mcp.tool()
    def fs_restore(path: str, version: int = 0, project: str = "", agent: str = "", api_key: str = "") -> str:
        """Restore a deleted file or roll back to a version (creates a new version)."""
        return _js(drive().restore(path, version or None, project or None, who(agent, api_key)[0]))

    @mcp.tool()
    def fs_search(query: str, project: str = "", max_tokens: int = 800) -> str:
        """Full-text/semantic search over Drive text files (and path names)."""
        return drive().search(query, project or None, max_tokens)["text"] or "no matches"

    @mcp.tool()
    def fs_lock(path: str, project: str = "", ttl: int = 600, release: bool = False, agent: str = "", api_key: str = "") -> str:
        """Take (or release) an exclusive edit lock on a Drive file."""
        return _js(drive().lock(path, me(agent, api_key), project or None, ttl, release))

    # ---- scheduling / mission control
    @mcp.tool()
    def mission_control() -> str:
        """All active projects/jobs/agents at once: queue depth, in-flight work, load vs capacity, throughput, cost."""
        return _js(sched.mission_control())

    @mcp.tool()
    def job_breakdown(job_id: str) -> str:
        """How a job was split: planning step, DAG, and each subtask's agent/model/effort/rationale/timeline/cost/outputs."""
        return _js(breakdown(store, job_id))

    @mcp.tool()
    def schedule_project(project: str, priority: int = -999, weight: float = 0, max_concurrent: int = -1,
                         budget_cost: float = -1, paused: str = "") -> str:
        """Per-project scheduling: priority, fair-share weight, max in-flight subtasks, $ budget, pause ('true'/'false')."""
        return _js(sched.configure(project, priority=None if priority == -999 else priority, weight=weight or None,
                                   max_concurrent=None if max_concurrent < 0 else max_concurrent,
                                   budget_cost=None if budget_cost < 0 else budget_cost,
                                   paused=None if not paused else paused.lower() == "true"))

    @mcp.tool()
    def schedule_job(job_id: str, priority: int = -999, after: list[str] | None = None) -> str:
        """Set a job's priority and/or make it wait for other jobs (any project)."""
        return _js(sched.set_job(job_id, priority=None if priority == -999 else priority, after=after))

    @mcp.tool()
    def preempt(below_priority: int = 0, requeue: bool = False, resume_job: str = "", resume_all: bool = False) -> str:
        """Pause running jobs with priority below N (requeue=True also hands back their in-flight subtasks), or resume."""
        if resume_job or resume_all:
            return _js(sched.resume(resume_job or None))
        return _js(sched.preempt(below_priority, requeue))
