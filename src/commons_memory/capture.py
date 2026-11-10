"""Zero-effort capture: lifecycle hooks + typed observation pipeline (v0.8).

Harness hooks (Claude Code, Codex, Gemini CLI, Cursor) call ``commons hook <harness> <event>``; the hook client
normalises the payload and posts it to ``/agent/hook`` (remote server, agent key) or handles it in-process (local
DB). Canonical events:

==============  ===============================================================================================
session_start   inject a token-budgeted context pack (pickup: tasks/inbox/leftovers, preferences, KB digest,
                recent decisions and observations for the project); the hook client puts Canon first, from its
                local cache (canoncache.py), and holds a start the hub missed for the session's next prompt
prompt          record the prompt; return a small relevant-memory snippet when there is a confident match; the
                hook client adds Canon changes since the session's watermark and the card of a product named
tool            append a raw observation (tool name + compact input/output), fire-and-forget
stop            summarise pending observations (+ transcript tail) into typed observations
session_end     same as stop, then close the capture session
precompact      emergency save (same as stop, keeps everything verbatim too)
==============  ===============================================================================================

Typed observations (``decision``, ``bugfix``, ``discovery``, ``security_alert``, ``todo``, ``change``,
``summary``) are stored as items of kind ``observation`` and fan out: todos -> leftovers, security alerts ->
notification, everything -> KB/graph via the normal item pipeline. A captured "decision" stays an observation unless
the observer model gave its reason: a regex hit on a chat fragment is not a decision (agents record real ones with
``record_decision``). Text inside ``<private>...</private>`` never leaves the client (stripped before posting and again
server-side), secrets are redacted as everywhere else, and capture can be disabled per project.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time

from .store import CommonsError, Store, estimate_tokens, new_id, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations(
  id TEXT PRIMARY KEY, agent_id TEXT, project_id TEXT, session_key TEXT, harness TEXT, event TEXT,
  kind TEXT, text TEXT, meta TEXT, ts REAL, processed INTEGER DEFAULT 0, item_id TEXT);
CREATE INDEX IF NOT EXISTS obs_sess ON observations(session_key, processed);
CREATE INDEX IF NOT EXISTS obs_proj ON observations(project_id, ts);
CREATE TABLE IF NOT EXISTS capture_settings(
  project_id TEXT PRIMARY KEY, enabled INTEGER DEFAULT 1, tools INTEGER DEFAULT 1, prompts INTEGER DEFAULT 1,
  inject INTEGER DEFAULT 1, updated_at REAL);
"""

EVENTS = ("session_start", "prompt", "tool", "stop", "session_end", "precompact")
HARNESS_NOTICES = ("<task-notification>", "[SYSTEM NOTIFICATION", "<system-reminder>", "<agent-message",
                   "Another Claude session sent a message")  # the last two: a subagent's report relayed as a turn
KINDS = ("decision", "bugfix", "discovery", "security_alert", "todo", "change", "summary", "prompt", "tool")
PRIVATE = re.compile(r"<private>.*?(?:</private>|$)", re.DOTALL | re.IGNORECASE)

# harness event name -> canonical
HARNESS_EVENTS = {
    "claude-code": {"SessionStart": "session_start", "UserPromptSubmit": "prompt", "PostToolUse": "tool",
                    "Stop": "stop", "SessionEnd": "session_end", "PreCompact": "precompact"},
    "codex": {"SessionStart": "session_start", "UserPromptSubmit": "prompt", "PostToolUse": "tool",
              "Stop": "stop", "SessionEnd": "session_end", "PreCompact": "precompact"},
    "gemini": {"SessionStart": "session_start", "BeforeAgent": "prompt", "AfterTool": "tool",
               "AfterAgent": "stop", "SessionEnd": "session_end", "PreCompress": "precompact"},
    "cursor": {"sessionStart": "session_start", "beforeSubmitPrompt": "prompt", "postToolUse": "tool",
               "afterFileEdit": "tool", "afterShellExecution": "tool", "stop": "stop", "sessionEnd": "session_end",
               "preCompact": "precompact"},
}


def strip_private(text: str) -> str:
    return PRIVATE.sub("[private]", text or "")


def _clip(v, n: int) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    s = strip_private(s)
    return s if len(s) <= n else s[:n] + f"…(+{len(s) - n} chars)"


def project_from_cwd(cwd: str | None) -> str | None:
    """Project name = git top-level directory name (or cwd basename), sanitised to a valid project name."""
    if not cwd:
        return None
    p = os.path.abspath(cwd)
    q = p
    while q and q != os.path.dirname(q):
        if os.path.exists(os.path.join(q, ".git")):
            p = q
            break
        q = os.path.dirname(q)
    name = re.sub(r"[^A-Za-z0-9._ -]", "-", os.path.basename(p)).strip(" .-_")[:64]
    return name or None


# --------------------------------------------------------------------------------------------- classification
_RULES = [
    ("security_alert", re.compile(r"\b(vulnerab|cve-\d|security (issue|hole|bug)|leak(ed)? (a |the )?(key|secret|token)|"
                                  r"injection|xss|csrf|ssrf|rce\b|exposed (key|secret|credential))", re.IGNORECASE)),
    ("todo", re.compile(r"\b(todo|fixme|still need(s)? to|next step|follow[- ]up|left to do|not (yet )?done|"
                        r"remaining work|we should later|unfinished)\b", re.IGNORECASE)),
    ("decision", re.compile(r"\b(decided|decision|we('ll| will) (use|go with)|chose|choosing|opted for|going with|"
                            r"instead of|trade-?off)\b", re.IGNORECASE)),
    ("bugfix", re.compile(r"\b(fix(ed|es)?|bug|regression|root cause|the (issue|problem) was|traceback|"
                          r"exception|failing test)\b", re.IGNORECASE)),
    ("discovery", re.compile(r"\b(found that|turns out|discovered|learned that|note that|it seems|apparently|"
                             r"documented|the api (returns|expects))\b", re.IGNORECASE)),
]


NOT_PROSE = re.compile(r"^\s*([-+@|>#$*{}\[\]]|```|\d+[:|]|[\w.]+\(|(def|class|import|from|return|if|for)\b)")


