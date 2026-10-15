"""Nothing waits forever: detect unassignable / stuck subtasks and escalate.

Ladder (each step is recorded in `escalations` and on the subtask, never repeated):
  1. reassign  — the assigned agent is stale/offline, or no profiled agent fits: assign the best *active* agent
                 (any agent seen recently, profile or not), still honouring author/self-review exclusions.
  2. llm       — no usable agent: if a distributor (or planner) model is configured, Commons executes the subtask
                 itself through the provider layer as agent `commons-distributor` (budget caps still apply).
  3. notify    — nobody can do it: a notification is raised for the user (dashboard + inbox for agent `user`).
A subtask is "stuck" when it is ready (deps done), unclaimed, and older than COMMONS_ESCALATE_AFTER seconds
(default 900). Unassignable subtasks (no candidate at all) escalate immediately.
"""
from __future__ import annotations

import json
import os

from .ids import new_id, now
from .store import Store

ACTIVE_WINDOW = 24 * 3600


def notify(s: Store, title: str, body: str = "", level: str = "warn", target: str = "") -> str:
    nid = new_id("ntf")
    s.x("INSERT INTO notifications(id,ts,level,title,body,target,read) VALUES(?,?,?,?,?,?,0)",
        (nid, now(), level, title, body[:2000], target))
    try:
        s.post_task(None, "user", f"[{level}] {title}\n{body}"[:2000], kind="notification")
    except Exception:  # noqa: BLE001
        pass
    hook = os.environ.get("COMMONS_NOTIFY_WEBHOOK")  # Slack/Discord/ntfy-compatible JSON POST
    if hook:
        import threading
        import urllib.request

        def _post():
            try:
                data = json.dumps({"text": f"[commons {level}] {title}\n{body[:1500]}", "title": title,
                                   "level": level, "id": nid}).encode()
                urllib.request.urlopen(urllib.request.Request(hook, data=data, headers={"Content-Type": "application/json"}),
                                       timeout=5)  # noqa: S310 - user-configured URL
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=_post, daemon=True).start()
    return nid


def notifications(s: Store, unread_only: bool = False, limit: int = 50) -> list[dict]:
    return [dict(r) for r in s.q("SELECT * FROM notifications" + (" WHERE read=0" if unread_only else "")
                                 + " ORDER BY ts DESC LIMIT ?", (limit,))]


