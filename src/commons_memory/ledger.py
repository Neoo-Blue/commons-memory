"""Which model, from which agent, at what effort, used how many tokens.

Each agent's own tool keeps the truth: Claude Code writes every reply's model, effort and token counts into its session
transcripts, and the Grok and Antigravity bridges keep a result per job. `commons quota sync` reads those on the machine
where the agents run (incrementally, offsets remembered), sums them per day, agent, model, effort, project and source,
and sends the totals with its usage windows. The hub stores totals, not increments, so a re-send replaces instead of
double counting. Commons' own model calls (the usage table, with effort from the routing decision) join the same view.
"""
from __future__ import annotations

import calendar
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path

from .ids import now
from .store import Store

SCHEMA = """CREATE TABLE IF NOT EXISTS usage_ledger(day TEXT NOT NULL, agent_id TEXT NOT NULL, model TEXT NOT NULL,
  effort TEXT NOT NULL, project TEXT NOT NULL, source TEXT NOT NULL, calls INTEGER, tokens_in INTEGER, tokens_out INTEGER,
  thinking INTEGER, cache_read INTEGER, cache_write INTEGER, cost REAL, updated_at REAL,
  PRIMARY KEY(day, agent_id, model, effort, project, source))"""
FIELDS = ("calls", "tokens_in", "tokens_out", "thinking", "cache_read", "cache_write", "cost")
EFFORT_SUFFIX = re.compile(r"-(minimal|low|medium|high|max|xhigh)$")
DAYS = 60  # history kept and read


def ensure(s: Store) -> None:
    if not getattr(s, "_ledger_ok", False):
        s.x(SCHEMA)
        s._ledger_ok = True


# ------------------------------------------------------------------------------------------------ hub side
def ingest(s: Store, agent_id: str, rows: list[dict]) -> dict:
    """Store daily totals for one agent. A row: {day, model, effort?, project?, source?, calls, tokens_in, tokens_out,
    thinking?, cache_read?, cache_write?, cost?}. Totals replace what was there for the same key."""
    ensure(s)
    n = 0
    with s.tx():
        for r in rows[:5000]:
            day = str(r.get("day") or "")[:10]
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or not r.get("model"):
                continue
            s.x("INSERT OR REPLACE INTO usage_ledger VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (day, agent_id, str(r["model"])[:80], str(r.get("effort") or "")[:20], str(r.get("project") or "")[:80],
                 str(r.get("source") or "")[:40], *[int(r.get(f) or 0) for f in FIELDS[:-1]], float(r.get("cost") or 0), now()))
            n += 1
    return {"stored": n}


def summary(s: Store, days: int = 14, agent: str | None = None) -> dict:
    """Totals per agent, model and effort for the last `days` days (plus a per-day series per model), from the agents'
    own records and from Commons' own calls. `rows` are sorted by total tokens."""
    ensure(s)
    since = time.strftime("%Y-%m-%d", time.gmtime(now() - (days - 1) * 86400))
    aid = (s.get_agent(agent) or {}).get("id", "unknown") if agent else None
    out: dict = {}
    series: dict = defaultdict(lambda: defaultdict(int))
    per_agent: dict = defaultdict(lambda: defaultdict(int))
    from .projects import _groups, _rows
    prow = _rows(s)
    root_of = {m: root for root, ids in _groups(prow).items() for m in ids}
    shown = {r["name"]: (prow[root_of.get(pid, pid)]["title"] or prow[root_of.get(pid, pid)]["name"]) for pid, r in prow.items()}
    def add(key, vals, day):
        e = out.setdefault(key, dict.fromkeys(FIELDS, 0) | {"projects": defaultdict(int), "days": set()})
        for f in FIELDS:
            e[f] += vals.get(f) or 0
        e["days"].add(day)
        if key[3]:  # named as the Projects page groups it (commons-dev, commons-test -> Commons)
            e["projects"][shown.get(key[3], key[3])] += (vals.get("tokens_in") or 0) + (vals.get("tokens_out") or 0)
        tok = (vals.get("tokens_in") or 0) + (vals.get("tokens_out") or 0) + (vals.get("cache_write") or 0)
        series[key[0] + " / " + key[1]][day] += tok
        per_agent[key[0]][day] += tok
    q, a = "SELECT * FROM usage_ledger WHERE day>=?", [since]
    if aid:
        q, a = q + " AND agent_id=?", a + [aid]
    reported = set()
    for r in s.q(q, a):
        name = s.label(r["agent_id"])[1]
        reported.add(r["agent_id"])
        add((name, r["model"], r["effort"], r["project"], r["source"] or "own records"), dict(r), r["day"])
    # Commons' own calls: the usage table, with the effort the router chose for that step (agents that report their
    # own records are skipped here, or their Commons steps would count twice)
    eff = {x["subtask_id"]: x["level"] for x in s.q("SELECT subtask_id, level FROM routing_decisions WHERE subtask_id IS NOT NULL")}
    uq = ("SELECT CAST(ts AS INTEGER)/86400 d, agent_id, model, subtask_id, project_id, COUNT(*) calls, SUM(tokens_in) ti, "
          "SUM(tokens_out) tout, SUM(cost) cost, SUM(COALESCE(cached_tokens,0)) cr FROM usage WHERE ts>=? "
          + ("AND agent_id=? " if aid else "") + "GROUP BY d, agent_id, model, subtask_id, project_id")
    for r in s.q(uq, [calendar.timegm(time.strptime(since, "%Y-%m-%d"))] + ([aid] if aid else [])):
        if r["agent_id"] in reported or not r["model"]:
            continue
        day = time.strftime("%Y-%m-%d", time.gmtime(int(r["d"]) * 86400))
        add((s.label(r["agent_id"])[1] if r["agent_id"] else "commons", r["model"], eff.get(r["subtask_id"]) or "",
             s.project_name(r["project_id"]) or "", "commons"),
            {"calls": r["calls"], "tokens_in": r["ti"] or 0, "tokens_out": r["tout"] or 0, "cost": r["cost"] or 0,
             "cache_read": r["cr"] or 0}, day)
    merged: dict = {}
    for (name, model, effort, project, source), e in out.items():  # the table shows agent x model x effort x source
        k = (name, model, effort, source)
        m = merged.setdefault(k, dict.fromkeys(FIELDS, 0) | {"projects": defaultdict(int), "days": set()})
        for f in FIELDS:
            m[f] += e[f]
        for p, v in e["projects"].items():
            m["projects"][p] += v
        m["days"] |= e["days"]
    rows = [{"agent": k[0], "model": k[1], "effort": k[2], "source": k[3], **{f: (round(v[f], 4) if f == "cost" else v[f]) for f in FIELDS},
             "total": v["tokens_in"] + v["tokens_out"] + v["cache_write"], "active_days": len(v["days"]),
             "top_projects": sorted(v["projects"].items(), key=lambda kv: -kv[1])[:5]} for k, v in merged.items()]
    rows.sort(key=lambda r: -r["total"])
    days_list = [time.strftime("%Y-%m-%d", time.gmtime(now() - (days - 1 - i) * 86400)) for i in range(days)]
    return {"days": days_list, "rows": rows,
            "series": {k: [v.get(d, 0) for d in days_list] for k, v in sorted(series.items(), key=lambda kv: -sum(kv[1].values()))[:12]},
            "agents": {k: [v.get(d, 0) for d in days_list] for k, v in per_agent.items()},
            "totals": {f: sum(r[f] for r in rows) for f in FIELDS} | {"total": sum(r["total"] for r in rows)}}