def classify(text: str) -> str | None:
    for kind, rx in _RULES:
        if rx.search(text or ""):
            return kind
    return None


def heuristic_observations(raw: list[dict], transcript: str = "") -> list[dict]:
    """Typed observations from raw tool/prompt events + transcript tail (no LLM)."""
    out, seen = [], set()
    files = sorted({m.get("file") for m in raw if m.get("file")})
    if files:
        out.append({"kind": "change", "text": f"Edited {len(files)} file(s): " + ", ".join(files[:12]),
                    "files": files})
    sentences = []
    for chunk in [transcript] + [r.get("text", "") for r in raw if r.get("kind") in ("prompt",)]:
        sentences += [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", chunk or "") if 25 <= len(s.strip()) <= 400]
    for s in sentences:
        if NOT_PROSE.match(s) or s.count("`") >= 4:
            continue  # diff, code, table, quote or shell lines in the transcript are not decisions
        k = classify(s)
        if k and s.lower() not in seen:
            seen.add(s.lower())
            out.append({"kind": k, "text": s})
    errs = [r for r in raw if r.get("kind") == "tool" and r.get("error")]
    if errs and any(o["kind"] == "bugfix" for o in out) is False:
        out.append({"kind": "discovery", "text": f"{len(errs)} tool call(s) failed this session, e.g. "
                                                 f"{errs[0].get('tool')}: {errs[0]['text'][:160]}"})
    return out[:40]


OBSERVER_PROMPT = """You turn a coding-agent session log into durable memory. Return JSON only:
{"summary": "2-4 sentences: what was done and why",
 "observations": [{"kind": "decision|bugfix|discovery|security_alert|todo", "text": "one self-contained sentence",
                   "why": "rationale (decisions only)"}]}
Only include things worth remembering next week. Never include secrets.

SESSION LOG:
"""


class Capture:
    def __init__(self, store: Store):
        self.s = store

    # ---------------------------------------------------------------------------------------- settings
    def settings(self, project: str | None) -> dict:
        d = {"enabled": 1, "tools": 1, "prompts": 1, "inject": 1}
        if os.environ.get("COMMONS_CAPTURE", "1") in ("0", "false", "off"):
            d["enabled"] = 0
        if project:
            pid = self.s.q("SELECT id FROM projects WHERE name=? OR id=?", (project, project))
            if pid:
                r = self.s.q("SELECT * FROM capture_settings WHERE project_id=?", (pid[0]["id"],))
                if r:
                    d.update({k: r[0][k] for k in ("enabled", "tools", "prompts", "inject")})
        return d

    def configure(self, project: str, **kw) -> dict:
        pid = self.s.project_id(project)
        cur = self.settings(project)
        cur.update({k: int(bool(v)) for k, v in kw.items() if k in ("enabled", "tools", "prompts", "inject") and v is not None})
        self.s.x("INSERT OR REPLACE INTO capture_settings(project_id,enabled,tools,prompts,inject,updated_at) VALUES(?,?,?,?,?,?)",
                 (pid, cur["enabled"], cur["tools"], cur["prompts"], cur["inject"], now()))
        return {"project": project} | cur

    # ---------------------------------------------------------------------------------------- events
    def event(self, agent_id: str, event: str, payload: dict, harness: str = "generic", project: str | None = None) -> dict:
        if event not in EVENTS:
            raise CommonsError(f"unknown hook event {event!r}; one of {', '.join(EVENTS)}")
        project = project or payload.get("project") or project_from_cwd(payload.get("cwd"))
        st = self.settings(project)
        if not st["enabled"]:
            return {"ok": True, "skipped": "capture disabled for this project"}
        pid = self.s.project_id(project) if project else None
        skey = f"{harness}:{payload.get('session_id') or payload.get('conversation_id') or 'default'}"
        if event == "session_start":
            from .skillsync import index
            return {"ok": True, "skills": index(self.s),  # the hook writes pointers so every pool skill shows up natively
                    "context": self.context(agent_id, project, facts=not payload.get("canon_local")) if st["inject"] else ""}
        if event == "prompt":
            text = strip_private(payload.get("prompt") or payload.get("text") or "")
            if text.lstrip().startswith(HARNESS_NOTICES):  # a background-task notice the harness relays, not the user
                return {"ok": True, "context": ""}
            ctx, late = "", ""
            if st["inject"]:  # before storing, or it finds itself
                from . import settings
                from . import work as W
                try:
                    ctx = W.warning(self.s, agent_id, text, project) if settings.get(self.s, "dup_warnings") else ""
                except Exception:  # noqa: BLE001 - a failed check must not block the prompt
                    ctx = ""
                ctx += self.recall(text, project, skey=skey)
                if payload.get("start_pack"):  # the hub did not answer this session's start: its pack comes now
                    try:
                        late = ("Commons start pack (late: the hub did not answer when this session started)\n" +
                                self.context(agent_id, project, facts=not payload.get("canon_local")))
                    except Exception:  # noqa: BLE001 - never cost the prompt its own context
                        pass
            claim = self._session_claim(agent_id, skey, text, project, payload.get("cwd"))
            # the prompt's own warning, recall and claim id first, a late pack last: a cut to fit trims only the pack
            ctx = "\n\n".join(x.strip("\n") for x in (ctx, claim, late) if x.strip())
            if st["prompts"] and text.strip():
                self._raw(agent_id, pid, skey, harness, event, "prompt", _clip(text, 4000), {})
                self.verbatim(agent_id, project, skey, harness, "user", text)
            return {"ok": True, "context": ctx}
        if event == "tool":
            if st["tools"]:
                tool = payload.get("tool_name") or payload.get("tool") or ("Edit" if payload.get("file_path") else "shell")
                inp = payload.get("tool_input") or payload.get("command") or payload.get("edits") or {}
                outp = payload.get("tool_response") or payload.get("tool_output") or payload.get("output") or ""
                fpath = payload.get("file_path") or (inp.get("file_path") if isinstance(inp, dict) else None)
                err = bool(isinstance(outp, dict) and (outp.get("is_error") or outp.get("error"))) or \
                    bool(re.search(r"\b(Traceback|Error:|FAILED|exit code [1-9])", _clip(outp, 4000)))
                self._raw(agent_id, pid, skey, harness, event, "tool",
                          f"{tool}: {_clip(inp, 600)} -> {_clip(outp, 600)}",
                          {"tool": tool, "file": fpath, "error": err})
            return {"ok": True}
        # stop / session_end / precompact
        if st["prompts"]:
            got = [payload["reply"]] if payload.get("reply") else payload.get("replies")
            for reply in (got if isinstance(got, list) else replies(payload.get("transcript_tail", ""))):
                self.verbatim(agent_id, project, skey, harness, "assistant", reply)
        out = {"ok": True} | self.flush(agent_id, pid, skey, harness, strip_private(payload.get("transcript_tail", "")),
                                        final=event != "stop", reason=event)
        if event == "session_end":  # the session's own claim closes with it, carrying its last word as the result
            got = payload.get("replies") if isinstance(payload.get("replies"), list) else []
            self._close_session_claim(agent_id, skey, (got[-1] if got else "") or "session ended")
        if isinstance(payload.get("git"), dict):
            out["changes"] = self.changes(agent_id, skey, payload["git"])
        if event == "stop" and payload.get("keep_working"):
            mx = payload["keep_working"] if isinstance(payload["keep_working"], int) and payload["keep_working"] > 1 else None
            reason = self.keep_working(agent_id, skey, mx)
            if reason:
                out["block"] = reason
        return out

    def _session_claim(self, agent_id: str, skey: str, text: str, project: str | None, cwd: str | None) -> str:
        """The first real message of a session opens its work claim, so the board shows who is on what without each
        agent remembering start_work (on the NAS 1 session in 10 did, and the daemons never). Busy or done work is
        left to the duplicate warning above: no claim is opened over another agent's."""
        from . import settings
        from . import work as W
        t = (text or "").strip()
        # Injected text is not the task: a wake prompt that opens with the Canon block made 1 claim in 10 read "CANON (current truth...".
        if not settings.get(self.s, "auto_claims") or len(t.split()) < 4 or t.startswith(("/", "<", "CANON (", "COMPANY CORE")) or \
                self.s.q("SELECT 1 FROM meta WHERE key=?", (f"session_claim:{skey}",)):
            return ""
        try:
            r = W.start(self.s, agent_id, " ".join(t.split())[:300], project, [cwd] if cwd else None)
        except Exception:  # noqa: BLE001 - never block a prompt over this
            return ""
        if r.get("status") != "ok" or not r.get("claim_id"):
            return ""
        self.s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"session_claim:{skey}", r["claim_id"]))
        return (f"Commons opened work claim {r['claim_id']} for this task from your message, so other agents see it is "
                f"taken. Close it with finish_work(claim_id='{r['claim_id']}', result=...) when done; no start_work "
                "needed for it.\n\n")

    def _close_session_claim(self, agent_id: str, skey: str, result: str) -> None:
        """Done only when the session's last word says the work was done (Jev reads it); otherwise dropped, so an
        "I couldn't do this" never shows up later as DONE BEFORE and turns the next agent away."""
        from . import typesafe
        from . import work as W
        r = self.s.q("SELECT value FROM meta WHERE key=?", (f"session_claim:{skey}",))
        if not r or not self.s.q("SELECT 1 FROM work_claims WHERE id=? AND status='active'", (r[0]["value"],)):
            return
        text, done = strip_private(result)[:2000], False
        if typesafe.enabled() and text.strip():
            try:
                done = typesafe.noul(f"An AI agent's last message at the end of a work session:\n{text}", {"done": (
                    "Does this message say the requested work was completed, rather than refused, blocked, unfinished "
                    "or only discussed?")}, timeout=5)["done"] >= 0.6
            except Exception:  # noqa: BLE001 - unreadable: not counted as done
                done = False
        try:
            W.finish(self.s, agent_id, r[0]["value"], text, status="done" if done else "dropped")
        except Exception:  # noqa: BLE001
            pass

    def changes(self, agent_id: str, skey: str, git: dict) -> dict:
        """The hook saw code changes in the agent's working copy: keep them with the subtask it holds, else its open
        work claim, else this session, so an agent that forgets to report them loses nothing."""
        from . import settings
        from . import workspace as W
        if not settings.get(self.s, "capture_diffs"):
            return {}
        sub = self.s.q("SELECT * FROM subtasks WHERE claimed_by=? AND status='claimed' ORDER BY started_at DESC LIMIT 1",
                       (agent_id,))
        claim = None if sub else self.s.q("SELECT id FROM work_claims WHERE agent_id=? AND status='active' "
                                          "ORDER BY started_at DESC LIMIT 1", (agent_id,))
        try:
            return W.record_changes(self.s, agent_id, subtask=dict(sub[0]) if sub else None,
                                    claim=claim[0]["id"] if claim else None, session=None if sub or claim else skey,
                                    source="hook", untracked=[str(x) for x in git.get("untracked") or []][:100],
                                    **{k: str(git.get(k) or "") for k in ("repo", "branch", "commit", "diff", "stat")})
        except Exception as e:  # noqa: BLE001 - never fail a hook over this
            return {"error": str(e)[:200]}

    def verbatim(self, agent_id, project, skey, harness, role, text) -> str | None:
        """Everything said is memory: the user's messages and the agent's replies are stored word for word (searchable;
        search(context=N) brings back the turns around a hit). Distilled observations come on top, never instead."""
        from . import settings
        if not settings.get(self.s, "capture_verbatim"):
            return None
        from .redact import redact
        text = redact(strip_private(text or "").strip())[0][:20000]
        if not text or self.s.q("SELECT 1 FROM items WHERE session_id=? AND content=? LIMIT 1", (skey, text)):
            return None  # each Stop resends the transcript tail: store every message once
        who = "user" if role == "user" else self.s.label(agent_id)[1] or "agent"
        return self.s.add_item("memory", text, title=f"[{who}] {' '.join(text.split())[:70]}", project=project,
                               agent_id=agent_id, session_id=skey, tags=["conversation", role],
                               meta={"source": f"hook:{harness}", "role": role}, check_conflicts=False)["id"]

    def keep_working(self, agent_id: str, skey: str, max_rounds: int | None = None) -> str:
        """Opt-in (COMMONS_HOOK_KEEP_WORKING=1 on the client): when work is waiting for this agent and its quota allows,
        tell the harness not to stop yet. Bounded per session (COMMONS_HOOK_KEEP_WORKING_MAX rounds, default 5)."""
        from . import quota as Q
        from .orchestrator import Orchestrator
        key = f"keepwork:{skey}"
        row = self.s.q("SELECT value FROM meta WHERE key=?", (key,))
        n, ts = (json.loads(row[0]["value"]) if row else [0, 0])
        if now() - ts > 1800:
            n = 0
        if n >= (max_rounds or int(os.environ.get("COMMONS_HOOK_KEEP_WORKING_MAX", "5"))):
            return ""  # spent: no more forced rounds in this session until 30 minutes pass without one
        work = Orchestrator(self.s).claimable(agent_id, 5)
        if not work or not Q.can_take(self.s, agent_id)[0]:
            self.s.x("DELETE FROM meta WHERE key=?", (key,))
            return ""
        self.s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps([n + 1, now()])))
        lines = "\n".join(f"- {w['id']} [{w['type']}] {w['title'][:100]}" for w in work)
        return (f"Commons has {len(work)} task(s) waiting for you. Claim one with claim_subtask(subtask_id=...), do it, "
                f"complete_subtask with the full result, then stop:\n{lines}")

    def _raw(self, agent_id, pid, skey, harness, event, kind, text, meta):
        from .redact import redact
        text, _ = redact(text)
        self.s.x("INSERT INTO observations(id,agent_id,project_id,session_key,harness,event,kind,text,meta,ts) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?)", (new_id("obs"), agent_id, pid, skey, harness, event, kind, text,
                                                 json.dumps(meta), now()))

    def flush(self, agent_id, pid, skey, harness, transcript="", final=False, reason="stop") -> dict:
        rows = [dict(r) for r in self.s.q("SELECT * FROM observations WHERE session_key=? AND processed=0 AND kind IN "
                                         "('prompt','tool') ORDER BY ts", (skey,))]
        if not rows and not transcript.strip():
            return {"observations": 0}
        raw = [{"kind": r["kind"], "text": r["text"], **json.loads(r["meta"] or "{}")} for r in rows]
        project = (self.s.q("SELECT name FROM projects WHERE id=?", (pid,)) or [{"name": None}])[0]["name"] if pid else None
        typed, summary, via = None, "", "heuristic"
        try:
            from .providers import ProviderLayer
            log = "\n".join(f"[{r['kind']}] {r['text'][:500]}" for r in raw)[-12000:] + "\n\nTRANSCRIPT TAIL:\n" + transcript[-8000:]
            out = ProviderLayer(self.s).call_role("observer", OBSERVER_PROMPT + log, max_tokens=1200)
            if out:
                j = json.loads(out[out.find("{"): out.rfind("}") + 1])
                typed = [o for o in j.get("observations", []) if o.get("kind") in KINDS and o.get("text")]
                summary, via = j.get("summary", ""), "llm"
        except Exception:  # noqa: BLE001 - observer is optional; fall back to heuristics
            typed = None
        if typed is None:
            typed = heuristic_observations(raw, transcript)
            summary = ""  # the verbatim messages already hold what a count-and-first-ask summary said
        made = []
        from .orchestrator import Orchestrator
        orch = None
        for o in typed:
            text = strip_private(o["text"])[:1000]
            if self.s.q("SELECT 1 FROM observations WHERE session_key=? AND kind=? AND text=? AND processed=1 LIMIT 1",
                        (skey, o["kind"], text)):
                continue  # consecutive Stop events resend the same transcript tail: capture each line once
            # The same sentence in another session is a prompt, a playbook line or a harness notice, or already kept:
            # ten such texts were 4,442 items on a live hub ("Note that this status is a snapshot in time" 1,652 times).
            # "change" lists the files of this session, so it may repeat.
            if o["kind"] != "change":
                seen = "obs_seen:" + hashlib.sha1(" ".join(text.lower().split()).encode()).hexdigest()[:20]
                if self.s.q("SELECT 1 FROM meta WHERE key=?", (seen,)):
                    continue
                self.s.x("INSERT OR IGNORE INTO meta VALUES(?,?)", (seen, "1"))
            meta = {"source": f"hook:{harness}", "session": skey, "via": via}
            if o["kind"] == "decision" and via == "llm" and str(o.get("why") or "").strip():
                iid = self.s.record_decision(text, str(o["why"]), agent_id=agent_id, project=project)["id"]
            else:
                iid = self.s.add_item("observation", text, title=f"[{o['kind']}] {text[:70]}", project=project,
                                      agent_id=agent_id, tags=["observation", o["kind"]], meta=meta | ({"files": o["files"]} if o.get("files") else {}))["id"]
                if o["kind"] == "todo":
                    orch = orch or Orchestrator(self.s)
                    try:
                        orch.add_leftover(text, task_type="general", priority=1, project=project, source_id=iid)
                    except Exception:  # noqa: BLE001
                        pass
                elif o["kind"] == "security_alert":
                    try:
                        from .escalation import notify
                        notify(self.s, f"Security observation in {project or '-'}: {text[:200]}")
                    except Exception:  # noqa: BLE001
                        pass
            made.append({"id": iid, "kind": o["kind"]})
            self.s.x("INSERT INTO observations(id,agent_id,project_id,session_key,harness,event,kind,text,meta,ts,processed,item_id) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,1,?)", (new_id("obs"), agent_id, pid, skey, harness, reason, o["kind"],
                                                         text, json.dumps(meta), now(), iid))
        if summary:
            sid = self.s.add_item("observation", summary, title=f"[summary] {summary[:70]}", project=project,
                                  agent_id=agent_id, tags=["observation", "summary"],
                                  meta={"source": f"hook:{harness}", "session": skey, "via": via})["id"]
            made.append({"id": sid, "kind": "summary"})
        if rows:
            ph = ",".join("?" * len(rows))
            self.s.x(f"UPDATE observations SET processed=1 WHERE id IN ({ph})", [r["id"] for r in rows])
        return {"observations": len(made), "via": via, "items": made}

    # ---------------------------------------------------------------------------------------- injection
    def context(self, agent_id: str, project: str | None, max_tokens: int | None = None, facts: bool = True) -> str:
        """The start pack. facts=False leaves Canon's FACTS out: the hook client already put Canon first, from its
        local cache."""
        max_tokens = max_tokens or int(os.environ.get("COMMONS_HOOK_CONTEXT_TOKENS", "1200"))
        from .orchestrator import Orchestrator
        parts, canon = [f"# Commons memory ({project or 'all projects'})"], ""
        try:
            pk = Orchestrator(self.s).pickup(agent_id, project, max_tokens=int(max_tokens * 0.6), facts=facts)
            canon = pk.get("facts") or ""
            if pk["body"] and pk["body"] != "Nothing pending.":
                parts.append(pk["body"])
        except Exception:  # noqa: BLE001
            pass
        rec = self.s.search("", project=project, kinds=["decision", "observation"], limit=8,
                            max_tokens=int(max_tokens * 0.35)) if project else {"text": ""}
        if rec.get("text") and not rec["text"].startswith("("):
            parts.append("Recent decisions/observations (expand ids with get_items):\n" + rec["text"])
        parts.append("Use search/search_index for more; record decisions with record_decision.")
        text = "\n\n".join(parts)
        while estimate_tokens(text) > max_tokens and "\n" in text:
            text = text.rsplit("\n", 1)[0]
        if canon:  # Canon is never cut by the budget: it goes right under the header, whole
            head, _, rest = text.partition("\n\n")
            text = "\n\n".join(x for x in (head, canon, rest) if x)
        return text

    def recall(self, prompt: str, project: str | None, max_tokens: int = 300, skey: str | None = None) -> str:
        if len((prompt or "").split()) < 3:
            return ""
        rep = self._repeated_ask(prompt, project)
        good = self._recall_ids(prompt, project, skey)
        if project and len(good) < 3:  # a homelab question asked inside a code project still deserves the answer
            good += [i for i in self._recall_ids(prompt, None, skey) if i not in good]
        if not good:
            return rep
        r = self.s._budgeted(good[:3], max_tokens, total=len(good))
        return rep + "Possibly relevant Commons memory:\n" + r["text"]

    def _recall_ids(self, prompt: str, project: str | None, skey: str | None) -> list[str]:
        ids, _, why = self.s.rank(prompt, project=project, limit=12)
        if not ids:  # None: project not created yet
            return []
        from .board import _words
        w, ph = _words(prompt), ",".join("?" * len(ids))
        drop = set()
        for r in self.s.q(f"SELECT id, session_id, tags, content FROM items WHERE id IN ({ph})", ids):
            if skey and r["session_id"] == skey:  # this conversation's own turns are already in the agent's context
                drop.add(r["id"])
            elif '"conversation"' in (r["tags"] or "") and '"user"' in (r["tags"] or "") and w:  # someone else asking the same question carries no answer
                o = _words(r["content"])
                if o and len(w & o) / len(w | o) >= 0.6:
                    drop.add(r["id"])
        return [i for i in ids if i not in drop and (
            why.get(i, {}).get("sim", 0) >= float(os.environ.get("COMMONS_HOOK_RECALL_SIM", "0.6"))
            or (why.get(i, {}).get("bm25_rank", 99) <= 2 and why.get(i, {}).get("vec_rank", 99) <= 3))]

    def _repeated_ask(self, prompt: str, project: str | None) -> str:
        """The user is asking for something already on the board and still open: say so before the agent answers."""
        from .board import _words
        w = _words(prompt)
        pid = self.s.project_id(project) if project else None
        hits = []
        rows = self.s.q("SELECT id, text, repeats, project_id FROM board WHERE kind='ask' AND status IN ('open','doing','blocked')")
        for r in (x for x in rows if not pid or x["project_id"] == pid):
            o = _words(r["text"])
            if w and o and len(w & o) / len(w | o) >= 0.5:
                self.s.x("UPDATE board SET repeats=repeats+1, updated_at=? WHERE id=?", (now(), r["id"]))
                hits.append(f"- {r['id']} (now asked {r['repeats'] + 2}x): {r['text'][:200]}")
        return ("ALREADY ASKED AND STILL OPEN on the Commons board; deliver it this time, then board_update with evidence:\n"
                + "\n".join(hits[:3]) + "\n\n") if hits else ""

    def stream(self, project: str | None = None, since: float = 0, limit: int = 100) -> list[dict]:
        args: list = [since]
        sql = "SELECT o.*, a.name AS agent FROM observations o LEFT JOIN agents a ON a.id=o.agent_id WHERE o.ts>?"
        if project:
            pid = self.s.q("SELECT id FROM projects WHERE name=? OR id=?", (project, project))
            sql += " AND o.project_id=?"
            args.append(pid[0]["id"] if pid else "__none__")
        denied = self.s.denied_projects()
        if denied:
            sql += f" AND (o.project_id IS NULL OR o.project_id NOT IN ({','.join('?' * len(denied))}))"
            args += denied
        rows = self.s.q(sql + " ORDER BY o.ts DESC LIMIT ?", args + [limit])
        return [{"id": r["id"], "agent": r["agent"], "kind": r["kind"], "event": r["event"], "harness": r["harness"],
                 "text": r["text"][:300], "ts": r["ts"], "item_id": r["item_id"]} for r in rows]


