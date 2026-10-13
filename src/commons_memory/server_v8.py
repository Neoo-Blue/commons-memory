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
    def turn(user_message: str = "", reply: str = "", conversation: str = "", project: str = "", agent: str = "",
             api_key: str = "") -> str:
        """Chat apps without hooks: call on EVERY turn, before answering: turn(user_message, reply=<your previous reply>). Stores both word for word and returns what Commons already knows about the message."""
        aid = me(agent, api_key)
        import time as _t
        pl = {"session_id": conversation or f"{aid}:{_t.strftime('%Y%m%d', _t.gmtime())}", "project": project or None}
        out = {}
        if reply.strip():
            r = cap.event(aid, "stop", pl | {"reply": reply, "transcript_tail": "assistant: " + reply[-8000:]}, harness="mcp")
            out["captured"] = r.get("observations", 0)
        if user_message.strip():
            r = cap.event(aid, "prompt", pl | {"prompt": user_message}, harness="mcp")
            out["context"] = r.get("context") or "Nothing in Commons matches this message."
        if not out:
            raise CommonsError("pass user_message (before you answer) and/or reply (your previous answer)")
        from .server import playbook_once
        pb = playbook_once(store, aid)
        return _js(({"playbook": pb} if pb else {}) | out)

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
    def quota_report(windows: list[dict], for_agent: str = "", agent: str = "", api_key: str = "") -> str:
        """Report your usage windows so Commons only hands you work you can finish: [{window: "five_hour", used_pct:
        14, resets_at: "2026-10-13T03:00:00Z"}, {window: "weekly", ...}]. for_agent: report for another agent (only
        callers listed in COMMONS_QUOTA_REPORTERS, e.g. the machine running `commons quota sync` from aiusage)."""
        import os as _os
        from . import quota as Q
        caller = me(agent, api_key)
        target = caller
        if for_agent:
            t = store.get_agent(for_agent)
            if not t:
                raise CommonsError(f"unknown agent {for_agent}")
            if t["id"] != caller:
                allowed = {x.strip() for x in _os.environ.get("COMMONS_QUOTA_REPORTERS", "").split(",") if x.strip()}
                if store.label(caller)[1] not in allowed:
                    raise CommonsError("only agents in COMMONS_QUOTA_REPORTERS may report quota for other agents")
            target = t["id"]
        return _js(Q.report(store, target, windows, source="agent" if target == caller else f"via {store.label(caller)[1]}"))

    @mcp.tool()
    def quota_status() -> str:
        """Every agent's windows: used %, the user's reserve (static floor or learned need until reset), what is free
        for Commons work, reset times, and who can take work now."""
        from . import quota as Q
        return _js(Q.status(store))

    @mcp.tool()
    def subtask_pause(subtask_id: str, checkpoint: str, resume_at: str = "", release: bool = False, agent: str = "",
                      api_key: str = "") -> str:
        """Near your session limit? Park a claimed subtask with a checkpoint (what's done, what's left, where the work
        is). It resumes at resume_at (default: your next quota reset) with the checkpoint in the packet; release=true
        lets another agent with headroom continue it now."""
        from .orchestrator import Orchestrator
        from .store import _parse_time
        return _js(Orchestrator(store).pause(subtask_id, me(agent, api_key), checkpoint,
                                             _parse_time(resume_at) if resume_at else None, release))

    @mcp.tool()
    def quota_exhausted(resets_at: str = "", checkpoint: str = "", agent: str = "", api_key: str = "") -> str:
        """You hit your usage limit: Commons marks you full until resets_at and parks everything you hold, with the
        checkpoint, so nothing is lost and the work resumes after the reset (or moves to an agent with headroom)."""
        from .orchestrator import Orchestrator
        from .store import _parse_time
        return _js(Orchestrator(store).exhausted(me(agent, api_key), _parse_time(resets_at) if resets_at else None,
                                                 checkpoint))

    @mcp.tool()
    def schedule_report(name: str, cron: str, instructions: str, acceptance: str = "", how: str = "", project: str = "",
                        tz: str = "UTC", grace_minutes: float = 30, agent: str = "", api_key: str = "") -> str:
        """Register a task YOU run on a schedule (cron + IANA tz), with the instructions another agent would follow,
        how it runs (tools, inputs, outputs) and the acceptance bar. Commons watches it: if a run is missed or you are
        out of quota, another agent takes it over to the same bar. Report each run with schedule_ran."""
        from . import schedules as SC
        return _js(SC.register(store, me(agent, api_key), name, cron, instructions, acceptance=acceptance, how=how,
                               project=project or None, tz=tz, grace_minutes=grace_minutes))

    @mcp.tool()
    def schedule_ran(name: str, status: str = "ok", summary: str = "", output_ref: str = "", agent: str = "",
                     api_key: str = "") -> str:
        """Report a scheduled run (ok | failed | partial). The latest good summary becomes the quality reference a
        takeover agent must match."""
        from . import schedules as SC
        return _js(SC.ran(store, me(agent, api_key), name, status, summary, output_ref))

    @mcp.tool()
    def schedules(agent_name: str = "") -> str:
        """Every registered scheduled task: owner, cron/tz, next run, whether the latest run was delivered (by the owner
        or a takeover), how it runs and its acceptance bar."""
        from . import schedules as SC
        return _js(SC.listing(store, agent_name or None))

    @mcp.tool()
    def import_items(items: list[dict] | None = None, check: list[str] | None = None, agent: str = "",
                     api_key: str = "") -> str:
        """Bulk-import memories (max 500 per call), idempotent by each item's `src` id. Item fields: content* (or text), kind,
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
    def start_work(what: str, project: str = "", paths: list[str] | None = None, force: bool = False, reason: str = "",
                   agent: str = "", api_key: str = "") -> str:
        """Before ANY task: checks it against other agents' live and recently finished work. Branch on status: busy = someone has it, don't start; done = build on it (force=true + reason only for a real follow-up). ok = yours until finish_work or handoff; `related` lists connected memories to update. paths: files or resources you'll change."""
        from . import work as W
        return _js(W.start(store, me(agent, api_key), what, project or None, paths, force, reason))

    @mcp.tool()
    def finish_work(claim_id: str = "", result: str = "", status: str = "done", agent: str = "", api_key: str = "") -> str:
        """Close your work claim (all of your open ones when claim_id is empty) with the result: what changed, where,
        and how it was verified. status: done | dropped. This is what the next agent is shown, so be specific."""
        from . import work as W
        return _js(W.finish(store, me(agent, api_key), claim_id, result, status))

    @mcp.tool()
    def working_now(project: str = "") -> str:
        """Every agent's open work claims (who is doing what, since when, which paths)."""
        from . import work as W
        return _js(W.active(store, project or None))

    @mcp.tool()
    def credentials() -> str:
        """The user's API keys Commons holds (names, services, where each may be sent; never values). Use one with
        use_credential; never ask the user to paste a key."""
        from . import vault as V
        return _js(V.listing(store))

    @mcp.tool()
    def use_credential(credential: str, url: str, justification: str, method: str = "GET", body: str = "",
                       content_type: str = "", agent: str = "", api_key: str = "") -> str:
        """Call a service with one of the user's keys without seeing it: Commons makes the request and returns the response. justification = what you need it for; the decision agent allows sound reads, writes need the user's request, anything risky goes to the user (verdict waiting_for_user, answer arrives in msg_recv)."""
        from . import vault as V
        return _js(V.use(store, me(agent, api_key), credential, method, url, justification, body, content_type))

    @mcp.tool()
    def vault_generate(name: str, service: str, url: str, purpose: str, username: str = "", length: int = 24,
                       symbols: bool = True, agent: str = "", api_key: str = "") -> str:
        """Creating an account? Get a username and a strong password from the vault, which stores them. The password
        is shown this once: use it to finish the sign-up. url = the site, purpose = what the account is for (the
        user's ask). Leave username empty to have one generated, or pass the email/handle you'll register with."""
        from . import vault as V
        return _js(V.generate(store, me(agent, api_key), name, service, url, username, length, symbols, purpose))

    @mcp.tool()
    def vault_reveal(name: str, justification: str, agent: str = "", api_key: str = "") -> str:
        """Get a stored login (username + password) back, e.g. to sign in again. Yours for 24 hours after you created
        it; otherwise the decision agent or the user decides. API keys are never revealed (use use_credential)."""
        from . import vault as V
        return _js(V.reveal(store, me(agent, api_key), name, justification))

    @mcp.tool()
    def skill(name: str = "", query: str = "", include_files: bool = True, agent: str = "", api_key: str = "") -> str:
        """Use a skill straight from Commons, nothing to install: skill(name) returns its full instructions (and its
        files) to follow now; skill(query=...) finds skills. Can't run it (a tool, access or environment you lack)?
        skill_help(name, task, why) gets an agent that can to do it for you."""
        from .skillpool import SkillPool
        aid = me(agent, api_key)
        if not name:
            r = SkillPool(store).list(query, max_tokens=600)
            return r.get("text") or _js(r)
        try:
            sk = SkillPool(store).get(name, include_files=include_files, agent_id=aid)
        except CommonsError:
            from .skillhelp import declared
            who_has = [store.label(a)[1] for a in declared(store, name)]
            if who_has:
                return _js({"not_in_pool": name, "local_to": who_has,
                            "do": f"skill_help(name='{name}', task=...) and one of them runs it for you"})
            raise
        return _js(sk | {"how": "Follow these instructions now. After using it: skill_rate(name, success). If you "
                                "can't run it, skill_help(name, task, why)."})

    @mcp.tool()
    def skill_help(name: str, task: str, why: str = "", agent: str = "", api_key: str = "") -> str:
        """Ask for help with a skill you can't run (missing tool, access or environment, or a skill only another agent
        has): Commons hands the task to an agent that has used or holds it, live ones first, and the result comes back
        to you in msg_recv."""
        from .skillhelp import ask
        return _js(ask(store, me(agent, api_key), name, task, why))

    @mcp.tool()
    def status_update(state: str, headline: str, bullets: list[str] | None = None, agent: str = "", api_key: str = "") -> str:
        """Your status card on the dashboard: state = working | blocked | done | needs_you | idle, headline <= 90 chars,
        up to 5 short bullets. start_work, finish_work and Needs-you questions set it for you."""
        from .status import put
        return _js(put(store, me(agent, api_key), state, headline, bullets))

    @mcp.tool()
    def whats_new(agent: str = "", api_key: str = "") -> str:
        """What changed in Commons since you last looked, and what you must adapt (from the release channel)."""
        from .releases import whats_new as _wn
        return _js(_wn(store, me(agent, api_key)))

    @mcp.tool()
    def judge_answer(request_id: str, legit: float, destructive: float = 0.0, user_backed: float = 0.0, reason: str = "",
                     agent: str = "", api_key: str = "") -> str:
        """Answer a credential-request judgment Commons sent you (you are the decision agent for it): the probability
        (0-1) that each answer is yes. Pass the questions you were asked; reason = one line."""
        from . import vault as V
        sc = {"legit": legit, "destructive": destructive, "user_backed": user_backed}
        r = store.q("SELECT questions FROM vault_judgments WHERE id=?", (request_id,))
        asked = json.loads(r[0]["questions"]) if r else {}
        return _js(V.judge_answer(store, me(agent, api_key), request_id, {k: sc[k] for k in asked}, reason))

    @mcp.tool()
    def vault_deposit(name: str, value: str, service: str, base_urls: list[str], auth: str = "bearer",
                      extra_headers: list[str] | None = None, description: str = "", risk: str = "normal",
                      agent: str = "", api_key: str = "") -> str:
        """Hand over an API key, token or password you hold so Commons keeps it (encrypted) and other agents can use it
        through use_credential without seeing it. base_urls = where it may be sent; auth = bearer | basic:USER |
        header:NAME | header:NAME:PREFIX | query:PARAM; risk = low | normal | high (high: every write asks the user).
        You can add new keys and update your own, not replace another's."""
        from . import vault as V
        return _js(V.deposit(store, me(agent, api_key), name, value, service, base_urls, auth, extra_headers,
                             description, risk))

    @mcp.tool()
    def handover_status(agent: str = "", api_key: str = "") -> str:
        """What Commons still needs from you (profile and reach, memories, keys, scheduled tasks, usage windows,
        current work, live activity), with the tool for each. Hand everything over, and anything new as you get it."""
        from . import handover as H
        return _js(H.status(store, me(agent, api_key)))

    @mcp.tool()
    def handover_report(item: str, status: str = "none", note: str = "", agent: str = "", api_key: str = "") -> str:
        """Declare a hand-over item you have nothing for (status=none, e.g. no scheduled tasks), or done."""
        from . import handover as H
        return _js(H.report(store, me(agent, api_key), item, status, note))

    @mcp.tool()
    def forget(id: str, agent: str = "", api_key: str = "") -> str:
        """Delete a memory you wrote yourself (a mistake, a blank or broken import). Other agents' memories can only be
        removed by the user from the dashboard."""
        aid = me(agent, api_key)
        r = store.q("SELECT agent_id, tags FROM items WHERE id=?", (id,))
        if not r:
            raise CommonsError(f"unknown id {id}")
        if "conversation" in json.loads(r[0]["tags"] or "[]"):
            raise CommonsError("conversation records (what the user and agents said) can only be removed by the user")
        if r[0]["agent_id"] != aid:
            raise CommonsError("you can only forget memories you wrote; ask the user to remove this one in the dashboard")
        return _js(store.delete_item(id, agent_id=aid))

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
        """Write an interactive HTML diagram to Commons Drive. kind: code (target=project; source links pinned to the
        indexed commit), workflow (target=job id), sequence (target=job id for how it moved between agents, or a
        function name for its call path), lifecycle (target=subtask|board: real state machine with transition counts),
        delta (target=project or project:from:to snapshot positions/commits). Returns the Drive path."""
        from .diagrams import make
        html = make(store, kind, target)
        from .drive import Drive
        path = f"/{target or 'shared'}/diagrams/{kind}-{(target or 'all').replace('/', '_')}.html".replace("//", "/")
        try:
            Drive(store).write(path, html.encode(), agent_id=None, project=None, mime="text/html")
        except Exception as e:  # noqa: BLE001
            return _js({"error": str(e), "bytes": len(html)})
        return _js({"path": path, "bytes": len(html)})
