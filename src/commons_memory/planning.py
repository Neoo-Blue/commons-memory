"""Who splits a job into parts, and how: the plan chain (a Setup setting, like the decision chain).

Entries are tried in order: role:planner (the model set for the planner or distributor role), any provider/model the
hub has a key for (default anthropic/claude-haiku-5-5), agent (the best live agent at planning writes the plan as the
job's first step, on its own subscription, and Commons expands its answer into the parts), rules (the keyword
planner, always available). Every planner sees the team: who is live, what each agent is good at and how much usage
it has left, so it sizes the parts to the team. The router still assigns every part.
"""
from __future__ import annotations

import json
import re

from .store import Store

TYPES = ("research", "plan", "code", "review", "test", "red_team", "debate", "judge", "summarize", "write", "general")
FORMAT = ("Return ONLY a JSON array of {\"key\",\"type\",\"title\",\"body\",\"deps\":[keys],\"gates\":[keys]}. "
          f"Types: {', '.join(TYPES)}. Put review/test/red_team keys in the \"gates\" of the work they check. Each body "
          "must stand on its own (an agent sees only its part and the results of the parts it depends on) and stay "
          "under 120 words. Don't name "
          "agents: Commons assigns each part to the agent best at it with usage to spare. A small task is one node.")
SYSTEM = "ROLE: planner. You split work for a team of AI agents in Commons. Follow the output format exactly."


def roster(s: Store) -> list[dict]:
    """The team as a planner sees it: live state, strengths, and usage left (agents that declared a profile)."""
    from . import quota as Q
    from .messaging import LIVE_MODES, presence
    out = []
    for a in s.q("SELECT id, name, profile FROM agents WHERE profile IS NOT NULL AND profile NOT IN ('', '{}')"):
        exp = json.loads(a["profile"] or "{}").get("expertise") or {}
        pr = presence(s, a["id"])
        room, _ = Q.headroom(s, a["id"])
        out.append({"agent": a["name"], "state": "live" if pr["wake_mode"] in LIVE_MODES else pr["state"],
                    "good_at": dict(sorted(exp.items(), key=lambda x: -x[1])[:4]),
                    "usage_left": "unknown" if room is None else f"{round(room * 100)}%"})
    return out


def prompt(s: Store, task: str) -> str:
    team = roster(s)
    live = sum(t["state"] == "live" for t in team)
    return (f"Split this task into a DAG of subtasks. {live} agent(s) are live now; don't plan more parallel parts than "
            f"that unless the parts are small.\n{FORMAT}\nTEAM: {json.dumps(team)}\nTASK:\n{task}")


def parse(text: str) -> list[dict] | None:
    """A planner's answer -> a valid DAG (keys unique, deps/gates known, acyclic, at most 20 parts), else None."""
    from .orchestrator import _check_acyclic
    try:
        dag = json.loads(re.search(r"\[.*\]", text or "", re.S).group(0))
        assert isinstance(dag, list) and 0 < len(dag) <= 20 and all(isinstance(d, dict) for d in dag)
        keys = [str(d.get("key") or "") for d in dag]
        assert all(keys) and len(set(keys)) == len(keys)
        for d in dag:
            d["key"] = str(d["key"])
            d["deps"] = [str(k) for k in d.get("deps") or []]
            d["gates"] = [str(k) for k in d.get("gates") or []]
            assert set(d["deps"] + d["gates"]) <= set(keys) and (d.get("title") or d.get("body"))
            d["type"] = d.get("type") if d.get("type") in TYPES else "general"
            d.pop("agent", None), d.pop("assign", None)  # the router assigns parts, not the planner
        _check_acyclic(dag)
        return dag
    except Exception:  # noqa: BLE001 - anything malformed falls through to the next planner
        return None


def llm(s: Store, model: str, task: str) -> list[dict] | None:
    from .providers import ProviderLayer
    try:
        out = ProviderLayer(s).chat(model, prompt(s, task), system=SYSTEM, max_tokens=8000, role="planner")
    except Exception as e:  # noqa: BLE001 - no key, quota, outage: the next entry plans
        s.audit(None, "plan.error", model, str(e)[:200])
        return None
    dag = parse(out.get("text") or "")
    if not dag:  # usually a plan cut off at max_tokens; the next entry plans instead
        s.audit(None, "plan.unparsed", model, (out.get("text") or "")[-300:])
    return dag


def agent_brief(s: Store, task: str) -> str:
    return ("Plan this job for the team; Commons turns your answer into the parts and assigns each one.\n"
            + prompt(s, task))


def looks_big(task: str) -> bool:
    """Worth planning before handing it to one agent: a list of parts, or a long brief."""
    from .orchestrator import _items
    return len(_items(task)) >= 2 or len((task or "").split()) > 150