# ------------------------------------------------------------------------------------------- hook client
def output_for(harness: str, hevent: str, context: str, notice: str = "") -> str:
    """Harness-specific stdout JSON for context injection ('' when nothing to inject). Claude Code also shows `notice`
    to the owner (systemMessage); every harness has it in the context."""
    if not context:
        return ""
    if harness == "cursor":
        return json.dumps({"additional_context": context}) if hevent == "sessionStart" else ""
    if harness in ("claude-code", "codex", "gemini"):
        out = {"hookSpecificOutput": {"hookEventName": hevent, "additionalContext": context}}
        return json.dumps(out | ({"systemMessage": notice} if notice and harness == "claude-code" else {}))
    return context


def replies(tail: str) -> list[str]:
    """The assistant messages in a transcript tail ("user: ..."/"assistant: ..." lines); a message cut off at the
    start of the tail is skipped (it was stored whole by an earlier Stop)."""
    parts = re.split(r"(?m)^(user|assistant): ", tail or "")
    return [parts[i + 1].strip() for i in range(1, len(parts) - 1, 2) if parts[i] == "assistant" and parts[i + 1].strip()]


def transcript_messages(path: str | None) -> list[tuple[str, str]]:
    """(role, text) of the last user/assistant messages in a JSONL transcript."""
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 400_000))
            lines = f.read().decode("utf-8", "replace").splitlines()[1:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            o = json.loads(ln)
        except ValueError:
            continue
        m = o.get("message") or o.get("payload") or o
        role = m.get("role") or o.get("type")
        c = m.get("content")
        if isinstance(c, list):
            c = " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") in ("text", "input_text", "output_text"))
        if role in ("user", "assistant") and isinstance(c, str) and c.strip():
            out.append((role, c.strip()))
    return out


