"""API seats: cheap, mid and top models from your own API keys that take task cards when no subscription agent is free.

One virtual agent, `api-seats`, whose models are the three tiers (Setup: seat_models, else the provider key you added
first: Anthropic Haiku, Sonnet and Opus). The router treats it like any agent with several models, so a low-criticality
card gets the cheap model and a merge or a review the top one. Allocation falls back to it only when no subscription
agent can take the step right now (offline, out of usage, or none good at it), and never for steps that need a repo
(code, test, ship, or a card that requires evidence): an API model can't commit or run tests. The hub runs it in its own
threads when api_seats is on (off by default: it spends money on your key), stops for 24 hours of spend at
seat_daily_cap dollars, and tells you when it does.
"""
from __future__ import annotations

import json
import os
import threading

from .ids import iso, now
from .store import Store

AGENT = "api-seats"
TIERS = ("cheap", "mid", "top")
DEFAULTS = {"anthropic": ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5")}
NO_SEAT = {"code", "test", "ship", "owner", "tally", "debug"}  # work that needs a repo, a shell or the owner
SKILLS = {"spec": 0.75, "plan": 0.75, "review": 0.75, "research": 0.6, "summarize": 0.8, "docs": 0.8, "judge": 0.75,
          "debate": 0.7, "vote": 0.7, "red_team": 0.7, "general": 0.65, "librarian": 0.7}
_threads: dict = {}


def models(s: Store) -> list[str]:
    """[cheap, mid, top] model ids: the seat_models setting, else the defaults of the first provider key you added."""
    from . import settings
    own = settings.get(s, "seat_models")
    if len(own) == 3:
        return list(own)
    for r in s.q("SELECT name, kind FROM providers WHERE enabled=1 ORDER BY created_at, name"):
        if r["kind"] in DEFAULTS:
            return [f"{r['name']}/{m}" for m in DEFAULTS[r["kind"]]]
    return []


def spent(s: Store, aid: str | None = None) -> float:
    aid = aid or (s.get_agent(AGENT) or {}).get("id")
    if not aid:
        return 0.0
    return float(s.q("SELECT COALESCE(SUM(cost),0) AS c FROM usage WHERE agent_id=? AND ts>?", (aid, now() - 86400))[0]["c"])


def capped(s: Store, aid: str | None = None) -> bool:
    from . import settings
    return spent(s, aid) >= float(settings.get(s, "seat_daily_cap"))


def sync(s: Store) -> str | None:
    """Keep the seat agent's profile and models in line with the settings. Returns its id while it may take work."""
    from . import settings
    from .orchestrator import Profiles
    from .providers import ProviderLayer
    ms = models(s) if settings.get(s, "api_seats") else []
    a = s.get_agent(AGENT)
    if not ms:
        if a and a.get("profile"):  # off, or no key: never ranked for work
            s.x("UPDATE agents SET profile=NULL WHERE id=?", (a["id"],))
        return None
    aid = a["id"] if a else s.ensure_agent(AGENT)
    want = {"expertise": SKILLS, "cost": 5, "max_concurrent": int(os.environ.get("COMMONS_SEAT_PARALLEL", "2"))}
    prof = json.loads((s.get_agent(aid) or {}).get("profile") or "{}")
    if any(prof.get(k) != v for k, v in want.items()):
        Profiles(s).set(aid, platform="api", notes="your API keys, used when no subscription agent is free", **want)
    if sorted(r["model_id"] for r in s.q("SELECT model_id FROM agent_models WHERE agent_id=?", (aid,))) != sorted(ms):
        ProviderLayer(s).set_agent_models(aid, ms)
    return aid


def fallback(s: Store, st: dict, ranked: list) -> list:
    """The seat stays a candidate only for work an API model can do, under the cap, when nobody else is free."""
    seat = next((a for _sc, a in ranked if a["name"] == AGENT), None)
    if not seat:
        return ranked
    from .messaging import LIVE_MODES, presence
    from .sdlc import card
    others = [(sc, a) for sc, a in ranked if a["name"] != AGENT]
    if st["type"] in NO_SEAT or card(s, st["id"]).get("evidence") or capped(s, seat["id"]):
        return others
    free = [a for _sc, a in others if (lambda p: p["state"] in ("online", "idle") or
                                       (p.get("ready") and p.get("wake_mode") in LIVE_MODES))(presence(s, a["id"]))]
    return others if free else ranked


def run_once(s: Store) -> int:
    """Claim and run the ready work assigned to the seat (a few at a time). Returns how many it finished."""
    from .orchestrator import Orchestrator
    aid = sync(s)
    if not aid:
        return 0
    if capped(s, aid):
        _tell_cap(s)
        return 0
    from .messaging import heartbeat
    heartbeat(s, aid, status="online", client="seats")  # live while on, so nothing treats its work as abandoned
    o = Orchestrator(s)
    ready = [st for st in o.claimable(aid, 20) if st["assigned_agent"] == aid]
    cap = int(os.environ.get("COMMONS_SEAT_PARALLEL", "2"))
    if not ready:
        return 0
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=cap) as pool:
        return sum(pool.map(lambda st: _run(s, o, aid, st["id"]), ready[:cap]))