# ------------------------------------------------------------------------------------------------ local side
def _state_path() -> Path:
    return Path(os.environ.get("COMMONS_HOME") or Path.home() / ".commons") / "ledger-state.json"


def _load() -> dict:
    try:
        return json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {"files": {}, "jobs": {}, "totals": {}, "dirty": []}


def _save(st: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(p)


def _bump(st: dict, agent: str, day: str, model: str, effort: str, project: str, source: str, vals: dict) -> None:
    k = "|".join((agent, day, model, effort or "", project or "", source))
    t = st["totals"].setdefault(k, [0] * len(FIELDS))
    for i, f in enumerate(FIELDS):
        t[i] += vals.get(f) or 0
    if k not in st["dirty"]:
        st["dirty"].append(k)


def _day(ts) -> str | None:
    """A UTC day from an ISO time, a "YYYY-MM-DD hh:mm:ss" string or a unix time (number or numeric string)."""
    try:
        if re.match(r"\d{4}-\d{2}-\d{2}", str(ts)):
            return str(ts)[:10]
        return time.strftime("%Y-%m-%d", time.gmtime(float(ts)))
    except (TypeError, ValueError, OverflowError):
        return None


def read_claude(st: dict, agent: str, root: Path | None = None) -> int:
    """New replies in Claude Code's transcripts since the last read. A reply is written once per content block with the
    same message id and usage, so only the first of a run counts."""
    from .capture import project_from_cwd
    root = root or Path.home() / ".claude" / "projects"
    cutoff, n = time.time() - DAYS * 86400, 0
    projects: dict = {}
    for p in root.rglob("*.jsonl"):
        try:
            stt = p.stat()
        except OSError:
            continue
        if stt.st_mtime < cutoff:
            continue
        f = st["files"].setdefault(str(p), {"offset": 0, "last": ""})
        if stt.st_size < f["offset"]:  # rewritten: start over (its earlier totals stay; rare)
            f["offset"], f["last"] = 0, ""
        if stt.st_size == f["offset"]:
            continue
        with open(p, "rb") as fh:
            fh.seek(f["offset"])
            data = fh.read()
        end = data.rfind(b"\n") + 1  # never consume a half-written last line
        for line in data[:end].splitlines():
            if b'"assistant"' not in line or b'"usage"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            m = d.get("message") if isinstance(d.get("message"), dict) else {}
            u, model, mid = m.get("usage") or {}, m.get("model") or "", m.get("id") or ""
            if d.get("type") != "assistant" or not u or not model or model.startswith("<"):
                continue
            if mid and mid == f["last"]:
                continue
            f["last"] = mid
            day = _day(d.get("timestamp"))
            if not day:
                continue
            cwd = d.get("cwd") or ""
            if cwd not in projects:
                projects[cwd] = project_from_cwd(cwd) or ""
            _bump(st, agent, day, model, d.get("effort") or d.get("perTurnEffort") or "", projects[cwd],
                  "claude-code subagent" if d.get("isSidechain") else "claude-code",
                  {"calls": 1, "tokens_in": u.get("input_tokens") or 0, "tokens_out": u.get("output_tokens") or 0,
                   "thinking": (u.get("output_tokens_details") or {}).get("thinking_tokens") or 0,
                   "cache_read": u.get("cache_read_input_tokens") or 0, "cache_write": u.get("cache_creation_input_tokens") or 0})
            n += 1
        f["offset"] += end
    return n


def read_jobs(st: dict, agent: str, jobs: Path, source: str) -> int:
    """Finished jobs of a peer bridge (~/.claude/grok/jobs, ~/.claude/antigravity/jobs): model, tokens and cost per job.
    A model id that carries its effort (claude-opus-5-5-medium) is split into model and effort."""
    from .capture import project_from_cwd
    if not jobs.is_dir():
        return 0
    seen = set(st["jobs"].setdefault(source, []))
    cutoff, n = time.time() - DAYS * 86400, 0
    for d in jobs.iterdir():
        if d.name in seen or not (d / "result.json").exists():
            continue
        try:
            if d.stat().st_mtime < cutoff:
                seen.add(d.name)
                continue
            r = json.loads((d / "result.json").read_text())
            meta = json.loads((d / "meta.json").read_text()) if (d / "meta.json").exists() else {}
        except (OSError, ValueError):
            continue
        if r.get("status") not in ("done", None):
            continue  # still running: read it next time
        seen.add(d.name)
        u = r.get("usage") or {}
        tin, tout = u.get("input_tokens") or 0, u.get("output_tokens") or 0
        if not (tin or tout or r.get("tokens")):
            continue  # a failed call spent nothing
        model = str(r.get("model") or (meta.get("model") if meta.get("model") not in (None, "None") else "") or "default")
        effort = str(meta.get("effort") or r.get("effort") or "")
        m = EFFORT_SUFFIX.search(model)
        if m:
            model, effort = model[: m.start()], m.group(1)
        day = _day(meta.get("started")) or _day(d.stat().st_mtime)
        _bump(st, agent, day, model, effort, project_from_cwd(r.get("cwd") or meta.get("cwd") or "") or "", source,
              {"calls": 1, "tokens_in": tin or max(0, (r.get("tokens") or 0) - tout), "tokens_out": tout,
               "thinking": u.get("thinking_tokens") or 0, "cache_read": u.get("cache_read_tokens") or 0,
               "cost": float(r.get("cost_usd") or 0)})
        n += 1
    st["jobs"][source] = sorted(seen)
    return n


def collect(agents: dict[str, str] | None = None) -> tuple[dict, dict[str, list[dict]], dict[str, dict]]:
    """Read everything new on this machine. Returns (state, {agent: rows to send}, {agent: {models, current}}).
    agents maps a source to the Commons agent: {"claude": "Claude", "grok": "grok-build", "antigravity": "antigravity"}."""
    agents = {"claude": "Claude", "grok": "grok-build", "antigravity": "antigravity"} | (agents or {})
    st = _load()
    home = Path.home() / ".claude"
    read_claude(st, agents["claude"])
    read_jobs(st, agents["grok"], home / "grok" / "jobs", "grok bridge")
    read_jobs(st, agents["antigravity"], home / "antigravity" / "jobs", "antigravity bridge")
    keep = time.strftime("%Y-%m-%d", time.gmtime(time.time() - DAYS * 86400))
    st["totals"] = {k: v for k, v in st["totals"].items() if k.split("|")[1] >= keep}
    st["dirty"] = [k for k in st["dirty"] if k in st["totals"]]
    send: dict[str, list[dict]] = defaultdict(list)
    for k in st["dirty"]:
        agent, day, model, effort, project, source = k.split("|")
        send[agent].append({"day": day, "model": model, "effort": effort, "project": project, "source": source,
                            **dict(zip(FIELDS, st["totals"][k]))})
    recent = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 30 * 86400))
    catalog: dict[str, dict] = defaultdict(lambda: {"models": defaultdict(int), "current": ("", 0, "")})
    for k, v in st["totals"].items():
        agent, day, model = k.split("|")[:3]
        if day >= recent and model != "default":  # "default" = a job that never said which model ran it
            c = catalog[agent]
            c["models"][model] += v[1] + v[2]
            c["current"] = max(c["current"], (day, v[1] + v[2], model))  # the newest day's busiest model
    models = {a: {"models": sorted(c["models"], key=lambda m: -c["models"][m]), "current": c["current"][2]} for a, c in catalog.items()}
    return st, dict(send), models


def mark_sent(st: dict, agent: str) -> None:
    st["dirty"] = [k for k in st["dirty"] if not k.startswith(agent + "|")]


def save(st: dict) -> None:
    _save(st)