def run_hook(harness: str, hevent: str, stdin_text: str, store_factory=None) -> str:
    """Entry point for ``commons hook``: never raises, never blocks longer than COMMONS_HOOK_TIMEOUT."""
    started = time.monotonic()
    try:
        payload = json.loads(stdin_text or "{}")
    except ValueError:
        payload = {"text": stdin_text}
    event = HARNESS_EVENTS.get(harness, {}).get(hevent) or (hevent if hevent in EVENTS else None)
    if not event:
        return ""
    sid = str(payload.get("session_id") or payload.get("conversation_id") or "")
    cwd = payload.get("cwd") or os.getcwd()
    if event == "session_start":
        try:  # keep the always-loaded Commons instructions in step with the installed version
            from .instructions import refresh
            refresh()
        except Exception:  # noqa: BLE001 - never block a session over this
            pass
        git_changes(cwd, sid, start=True)  # remember where the session began, so its commits count too
    for k in ("prompt", "text"):
        if isinstance(payload.get(k), str):
            payload[k] = strip_private(payload[k])
    if event in ("stop", "session_end", "precompact"):
        msgs = transcript_messages(payload.get("transcript_path"))[-40:]
        raw = strip_private(payload.get("last_assistant_message") or payload.get("prompt_response") or "")
        payload["transcript_tail"] = strip_private("\n".join(f"{r}: {c}" for r, c in msgs))[-12000:] or raw
        payload["replies"] = [strip_private(c) for r, c in msgs if r == "assistant"] or ([raw] if raw.strip() else [])
    if event in ("stop", "session_end"):
        g = git_changes(cwd, sid)
        if g:
            payload["git"] = g
    payload.pop("transcript_path", None)
    payload.setdefault("project", project_from_cwd(payload.get("cwd") or os.getcwd()))
    if event == "stop" and os.environ.get("COMMONS_HOOK_KEEP_WORKING") == "1":
        payload["keep_working"] = int(os.environ.get("COMMONS_HOOK_KEEP_WORKING_MAX", "5"))
    url = os.environ.get("COMMONS_URL", "").rstrip("/")
    timeout = float(os.environ.get("COMMONS_HOOK_TIMEOUT", "2"))
    canon, notice, st, flags = _canon_first(harness, event, payload, sid, cwd, url, store_factory, started)
    payload |= flags  # canon_local (the hub leaves FACTS out), start_pack (a start the hub missed)
    late = bool(payload.get("start_pack"))
    # the harness kills a hook at 5 s, counted from before run_hook: a start, and a prompt (whose Canon may have run
    # git), wait for the hub only for what the budget has left after the interpreter's own start; a late pack, all of it
    if event == "session_start" or st is not None:
        left = QUEUE_HOOK_BUDGET_S - HOOK_START_S - (time.monotonic() - started)
        timeout = max(0.1, left) if late else min(timeout, max(0.1, left))
    if late:  # asked for once: a hook killed while it waits must not leave the next prompt to ask again
        try:
            from .canoncache import save, session
            save(sid, session(sid) | {"start_pending": False})
        except Exception:  # noqa: BLE001
            pass
    if url:
        res = _post(url, harness, event, payload, timeout, started)
    else:
        res = _local(harness, event, payload, store_factory)
    slow = res is SLOW  # the hub has a start it did not answer in time: its pack comes with the next prompt, once
    res = res if isinstance(res, dict) else None  # a reply of any other shape is no reply
    if event == "session_start" and url:
        try:  # Commons still registered as this client's MCP server? (put back if something removed it)
            from .setup import ensure_mcp
            note = ensure_mcp(harness, url)
            if note:
                res = dict(res or {})
                res["context"] = (note + "\n\n" + str(res.get("context") or "")).strip()
        except Exception:  # noqa: BLE001 - never block a session over this
            pass
    if res and event == "session_start" and isinstance(res.get("skills"), list):
        try:
            from .skillsync import write
            write(res["skills"])
        except Exception:  # noqa: BLE001 - never block a session over this
            pass
    if res and res.get("block") and harness == "claude-code" and hevent == "Stop":
        return json.dumps({"decision": "block", "reason": str(res["block"])})  # Claude Code keeps going, not stopping
    ctx = (res or {}).get("context")
    out = output_for(harness, hevent, _join(canon, ctx if isinstance(ctx, str) else ""), notice)
    if st is not None:  # saved only now that the output exists: a hook killed earlier leaves the session unmarked
        if event == "session_start":
            st["start_pending"] = slow  # an unreachable hub is not asked again with every prompt
        elif late:
            st["start_pending"] = False
        try:
            from .canoncache import save
            save(sid, st)
        except Exception:  # noqa: BLE001
            pass
    return out