def _run(s: Store, o, aid: str, sid: str) -> int:
    from .providers import ProviderLayer
    from .worker import parse_output, render_prompt
    if capped(s, aid):
        return 0
    p = o.claim(aid, sid)
    if not p:
        return 0
    model = p.get("model") or models(s)[1]
    prompt = render_prompt(p)
    pre = (p.get("context") or {}).get("prefix") or ""
    try:
        r = ProviderLayer(s).chat(model, prompt[len(pre):] if pre and prompt.startswith(pre) else prompt,
                                  cache_prefix=pre, max_tokens=8000, role="seat", agent_id=aid, job_id=p["job_id"],
                                  effort=p.get("effort"), record=False,
                                  system="You are an expert agent in a Commons team, working from the brief alone: "
                                         "you have no shell, repository or tools. Be concrete and complete.")
        out = parse_output(r["text"])
        o.complete(sid, aid, r["text"], verdict=out.get("verdict"), tokens=r["tokens_out"], tokens_in=r["tokens_in"],
                   cost=r["cost"], model=model, leftovers=out["leftovers"] or None,
                   preferences=out["preferences"] or None)
        return 1
    except Exception as e:  # noqa: BLE001 - give the step back (retried or moved) instead of holding it
        s.audit(aid, "seat.error", sid, f"{model}: {str(e)[:300]}")
        try:
            o.complete(sid, aid, f"api seat failed: {str(e)[:300]}", success=False)
        except Exception:  # noqa: BLE001
            pass
        return 0


def _tell_cap(s: Store) -> None:
    from . import settings
    from .escalation import notify
    day = iso(now())[:10]
    if s.q("SELECT 1 FROM meta WHERE key=?", (f"seatcap:{day}",)):
        return
    s.x("INSERT OR REPLACE INTO meta VALUES(?, '1')", (f"seatcap:{day}",))
    notify(s, f"API seats reached ${settings.get(s, 'seat_daily_cap'):.2f} in 24 hours",
           "They pause until the oldest spend ages out; raise seat_daily_cap on Setup to keep going.", level="warn")


def status(s: Store) -> dict:
    from . import settings
    a = s.get_agent(AGENT)
    aid = a["id"] if a else None
    done = s.q("SELECT COUNT(*) AS n FROM usage WHERE agent_id=? AND ts>? AND cost>0", (aid, now() - 86400))[0]["n"] if aid else 0
    return {"enabled": bool(settings.get(s, "api_seats")), "models": dict(zip(TIERS, models(s))) if models(s) else {},
            "spent_24h": round(spent(s, aid), 4), "cap": float(settings.get(s, "seat_daily_cap")),
            "capped": capped(s, aid) if aid else False, "runs_24h": done}


def start(s: Store) -> threading.Thread | None:
    """One seat loop per database in the serving process; it idles while api_seats is off."""
    from .background import _dbkey
    key = _dbkey(s)
    if key in _threads or os.environ.get("COMMONS_SEATS") in ("0", "off"):
        return _threads.get(key)
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                run_once(s)
            except Exception:  # noqa: BLE001
                pass
            stop.wait(float(os.environ.get("COMMONS_SEAT_POLL", "5")))
    t = threading.Thread(target=loop, name="commons-seats", daemon=True)
    t.stop = stop  # type: ignore[attr-defined]
    t.start()
    _threads[key] = t
    return t
