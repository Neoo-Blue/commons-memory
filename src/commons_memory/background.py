"""Background maintenance, shared by `commons scheduler` and the in-process thread.

`tick()` runs one maintenance pass: due recurring runs (with catch-up policy), lease reaping, escalation of stuck or
unassignable subtasks, dirty KB rebuilds, incremental librarian passes, model-list refresh, routing validation,
skill discovery, retention and scheduled backups. Each job has its own interval; state is kept in `meta` so the
in-process thread and an external scheduler do not double-run.

`start_background(store)` starts a daemon thread (used by `commons serve` and `commons dashboard`) that rebuilds
dirty KB pages after the debounce and runs the librarian incrementally after writes, so pages stay fresh without
a separate scheduler. Set COMMONS_BACKGROUND=full to run every job in-process, or 0 to disable.
"""
from __future__ import annotations

import json
import os
import threading
import time

from .ids import now
from .store import Store

INTERVALS = {  # seconds (env COMMONS_EVERY_<NAME> overrides)
    "recurring": 30, "reap": 30, "escalate": 60, "kb": 5, "librarian": 600, "models": 3600, "routing_validate": 3600,
    "discovery": 86400, "prune": 86400, "backup": 86400, "wake": 5, "a2a_push": 3, "reindex": 60, "tiers": 3600,
    "quota_wake": 60, "schedules": 60, "release": 300, "events": 300, "graphiti": 300,
}
LIGHT = ("kb", "librarian", "reap", "wake", "a2a_push", "reindex", "tiers", "quota_wake", "schedules", "release", "events", "graphiti")


def _every(name: str) -> float:
    return float(os.environ.get(f"COMMONS_EVERY_{name.upper()}", INTERVALS[name]))


def _due(s: Store, name: str, force: bool = False) -> bool:
    every = _every(name)
    if every <= 0:
        return False
    key = f"bg_last:{name}"
    s.x("INSERT OR IGNORE INTO meta VALUES(?, '0')", (key,))
    r = s.q("SELECT value FROM meta WHERE key=?", (key,))
    last = r[0]["value"] if r else "0"
    if force or now() - float(last) >= every:  # compare-and-swap: of two loops (or processes) only one claims the run
        with s.lock:
            return s.db.execute("UPDATE meta SET value=? WHERE key=? AND value=?", (str(now()), key, last)).rowcount == 1
    return False


_ME = f"{os.getpid()}@{__import__('socket').gethostname()}"


def leader(s: Store, ttl: float = 60) -> bool:
    """Only one process per database runs maintenance (several `commons serve` processes may share one DB/Postgres).
    A lease row in meta; whoever holds an unexpired lease keeps it, otherwise the first to write wins."""
    t = time.time()
    with s.lock:
        r = s.q("SELECT value FROM meta WHERE key='bg_leader'")
        cur = json.loads(r[0]["value"]) if r else None
        if cur and cur.get("holder") != _ME and cur.get("until", 0) > t:
            return False
        s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", ("bg_leader", json.dumps({"holder": _ME, "until": t + ttl})))
        r = s.q("SELECT value FROM meta WHERE key='bg_leader'")
        return json.loads(r[0]["value"]).get("holder") == _ME