CONTEXT_MAX = 9500  # Claude Code moves a longer additionalContext to a file and shows the session 2,000 chars of it


def _join(canon: str, ctx: str) -> str:
    """Canon from the cache first and whole (canoncache keeps it under CANON_MAX), then the hub's context, cut at a
    line end to fit."""
    room = CONTEXT_MAX - len(canon) - 2
    if len(ctx) > room:
        ctx = ctx[:room].rsplit("\n", 1)[0] if room > 0 else ""
    return "\n\n".join(x for x in (canon, ctx) if x)


def _canon_first(harness, event, payload, sid, cwd, url, store_factory, started) -> tuple[str, str, dict | None, dict]:
    """Canon from the local state cache (canoncache.py), ahead of the hub's answer and with no hub call: (context, a
    notice for the owner, this session's state for run_hook to save once its output is built, flags for the hub's
    payload). Starts the background pull when one is due, whatever else fails. Never raises."""
    if event not in ("session_start", "prompt") or (harness == "cursor" and event == "prompt"):
        return "", "", None, {}  # Cursor takes hook context only at session start
    if os.environ.get("COMMONS_HOOK_CANON") == "0":
        if event == "session_start":
            try:  # off on this machine: the core block leaves the instruction files too
                from .instructions import canon_core
                canon_core(None)
            except Exception:  # noqa: BLE001
                pass
        return "", "", None, {}
    try:
        from . import canoncache as K
    except Exception:  # noqa: BLE001
        return "", "", None, {}
    due, c, until = False, None, started + QUEUE_HOOK_BUDGET_S - 1.5  # git stops in time to leave the hub its turn
    try:
        due = K.due(K.PULL_START_S if event == "session_start" else K.PULL_PROMPT_S)
        if due and not url:  # a local database: the pull is a few queries, so it runs now
            K.pull(store_factory() if store_factory else None)
        c = K.read()
        if event == "session_start":
            st: dict = {}
            ctx, notice = K.start(c, cwd, st, until)
            flags = {"canon_local": st["canon"]}  # so the hub's pack leaves FACTS out: a fresh Canon is already here
        else:
            st, notice = K.session(sid), ""
            text = str(payload.get("prompt") or payload.get("text") or "")
            relayed = text.lstrip().startswith(HARNESS_NOTICES)  # a background task's report, not the user
            if st:
                ctx = K.prompt(c, st, text, cards=not relayed)
            else:  # no state: this session's start never ran here or was cut off, so its Canon comes now
                ctx, notice = K.start(c, cwd, st, until)
            flags = {"start_pack": True, "canon_local": bool(st.get("canon"))} \
                if st.get("start_pending") and not relayed else {}
    except Exception:  # noqa: BLE001 - Canon must never cost a session its start or a prompt
        return "", "", None, {}
    finally:
        if due and url:
            try:
                K.spawn()
            except Exception:  # noqa: BLE001
                pass
    if event == "session_start":
        try:  # the owner's core in every always-loaded instruction file, for sub-agents, peers and the woken Claude
            from .instructions import canon_core
            canon_core(K.block(c))  # None takes it out: no Canon, or a cache more than 3 days old
        except Exception:  # noqa: BLE001
            pass
    return ctx, notice, st, flags