class Escalator:
    def __init__(self, store: Store, after: float | None = None):
        self.s = store
        self.after = after if after is not None else float(os.environ.get("COMMONS_ESCALATE_AFTER", "900"))
        from .orchestrator import Orchestrator
        self.o = Orchestrator(store)

    def _log(self, st: dict, level: str, action: str, detail: dict) -> None:
        self.s.x("INSERT INTO escalations VALUES(?,?,?,?,?,?,?,?)",
                 (new_id("esc"), st["id"], st["job_id"], level, action, json.dumps(detail, default=str), now(), None))
        self.s.x("UPDATE subtasks SET escalation=?, updated_at=? WHERE id=?", (level, now(), st["id"]))
        self.s.audit(None, f"escalate.{level}", st["id"], detail)

    def _active_agents(self, st: dict) -> list[dict]:
        excl = set(json.loads(st["excluded_agents"] or "[]")) | set(self.o._author_of_chain(st))
        rows = self.s.q("SELECT * FROM agents WHERE last_seen>=? AND name NOT IN ('user','cli','commons-distributor')",
                        (now() - ACTIVE_WINDOW,))
        out = []
        for a in rows:
            prof = json.loads(dict(a).get("profile") or "{}")
            if a["id"] in excl or prof.get("availability") in ("offline", "unavailable", "paused"):
                continue
            out.append(dict(a))
        return sorted(out, key=lambda a: -(a["last_seen"] or 0))

    def stuck(self) -> list[dict]:
        rows = [dict(r) for r in self.s.q(
            "SELECT s.* FROM subtasks s JOIN jobs j ON j.id=s.job_id WHERE s.status='pending' AND j.status='running' "
            "AND s.type NOT IN ('tally','owner')")]  # owner steps wait on the user's board question
        out = []
        for st in rows:
            if not self.o._deps_ok(st):
                continue
            unassignable = not self.o.profiles.rank(st["type"], json.loads(st["excluded_agents"] or "[]")
                                                     + self.o._author_of_chain(st))
            stale_assignee = False
            if st["assigned_agent"]:
                a = self.s.q("SELECT last_seen, profile FROM agents WHERE id=?", (st["assigned_agent"],))
                stale_assignee = not a or (a[0]["last_seen"] or 0) < now() - ACTIVE_WINDOW or \
                    json.loads(a[0]["profile"] or "{}").get("availability") in ("offline", "unavailable", "paused")
            age = now() - (st["updated_at"] or st["created_at"])
            if unassignable or stale_assignee or age >= self.after:
                out.append(st | {"unassignable": unassignable, "stale_assignee": stale_assignee, "age": age})
        return out

    def run(self) -> list[dict]:
        if self.o.paused():
            return []
        done = []
        for st in self.stuck():
            level = st.get("escalation")
            if level == "notified":
                continue
            if level is None:
                cands = [a for a in self._active_agents(st) if a["id"] != st["assigned_agent"]]
                if cands:
                    prev = self.s.label(st["assigned_agent"])[1] if st["assigned_agent"] else None
                    why = ("no agent with this expertise was free" if st["unassignable"] else
                           f"{prev}, who had it, has gone quiet or is paused" if st["stale_assignee"] and prev else
                           f"nobody claimed it in {int(st['age'] // 60)} minutes")
                    self.s.x("UPDATE subtasks SET assigned_agent=? WHERE id=?", (cands[0]["id"], st["id"]))
                    self.s.relate(st["id"], cands[0]["id"], "assigned_to")
                    self.s.post_task(None, cands[0]["name"], f"Escalated subtask {st['id']} ({st['type']}): {st['title']}. "
                                     f"Why you: {why}, and you are the next active agent for {st['type']} work. Claim it "
                                     f"with claim_subtask('{st['id']}'); it is assigned to you, which pickup() confirms.",
                                     kind="task", by="the escalation ladder: stuck work reassigned")
                    self._log(st, "reassigned", "reassign", {"to": cands[0]["name"], "unassignable": st["unassignable"]})
                    done.append({"subtask": st["id"], "action": "reassigned", "to": cands[0]["name"]})
                    continue
                level = "reassigned"  # nobody to reassign to: fall through
            if level == "reassigned":
                r = self._llm(st)
                if r:
                    done.append(r)
                    continue
            notify(self.s, f"Subtask needs a human or an agent: {st['title'][:80]}",
                   f"Subtask {st['id']} ({st['type']}) in job {st['job_id']} has waited {int(st['age'])}s with no agent "
                   f"able to take it. Connect an agent with this expertise, configure a distributor model "
                   f"(models role distributor <model>), or cancel the job.", target=st["id"])
            self._log(st, "notified", "notify", {"age": st["age"]})
            done.append({"subtask": st["id"], "action": "notified"})
        return done

    def _llm(self, st: dict) -> dict | None:
        from .providers import ProviderLayer
        pl = ProviderLayer(self.s)
        model = pl.role_model("distributor") or pl.role_model("planner", preset=False)  # doing work needs a model you chose
        if not model:
            return None
        aid = self.s.ensure_agent("commons-distributor")
        pk = self.o.claim(aid, st["id"])
        if not pk:
            return None
        from .worker import parse_output, render_prompt
        try:
            r = pl.chat(model, render_prompt(pk), system="You are the Commons distributor executing an orphaned subtask.",
                        max_tokens=2000, role="distributor", agent_id=aid, job_id=st["job_id"])
            out = parse_output(r["text"])
            self.o.complete(st["id"], aid, r["text"], verdict=out.get("verdict"), leftovers=out.get("leftovers"),
                            tokens_in=r["tokens_in"], tokens=r["tokens_out"], cost=r["cost"], model=model)
        except Exception as e:  # noqa: BLE001
            self.s.x("UPDATE subtasks SET status='pending', claimed_by=NULL WHERE id=? AND status='claimed'", (st["id"],))
            self.s.audit(None, "escalate.llm.error", st["id"], str(e)[:200])
            return None
        self._log(st, "llm", "distributor", {"model": model})
        return {"subtask": st["id"], "action": "llm", "model": model}
