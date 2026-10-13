"""Any model uses skills straight from Commons (nothing to install), and when an agent can't run one (a tool, access or
environment it lacks, or a skill that only exists on another agent's machine), it asks an agent that can.

Helpers, best first: agents that used the skill successfully, its author, agents that list it as one of their local
skills (set_profile(skills=[...])); live agents before ones that only run when their user opens them."""
from __future__ import annotations

import json

from .store import CommonsError, Store


def declared(s: Store, name: str) -> list[str]:
    out = []
    for a in s.q("SELECT id, profile FROM agents WHERE profile LIKE ?", (f'%"{name}"%',)):
        if name in (json.loads(a["profile"] or "{}").get("skills") or []):
            out.append(a["id"])
    return out


def helpers(s: Store, name: str, requester: str | None) -> list[str]:
    from .messaging import LIVE_MODES, presence
    sk = s.q("SELECT id, author_agent FROM skills WHERE name=?", (name,))
    ranked: list[str] = []
    if sk:
        ranked += [r["agent_id"] for r in s.q("SELECT agent_id, SUM(success) ok FROM skill_usage WHERE skill_id=? AND "
                                              "agent_id IS NOT NULL GROUP BY agent_id HAVING SUM(success)>0 ORDER BY ok DESC",
                                              (sk[0]["id"],))]
        ranked += [sk[0]["author_agent"]] if sk[0]["author_agent"] else []
    ranked += declared(s, name)
    seen, out = set(), []
    for a in ranked:
        if a and a != requester and a not in seen and s.get_agent(a) and s.label(a)[1] not in ("user", "cli", "dashboard-user"):
            seen.add(a)
            out.append(a)
    live = [a for a in out if presence(s, a)["wake_mode"] in LIVE_MODES]
    return live + [a for a in out if a not in live]


def ask(s: Store, requester: str, name: str, task: str, why: str = "") -> dict:
    if not (task or "").strip():
        raise CommonsError("say what you need done with the skill (task)")
    hs = helpers(s, name, requester)
    if not hs:
        from .skillpool import SkillPool
        near = [x["name"] for x in SkillPool(s).list(f"{name.replace('-', ' ')} {task}", max_tokens=200)["skills"][:3]]
        raise CommonsError(f"no agent has used or declared skill {name!r} yet. "
                           + (f"Similar skills in the pool: {', '.join(near)} (load one with skill(name=...)). " if near else "")
                           + "If the task itself still needs doing, post it with msg_send(type='task') so the best live "
                             "agent takes it, or ask the user")
    from .orchestrator import Orchestrator
    in_pool = bool(s.q("SELECT 1 FROM skills WHERE name=? AND active_version IS NOT NULL", (name,)))
    body = (f"{s.label(requester)[1]} asks for help with the skill '{name}'"
            + (f" (they can't run it: {why})" if why else "") + f".\nTask: {task}\n"
            + (f"Load the skill with skill(name='{name}') and do the task, then complete with the result."
               if in_pool else f"'{name}' is one of your local skills: run it for them and complete with the result."))
    d = Orchestrator(s).delegate(requester, s.label(hs[0])[1], body, f"skill help: {name}: {task[:60]}", plan=False)
    s.audit(requester, "skill.help", name, {"helper": s.label(hs[0])[1], "job": d["job_id"]})
    return d | {"helper": d["assigned_to"], "candidates": [s.label(a)[1] for a in hs[:5]]}