def git_changes(cwd: str, session: str, start: bool = False) -> dict | None:
    """What this session changed in the git working copy at cwd: commits made since the session began plus anything
    uncommitted (secrets are redacted on the hub). start=True only records the starting commit. COMMONS_HOOK_GIT=0
    turns it off on this machine. Never raises; each git call is capped at 3 seconds."""
    import hashlib
    import subprocess
    from pathlib import Path
    if os.environ.get("COMMONS_HOOK_GIT") == "0" or not cwd:
        return None

    def git(*a):
        r = subprocess.run(["git", "-C", top, *a], capture_output=True, text=True, timeout=3)
        return r.stdout if r.returncode == 0 else ""
    try:
        top = cwd
        top = git("rev-parse", "--show-toplevel").strip()
        if not top:
            return None
        head = git("rev-parse", "HEAD").strip()
        d = Path(os.environ.get("COMMONS_HOME", Path.home() / ".commons")) / "git-start"
        mark = d / (hashlib.sha1(f"{session}:{top}".encode()).hexdigest()[:20] + ".txt")
        if start:
            if session and head:
                d.mkdir(parents=True, exist_ok=True)
                mark.write_text(head)
            return None
        base = (mark.read_text().strip() if session and mark.exists() else "") or head
        if not base:
            return None
        diff = git("diff", base)
        untracked = git("ls-files", "--others", "--exclude-standard").splitlines()[:100]
        if not diff.strip() and not untracked and head == base:
            return None
        m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?$", git("remote", "get-url", "origin").strip())
        return {"repo": m.group(1) if m else "", "branch": git("rev-parse", "--abbrev-ref", "HEAD").strip(),
                "commit": head if head != base else "", "base": base, "stat": git("diff", "--stat", base)[-4000:],
                "diff": diff[:200_000], "untracked": untracked}
    except Exception:  # noqa: BLE001 - not a repo, no git, a slow disk: the hook carries on without it
        return None