def tick(s: Store, jobs: tuple[str, ...] | None = None, force: bool = False, log=None) -> dict:
    log = log or (lambda *_: None)
    if not force and not leader(s):
        return {"skipped": "another process holds the maintenance lease"}
    jobs = jobs or tuple(INTERVALS)
    out: dict = {}

    def run(name, fn):
        if name in jobs and _due(s, name, force):
            try:
                from .telemetry import span
                with span(f"background.{name}"):
                    r = fn()
                if r:
                    out[name] = r
                    log({name: r})
            except Exception as e:  # noqa: BLE001 - one failing job must not stop the others
                out[name] = {"error": str(e)[:300]}
                s.audit(None, f"bg.{name}.error", "", str(e)[:300])

    from .orchestrator import Orchestrator
    o = Orchestrator(s)
    run("recurring", o.run_due)
    def reap():
        from .secrets import Broker
        r = o.reap()
        n = Broker(s).reap_expired()
        return (r or []) + ([{"dynamic_leases_revoked": n}] if n else [])
    run("reap", reap)

    def wake():
        from .messaging import Dispatcher
        r = Dispatcher.run_due(s)
        return r if any(r.values()) else None
    run("wake", wake)

    def a2a_push():
        from .a2a_server import push_due
        return push_due(s) or None
    run("a2a_push", a2a_push)

    def reindex():  # fill vectors for the active embedder (e.g. after upgrading hash -> fastembed)
        r = s.reindex(limit=int(os.environ.get("COMMONS_REINDEX_BATCH", "512")))
        return r if r.get("done") else None
    run("reindex", reindex)

    def quota_wake():  # work parked until a quota reset goes back out once the reset passes
        from .orchestrator import Orchestrator
        return Orchestrator(s).quota_wake() or None
    run("quota_wake", quota_wake)

    def release():  # a new version started: tell every agent what changed and what to adapt (once per version)
        from .releases import announce
        return announce(s)
    run("release", release)

    def events():  # outside events (GitHub) become jobs for the best live agent
        from .events import watch_github
        return watch_github(s) or None
    run("events", events)

    def graphiti():  # mirror new memories into a Graphiti knowledge graph, when one is configured
        from . import settings as _st
        url = _st.get(s, "graphiti_url")
        if not url:
            return None
        from .graphiti import push
        return push(s, url)["sent"] or None
    run("graphiti", graphiti)

    def schedules():  # take over scheduled runs an owner missed or can't deliver
        from . import settings as _st
        if not _st.get(s, "schedule_takeover"):
            return
        from .schedules import watch
        return watch(s) or None
    run("schedules", schedules)

    def tiers():  # hot / warm / cold from recency, frequency and kind
        from .tiers import retier
        return retier(s)
    run("tiers", tiers)

    def esc():
        from .escalation import Escalator
        return Escalator(s).run()
    run("escalate", esc)

    def kb():
        from .kb import KB
        return KB(s).refresh_dirty()
    run("kb", kb)

    def lib():
        from .librarian import Librarian
        L = Librarian(s)
        if L.status()["new_since_last_run"]:
            rep = L.run()
            return {k: rep.get(k) for k in ("items_scanned", "tagged", "assigned", "summaries")}
        return None
    run("librarian", lib)

    def models():
        if not s.q("SELECT 1 FROM providers WHERE enabled=1"):
            return None
        from .providers import ProviderLayer
        w = ProviderLayer(s).watch()
        return {"new": w["refresh"]["new_models"]}
    run("models", models)

    def rv():
        from .providers import ProviderLayer
        return ProviderLayer(s).validate_routing()
    run("routing_validate", rv)

    def disc():
        if os.environ.get("COMMONS_SKILL_DISCOVERY", "1") != "1":
            return None
        from .skilldiscovery import Discovery
        d = Discovery(s)
        if not d.configure()["token_ref"]:
            return None
        return {"discovery": d.discover(), "updates": d.check_updates()}
    run("discovery", disc)

    def prune():
        from .retention import prune as p
        return p(s)
    run("prune", prune)

    def backup():
        if os.environ.get("COMMONS_BACKUP", "0") != "1":
            return None
        from .retention import backup as b
        return b(s)
    run("backup", backup)
    return out


_started: dict[str, threading.Thread] = {}


def start_background(s: Store, mode: str | None = None) -> threading.Thread | None:
    mode = mode or os.environ.get("COMMONS_BACKGROUND", "light")
    key = _dbkey(s)  # one loop per database: `serve --dashboard` builds two Store objects on the same file
    if mode in ("0", "off", "none") or key in _started:
        return _started.get(key)
    jobs = tuple(INTERVALS) if mode == "full" else LIGHT
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                tick(s, jobs)
            except Exception:  # noqa: BLE001
                pass
            stop.wait(float(os.environ.get("COMMONS_BG_POLL", "2")))

    t = threading.Thread(target=loop, name="commons-background", daemon=True)
    t.stop = stop  # type: ignore[attr-defined]
    t.start()
    _started[key] = t
    return t


def _dbkey(s: Store) -> str:
    return s.path if s.dialect == "postgres" or s.path == ":memory:" else os.path.realpath(s.path)


def stop_background(s: Store) -> None:
    t = _started.pop(_dbkey(s), None)
    if t:
        t.stop.set()  # type: ignore[attr-defined]
        t.join(timeout=5)


def status(s: Store) -> dict:
    out = {}
    for k in INTERVALS:
        r = s.q("SELECT value FROM meta WHERE key=?", (f"bg_last:{k}",))
        out[k] = {"every": _every(k), "last": float(r[0]["value"]) if r else None}
    lp = s.q("SELECT value FROM meta WHERE key='last_prune'")
    lb = s.q("SELECT value FROM meta WHERE key='last_backup'")
    return {"jobs": out, "last_prune": json.loads(lp[0]["value"]) if lp else None,
            "last_backup": json.loads(lb[0]["value"]) if lb else None, "time": time.time()}