def _key() -> str:
    k = os.environ.get("COMMONS_API_KEY")
    if k:
        return k
    agent = os.environ.get("COMMONS_AGENT", "")
    from pathlib import Path
    p = Path.home() / ".commons" / "agents" / f"{agent}.key"
    return p.read_text().strip() if agent and p.exists() else ""


QUEUE_DRAIN_EVENTS = 25  # most events one hook sends for the queue, so a backlog never holds up the agent
QUEUE_HOOK_BUDGET_S = 4  # harnesses kill a hook at 5 s: draining, a start's wait and its git calls stop before that
HOOK_START_S = 0.7  # the interpreter starts before run_hook does (longer under load): Canon paths leave it this much
SLOW = object()  # _post: the hub has the event and did not answer in time (not: the hub could not be reached)
QUEUE_CLAIM_STALE_S = 60  # a claim this old belongs to a hook that died


def _queue_path():
    from pathlib import Path
    d = Path(os.environ.get("COMMONS_HOME", Path.home() / ".commons"))
    d.mkdir(parents=True, exist_ok=True)
    return d / "hook-queue.jsonl"


def _send(url, body, timeout):
    import urllib.request
    req = urllib.request.Request(url + "/agent/hook", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {_key()}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _queue_cap() -> int:
    return int(float(os.environ.get("COMMONS_HOOK_QUEUE_MAX_MB", "50")) * 2 ** 20)


def _enqueue(q, data: bytes) -> None:
    """One O_APPEND write per event, so hooks running at once cannot interleave lines. A full queue drops the new
    event: losing a capture is better than a file that grows until reading it takes the machine down."""
    try:
        if q.stat().st_size + len(data) > _queue_cap():
            return
    except OSError:
        pass
    fd = os.open(q, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _claim_queue(q):
    """Take a batch by renaming it, so exactly one hook holds it; returns (claimed file, offset to resume from). Order:
    a batch parked by an earlier drain, one left by a hook killed mid-drain (once it is old enough), then the live
    queue. Batches stay in their file with the offset in the name, so nothing is ever copied or rewritten."""
    def take(path, offset):
        own = q.with_name(f"{q.name}.{offset}.{os.getpid()}.{time.time_ns()}.draining")
        try:
            os.utime(path)  # before the rename: a rename keeps the old time and the claim would look abandoned
            os.replace(path, own)
        except OSError:
            return None  # another hook took it first
        return own, offset
    for held in q.parent.glob(q.name + ".*.backlog"):
        got = take(held, int(held.name[len(q.name) + 1:].split(".")[0]))
        if got:
            return got
    for held in q.parent.glob(q.name + ".*.draining"):
        try:
            stale = time.time() - held.stat().st_mtime > QUEUE_CLAIM_STALE_S
        except OSError:
            continue
        got = take(held, int(held.name[len(q.name) + 1:].split(".")[0])) if stale else None
        if got:
            return got
    try:
        return take(q, 0) if q.stat().st_size else None
    except OSError:
        return None


def _drain(url, q, timeout):
    """Send a bounded slice of the offline queue, reading it line by line: memory stays flat at any queue size, and the
    hook stays inside its time limit. Corrupt lines and events the hub refuses are dropped, because keeping them would
    stop every later drain at the same place. What was not sent stays parked for the next hook."""
    import urllib.error
    claimed = _claim_queue(q)
    if claimed is None:
        return
    claim, offset = claimed
    deadline = time.monotonic() + timeout
    with open(claim, "rb") as f:
        f.seek(offset)
        sent = 0
        while sent < QUEUE_DRAIN_EVENTS and time.monotonic() < deadline:
            start = f.tell()
            ln = f.readline()
            if not ln:
                break
            try:
                body = json.loads(ln)
            except ValueError:
                continue
            if not isinstance(body, dict):
                continue
            try:
                _send(url, body, max(0.1, deadline - time.monotonic()))
                sent += 1
            except TimeoutError:  # the hub has it and is slow (a stop with a diff takes 10 s or more): do not resend
                sent += 1
            except urllib.error.HTTPError as e:
                if e.code in (400, 404, 413, 422):
                    continue
                f.seek(start)
                break
            except Exception:  # noqa: BLE001 - offline again: the rest waits for the next hook
                f.seek(start)
                break
        pos = f.tell()
    if pos >= os.path.getsize(claim):
        os.unlink(claim)
    else:
        os.replace(claim, q.with_name(f"{q.name}.{pos}.{os.getpid()}.{time.time_ns()}.backlog"))


def _post(url, harness, event, payload, timeout, started=None):
    q = _queue_path()
    body = {"harness": harness, "event": event, "payload": payload, "queued_at": time.time()}
    try:
        res = _send(url, body, timeout)
    except TimeoutError:  # urllib raises this bare only once the request is sent: the hub has it and is just slow
        return SLOW
    except Exception:  # noqa: BLE001 - offline: queue for later, never block the agent
        if event != "session_start":  # a start is held for its own session instead: its pack comes with the next prompt
            _enqueue(q, (json.dumps(body) + "\n").encode())
        return None
    spare = QUEUE_HOOK_BUDGET_S - (time.monotonic() - (started or time.monotonic()))
    if spare > 0:  # only what the hook has left: a harness kill would lose this hook's own output
        _drain(url, q, min(timeout, spare))
    return res


def _local(harness, event, payload, store_factory):
    s = store_factory() if store_factory else Store()
    agent = os.environ.get("COMMONS_AGENT") or harness
    aid = s.ensure_agent(agent)
    return Capture(s).event(aid, event, payload, harness=harness)


# ------------------------------------------------------------------------------------------- installers
HOOK_TARGETS = {
    "claude-code": ("~/.claude/settings.json", ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd", "PreCompact"]),
    "codex": ("~/.codex/hooks.json", ["SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "SessionEnd", "PreCompact"]),
    "gemini": ("~/.gemini/settings.json", ["SessionStart", "BeforeAgent", "AfterTool", "AfterAgent", "SessionEnd", "PreCompress"]),
    "cursor": ("~/.cursor/hooks.json", ["sessionStart", "beforeSubmitPrompt", "postToolUse", "afterFileEdit", "stop", "sessionEnd", "preCompact"]),
}
MARK = "commons hook "


def install_hooks(harness: str, command: str = "commons", path: str | None = None, uninstall: bool = False) -> dict:
    if harness not in HOOK_TARGETS:
        raise CommonsError(f"unknown harness {harness!r}; one of {', '.join(HOOK_TARGETS)}")
    p = os.path.expanduser(path or HOOK_TARGETS[harness][0])
    try:
        cfg = json.load(open(p)) if os.path.exists(p) else {}
    except ValueError as e:
        raise CommonsError(f"{p} is not valid JSON ({e}); fix or move it first") from e
    if harness == "cursor":
        cfg.setdefault("version", 1)
    hooks = cfg.setdefault("hooks", {})
    changed = []
    for ev in HOOK_TARGETS[harness][1]:
        cmd = f"{command} hook {harness} {ev}"
        lst = hooks.get(ev, [])
        if harness == "cursor":
            lst = [h for h in lst if MARK not in h.get("command", "")]
            if not uninstall:
                lst.append({"command": cmd, "timeout": 5})
        else:
            lst = [g for g in lst if not any(MARK in h.get("command", "") for h in g.get("hooks", []))]
            if not uninstall:
                h = {"type": "command", "command": cmd, "timeout": 5000 if harness == "gemini" else 5}
                if harness == "gemini":
                    h["name"] = "commons-memory"
                lst.append({"matcher": "*", "hooks": [h]} if ev in ("PostToolUse", "AfterTool") else {"hooks": [h]})
        if lst:
            hooks[ev] = lst
        else:
            hooks.pop(ev, None)
        changed.append(ev)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if os.path.exists(p):
        import shutil
        shutil.copy(p, p + ".bak")
    with open(p, "w") as f:
        json.dump(cfg, f, indent=2)
    return {"harness": harness, "path": p, "events": changed, "removed" if uninstall else "installed": True}
