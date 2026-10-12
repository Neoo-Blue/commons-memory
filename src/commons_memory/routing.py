"""Execution targets: route every subtask to an agent + model + effort.

A target is {agent, model, effort}. effort = {level, thinking_budget, reasoning_effort, max_tokens, temperature, tools}.
The distributor (`Router.choose`) builds candidates from each eligible agent's model registry (`agent_models`, else the
agent's own model), filters by live presence/readiness, and scores them by:
  model quality for the task type (learned `model_scores`, else a name-based prior) and the policy's tier,
  price (registry prices, else the built-in table) and the global/per-job mode (quality-first | balanced | cost-first),
  the agent's learned profile score, remaining job budget, and "different family than the author" for reviews.
Effort comes from the policy table (per task type) adjusted by estimated difficulty (heuristic, or the `difficulty`
LLM role when configured), by learned outcomes (de-escalate when a cheaper tier keeps succeeding, escalate when the
current tier fails) and by the retry ladder (each failed/rejected attempt raises effort, then the model tier).
Each decision is stored in `routing_decisions` (target, effort, rationale, alternatives, estimated cost) and later its
outcome (success, quality, actual cost, latency); `why()` explains it. Provider and CLI mappings turn the abstract
effort into native knobs.
"""
from __future__ import annotations

import json
import os
import re

from .ids import new_id, now
from .store import CommonsError, Store

LEVELS = ["minimal", "low", "medium", "high", "max"]
MODES = {"quality-first": (1.0, 0.05), "balanced": (1.0, 0.35), "cost-first": (0.6, 1.2)}
_BUDGET = {"minimal": 0, "low": 1024, "medium": 4096, "high": 16000, "max": 32000}
_MAXTOK = {"minimal": 1024, "low": 2048, "medium": 4096, "high": 8192, "max": 16000}
_TEMP = {"minimal": 0.2, "low": 0.2, "medium": 0.3, "high": 0.4, "max": 0.4}

DEFAULT_POLICY = {
    "plan": {"effort": "high", "tier": "top"}, "architecture": {"effort": "high", "tier": "top"},
    "red_team": {"effort": "high", "tier": "top"}, "debate": {"effort": "high", "tier": "top"},
    "judge": {"effort": "high", "tier": "top"}, "research": {"effort": "medium", "tier": "mid"},
    "code": {"effort": "medium", "tier": "coding"}, "test": {"effort": "medium", "tier": "coding"},
    "debug": {"effort": "high", "tier": "coding"}, "review": {"effort": "medium", "tier": "top", "different_family": True},
    "vote": {"effort": "medium", "tier": "mid"}, "docs": {"effort": "low", "tier": "mid"},
    "summarize": {"effort": "low", "tier": "cheap"}, "format": {"effort": "minimal", "tier": "cheap"},
    "librarian": {"effort": "low", "tier": "cheap"}, "tally": {"effort": "minimal", "tier": "cheap"},
    "general": {"effort": "medium", "tier": "mid"},
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS routing_decisions(id TEXT PRIMARY KEY, subtask_id TEXT, job_id TEXT, task_type TEXT,
  difficulty TEXT, agent_id TEXT, model TEXT, level TEXT, effort TEXT, mode TEXT, rationale TEXT, alternatives TEXT,
  est_cost REAL, attempt INTEGER, created_at REAL, outcome TEXT, quality REAL, cost REAL, latency REAL, completed_at REAL);
CREATE INDEX IF NOT EXISTS routing_decisions_sub ON routing_decisions(subtask_id);
CREATE TABLE IF NOT EXISTS routing_stats(task_type TEXT, difficulty TEXT, model TEXT, level TEXT, n INTEGER,
  success INTEGER, quality_sum REAL, cost_sum REAL, latency_sum REAL, PRIMARY KEY(task_type, difficulty, model, level));
"""


# ------------------------------------------------------------------ effort + provider/CLI mapping
def effort_spec(level: str, tools: str | None = None) -> dict:
    if level not in LEVELS:
        raise CommonsError(f"effort must be one of {', '.join(LEVELS)}")
    re_ = {"minimal": "minimal", "low": "low", "medium": "medium", "high": "high", "max": "high"}[level]
    return {"level": level, "thinking_budget": _BUDGET[level], "reasoning_effort": re_, "max_tokens": _MAXTOK[level],
            "temperature": _TEMP[level], "tools": tools or ("full" if level in ("high", "max") else "read")}


def provider_params(kind: str, model: str, eff: dict) -> dict:
    """Map an effort spec to the provider's native request fields."""
    lvl, m = eff.get("level", "medium"), (model or "").lower()
    if kind == "anthropic":
        v = re.search(r"claude-(?:opus|sonnet|haiku|fable|mythos)-(\d+)(?:-(\d{1,2}))?(?!\d)", m)
        if v and (int(v.group(1)), int(v.group(2) or 0)) >= (4, 6):
            # Claude 4.6+ / 5.x: effort is the knob; thinking budgets and sampling params return 400 on 5.x.
            # minimal on Haiku also switches thinking off (Haiku accepts it; Opus/Sonnet 5.5 reject "disabled")
            out = {"output_config": {"effort": {"minimal": "low"}.get(lvl, lvl)}}
            return out | ({"thinking": {"type": "disabled"}} if lvl == "minimal" and "haiku" in m else {})
        if eff.get("thinking_budget", 0) >= 1024 and re.search(r"claude-(opus|sonnet|haiku)-4|claude-3-7", m):
            return {"thinking": {"type": "enabled", "budget_tokens": int(eff["thinking_budget"])}}  # temperature must stay 1
        return {"temperature": eff.get("temperature", 0.3)}
    if kind == "openai":
        if re.match(r"(o\d|gpt-5)", m):
            r = eff["reasoning_effort"]
            return {"reasoning_effort": "low" if r == "minimal" and m.startswith("o") else r}
        return {"temperature": eff.get("temperature", 0.3)}
    if kind == "xai":
        if "grok-3-mini" in m:  # the only xAI models with a reasoning_effort knob (low|high)
            return {"reasoning_effort": "high" if lvl in ("high", "max") else "low"}
        return {"temperature": eff.get("temperature", 0.3)} if "grok-4" not in m else {}
    if kind == "google":
        if "2.5" in m or "gemini-3" in m:
            b = eff.get("thinking_budget", 0)
            if "pro" in m:
                b = max(128, b)  # Pro can't disable thinking
            return {"generationConfig": {"thinkingConfig": {"thinkingBudget": int(b)}}}
        return {"generationConfig": {"temperature": eff.get("temperature", 0.3)}}
    if kind == "openrouter":
        return {"reasoning": {"effort": {"minimal": "low", "max": "high"}.get(eff["reasoning_effort"], eff["reasoning_effort"])
                              if lvl != "max" else "high"}}
    if kind == "ollama":
        return {"think": lvl in ("medium", "high", "max")}
    if kind == "mock":
        return {"_mock_effort": lvl}
    return {}


CLI_SUPPORT = {
    "claude": "--model <alias|id>; effort via MAX_THINKING_TOKENS env (Claude Code reads it as the thinking budget)",
    "codex": "-m <model> -c model_reasoning_effort=<minimal|low|medium|high>",
    "agent": "--model <model> (Cursor CLI); no effort flag",
    "cursor-agent": "--model <model>; no effort flag",
    "gemini": "-m <model>; thinking budget is settings-only (no CLI flag)",
}


def cli_target(cmd: str, model: str | None, eff: dict | None) -> tuple[str, dict]:
    """Insert model/effort flags for known agent CLIs right after the executable. Returns (cmd, extra_env)."""
    if not cmd or cmd.startswith("@llm"):
        return cmd, {}
    head, _, rest = cmd.strip().partition(" ")
    exe = os.path.basename(head)
    m = (model or "").split("/", 1)[-1] if model else ""
    flags, env = [], {}
    if exe == "claude":
        if m and "--model" not in cmd:
            flags += ["--model", m]
        if eff and eff.get("thinking_budget"):
            env["MAX_THINKING_TOKENS"] = str(eff["thinking_budget"])
    elif exe == "codex":
        if m and " -m " not in f" {cmd} " and "--model" not in cmd:
            flags += ["-m", m]
        if eff:
            flags += ["-c", f"model_reasoning_effort={eff['reasoning_effort']}"]
    elif exe in ("agent", "cursor-agent"):
        if m and "--model" not in cmd:
            flags += ["--model", m]
    elif exe == "gemini":
        if m and " -m " not in f" {cmd} " and "--model" not in cmd:
            flags += ["-m", m]
    if not flags:
        return cmd, env
    import shlex
    sub = rest.split(" ", 1)  # keep subcommands first (codex exec ...)
    if exe == "codex" and sub and sub[0] == "exec":
        return f"{head} exec {' '.join(shlex.quote(f) for f in flags)} {sub[1] if len(sub) > 1 else ''}".strip(), env
    return f"{head} {' '.join(shlex.quote(f) for f in flags)} {rest}".strip(), env


# ------------------------------------------------------------------ model knowledge
def family(model: str | None) -> str:
    m = (model or "").lower()
    parts = m.split("/")
    if parts[0] in ("anthropic", "openai", "google", "xai", "meta", "mistral", "deepseek", "qwen") and len(parts) > 1:
        return parts[0]
    for rx, fam in ((r"claude", "anthropic"), (r"\bgpt|\bo[134]\b|o[134]-|codex|davinci", "openai"), (r"gemini|gemma", "google"),
                    (r"grok", "xai"), (r"llama", "meta"), (r"mistral|devstral|codestral", "mistral"),
                    (r"qwen", "qwen"), (r"deepseek", "deepseek")):
        if re.search(rx, m):
            return fam
    return parts[0] if len(parts) > 1 else "unknown"


def prior_quality(model: str | None) -> float:
    m = (model or "").lower()
    if not m:
        return 0.6
    if re.search(r"opus|gpt-5(?!-mini|-nano)|\bo3\b|o3-pro|gemini-[23]\.?5?-pro|gemini-3-pro|grok-4(?!-fast)|-pro\b", m):
        return 0.92
    if re.search(r"sonnet|gpt-4\.1(?!-mini|-nano)|gpt-4o(?!-mini)|gpt-5-codex|o4-mini|gemini-2\.5-flash(?!-lite)|grok-3(?!-mini)|grok-4-fast|deepseek-r1|qwen3-coder", m):
        return 0.8
    if re.search(r"haiku|mini|nano|flash-lite|gemma|llama|8b|7b", m):
        return 0.6
    return 0.7


def is_coding(model: str | None) -> bool:
    return bool(re.search(r"codex|coder|devstral|codestral|sonnet|opus|gpt-5|claude", (model or "").lower()))


def price_per_m(s: Store, model: str | None) -> float:
    """Blended $/M tokens (3:1 input:output)."""
    if not model:
        return 3.0
    r = s.q("SELECT cost_in, cost_out FROM models WHERE id=? OR model=?", (model, model.split("/", 1)[-1]))
    if r and r[0]["cost_in"] is not None:
        return (3 * (r[0]["cost_in"] or 0) + (r[0]["cost_out"] or 0)) / 4
    from .usage import estimate_cost
    return (3 * estimate_cost(model.split("/", 1)[-1], 1_000_000, 0) + estimate_cost(model.split("/", 1)[-1], 0, 1_000_000)) / 4


# ------------------------------------------------------------------ difficulty
_HARD = r"\b(architect|design|migrat|security|concurren|distributed|race condition|prove|formal|optimi[sz]e|scal|" \
        r"refactor (the|a) (whole|entire)|threat model|cryptograph|consensus|deadlock|performance|trade-?offs?)\w*"
_EASY = r"\b(typo|rename|format|reformat|lint|bump|summari[sz]e|list|translate|spell|one-line|tiny|trivial|small)\w*"


def estimate_difficulty(s: Store, title: str, body: str = "", task_type: str = "general") -> tuple[str, str]:
    """(easy|medium|hard, how). Uses the `difficulty` role model when explicitly configured, else heuristics."""
    text = f"{title}\n{body}"
    if s.q("SELECT 1 FROM meta WHERE key='role:difficulty'"):
        from .providers import ProviderLayer
        out = ProviderLayer(s).call_role("difficulty", "Rate the difficulty of this subtask for a capable AI agent. "
                                         "Answer with exactly one word: easy, medium or hard.\n\n" + text[:3000], max_tokens=5)
        m = re.search(r"\b(easy|medium|hard)\b", (out or "").lower())
        if m:
            return m.group(1), "llm"
    hard, easy = len(re.findall(_HARD, text, re.I)), len(re.findall(_EASY, text, re.I))
    n = len(text)
    score = hard * 2 - easy * 2 + (1 if n > 1500 else 0) + (1 if n > 5000 else 0) - (1 if n < 120 else 0)
    if task_type in ("plan", "architecture", "red_team", "debate", "judge"):
        score += 1
    return ("hard" if score >= 2 else "easy" if score <= -1 else "medium"), "heuristic"


# ------------------------------------------------------------------ router
class Router:
    def __init__(self, s: Store):
        self.s = s

    # ---- configuration
    def policy(self) -> dict:
        r = self.s.q("SELECT value FROM meta WHERE key='routing:policy'")
        over = json.loads(r[0]["value"]) if r else {}
        return {k: DEFAULT_POLICY.get(k, {}) | over.get(k, {}) for k in sorted(set(DEFAULT_POLICY) | set(over))}

    def set_policy(self, task_type: str, *, effort: str | None = None, tier: str | None = None,
                   different_family: bool | None = None, model: str | None = None) -> dict:
        r = self.s.q("SELECT value FROM meta WHERE key='routing:policy'")
        over = json.loads(r[0]["value"]) if r else {}
        row = over.get(task_type, {})
        if effort:
            effort_spec(effort)
            row["effort"] = effort
        if tier:
            if tier not in ("top", "mid", "cheap", "coding"):
                raise CommonsError("tier must be top|mid|cheap|coding")
            row["tier"] = tier
        if different_family is not None:
            row["different_family"] = bool(different_family)
        if model is not None:
            row["model"] = model or None
        over[task_type] = row
        self.s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", ("routing:policy", json.dumps(over)))
        return self.policy()[task_type]

    def mode(self, job: dict | None = None) -> str:
        if job:
            b = json.loads(job.get("budget") or "{}") if isinstance(job.get("budget"), str) else (job.get("budget") or {})
            if b.get("mode") in MODES:
                return b["mode"]
        r = self.s.q("SELECT value FROM meta WHERE key='routing:mode'")
        return r[0]["value"] if r else os.environ.get("COMMONS_ROUTING_MODE", "balanced")

    def set_mode(self, mode: str) -> str:
        if mode not in MODES:
            raise CommonsError(f"mode must be one of {', '.join(MODES)}")
        self.s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", ("routing:mode", mode))
        return mode

    # ---- learned adjustment
    def _stats(self, tt: str, diff: str, level: str) -> tuple[int, float]:
        r = self.s.q("SELECT COALESCE(SUM(n),0) n, COALESCE(SUM(success),0) s FROM routing_stats WHERE task_type=? AND difficulty=? "
                     "AND level=?", (tt, diff, level))[0]
        return int(r["n"]), (r["s"] / r["n"] if r["n"] else 0.0)

    def learned_level(self, tt: str, diff: str, level: str) -> tuple[str, str]:
        i = LEVELS.index(level)
        min_n = int(os.environ.get("COMMONS_ROUTING_LEARN_N", "5"))
        if i > 0:
            n, sr = self._stats(tt, diff, LEVELS[i - 1])
            if n >= min_n and sr >= 0.85:
                return LEVELS[i - 1], f"learned: {LEVELS[i - 1]} effort succeeded {sr:.0%} of {n} {tt}/{diff} tasks -> de-escalated"
        n, sr = self._stats(tt, diff, level)
        if n >= min_n and sr < 0.6 and i < len(LEVELS) - 1:
            return LEVELS[i + 1], f"learned: {level} effort succeeded only {sr:.0%} of {n} {tt}/{diff} tasks -> escalated"
        return level, ""

    # ---- choose
    def _agent_models(self, agent: dict) -> list[str | None]:
        ms = [r["model_id"] for r in self.s.q("SELECT model_id FROM agent_models WHERE agent_id=?", (agent["id"],))]
        if not ms and agent.get("model"):
            ms = [agent["model"]]
        return ms or [None]

    def choose(self, st: dict, ranked: list[tuple[float, dict]], job: dict | None = None) -> tuple[dict, dict] | None:
        """ranked: [(agent_score, agent_row)] from Profiles.rank (already penalised for spreading). Returns (agent, decision)."""
        if not ranked:
            return None
        from .messaging import presence
        pol = self.policy()
        tt = st["type"]
        p = pol.get(tt) or pol["general"]
        diff, how = estimate_difficulty(self.s, st.get("title", ""), st.get("body", ""), tt)
        level = p.get("effort", "medium")
        why = [f"policy {tt}: {level} effort, {p.get('tier', 'mid')} tier"]
        i = LEVELS.index(level) + {"easy": -1, "medium": 0, "hard": 1}[diff]
        level = LEVELS[max(0, min(len(LEVELS) - 1, i))]
        if diff != "medium":
            why.append(f"difficulty {diff} ({how}) -> {level}")
        level, note = self.learned_level(tt, diff, level)
        if note:
            why.append(note)
        attempt = int(st.get("attempts") or 0)
        prev = self.s.q("SELECT model, level, outcome FROM routing_decisions WHERE subtask_id=? ORDER BY created_at DESC LIMIT 1",
                        (st["id"],))
        failed_models = {r["model"] for r in self.s.q("SELECT model FROM routing_decisions WHERE subtask_id=? AND "
                                                       "outcome IN ('failed','rejected','timeout')", (st["id"],)) if r["model"]}
        if attempt and prev:
            lv = min(len(LEVELS) - 1, max(LEVELS.index(level), LEVELS.index(prev[0]["level"] or level) + 1))
            if LEVELS[lv] != level:
                why.append(f"retry ladder: attempt {attempt + 1} after {prev[0]['outcome'] or 'failure'} -> {LEVELS[lv]} effort")
            elif prev[0]["level"] == LEVELS[-1]:
                why.append(f"retry ladder: effort already {LEVELS[-1]} -> stronger model")
            level = LEVELS[lv]
        mode = self.mode(job)
        wq, wc = MODES[mode]
        if p.get("tier") == "cheap":
            wc *= 2
        elif p.get("tier") == "top":
            wq *= 1.3
        # budget awareness
        spec = effort_spec(level)
        if job:
            b = json.loads(job.get("budget") or "{}") if isinstance(job.get("budget"), str) else (job.get("budget") or {})
            u = json.loads(job.get("used") or "{}") if isinstance(job.get("used"), str) else (job.get("used") or {})
            if b.get("cost"):
                remaining = float(b["cost"]) - float(u.get("cost", 0))
                if remaining < float(b["cost"]) * 0.25:
                    wq, wc = MODES["cost-first"]
                    why.append(f"budget: ${remaining:.2f} of ${b['cost']} left -> cost-first")
        author_fams = set()
        if p.get("different_family") and st.get("gate_of"):
            for r in self.s.q("SELECT model FROM routing_decisions WHERE subtask_id=? ORDER BY created_at DESC LIMIT 1",
                              (st["gate_of"],)):
                if r["model"]:
                    author_fams.add(family(r["model"]))
        cands = []
        top_agent = max(0.001, max(sc for sc, _ in ranked))  # all-negative scores must not invert the ranking
        for sc, a in ranked:
            pr = presence(self.s, a["id"])
            if not pr["ready"] and pr["state"] == "offline":
                ready_pen, rnote = 0.6, "offline, not wakeable"
            elif pr["state"] in ("online", "idle"):
                ready_pen, rnote = 0.0, pr["state"]
            else:
                ready_pen, rnote = 0.15, f"{pr['state']}, wakeable"
            for m in self._agent_models(a):
                from .providers import ProviderLayer
                q_learn, n = ProviderLayer(self.s).model_score(m, tt) if m else (0.5, 0)
                q = (q_learn * min(1, n / 5) + prior_quality(m) * (1 - min(1, n / 5)))
                if p.get("tier") == "coding" and is_coding(m):
                    q += 0.08
                if p.get("model") and m and p["model"] in m:
                    q += 0.5
                price = price_per_m(self.s, m)
                est_tokens_in, est_tokens_out = 3000, spec["max_tokens"] * 0.5 + spec["thinking_budget"] * 0.5
                est_cost = (est_tokens_in + est_tokens_out) * price / 1e6
                util = wq * q - wc * min(1.0, price / 20) + 0.4 * (sc / top_agent) - ready_pen
                notes = []
                if author_fams and family(m) in author_fams:
                    util -= 1.0
                    notes.append("same family as author")
                if m in failed_models:
                    util -= 0.8
                    notes.append("failed this task before")
                cands.append({"agent": a, "model": m, "util": util, "quality": round(q, 3), "price": round(price, 3),
                              "est_cost": est_cost, "presence": rnote, "notes": notes})
        cands.sort(key=lambda c: -c["util"])
        best = cands[0]
        why.append(f"mode {mode}: {self.s.label(best['agent']['id'])[1]} / {best['model'] or 'its default model'} "
                   f"(quality {best['quality']}, ${best['price']}/M, {best['presence']})")
        if author_fams:
            why.append(f"review by a different model family than the author ({', '.join(sorted(author_fams))})")
        alts = [{"agent": c["agent"]["name"], "model": c["model"], "util": round(c["util"], 3), "quality": c["quality"],
                 "price": c["price"], "presence": c["presence"], "notes": c["notes"]} for c in cands[:5]]
        did = new_id("rdc")
        self.s.x("INSERT INTO routing_decisions(id,subtask_id,job_id,task_type,difficulty,agent_id,model,level,effort,mode,"
                 "rationale,alternatives,est_cost,attempt,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (did, st["id"], st.get("job_id"), tt, diff, best["agent"]["id"], best["model"], level, json.dumps(spec), mode,
                  "; ".join(why), json.dumps(alts), round(best["est_cost"], 6), attempt, now()))
        return best["agent"], {"decision_id": did, "model": best["model"], "effort": spec, "difficulty": diff,
                               "mode": mode, "rationale": "; ".join(why)}

    def current(self, subtask_id: str) -> dict | None:
        r = self.s.q("SELECT * FROM routing_decisions WHERE subtask_id=? ORDER BY created_at DESC LIMIT 1", (subtask_id,))
        if not r:
            return None
        return {"decision_id": r[0]["id"], "model": r[0]["model"], "effort": json.loads(r[0]["effort"] or "{}"),
                "difficulty": r[0]["difficulty"], "mode": r[0]["mode"], "rationale": r[0]["rationale"]}

    # ---- outcomes
    def record_outcome(self, subtask_id: str, outcome: str, quality: float | None = None) -> dict | None:
        r = self.s.q("SELECT * FROM routing_decisions WHERE subtask_id=? AND outcome IS NULL ORDER BY created_at DESC LIMIT 1",
                     (subtask_id,))
        if not r:
            return None
        d = r[0]
        cost = self.s.q("SELECT COALESCE(SUM(cost),0) c FROM usage WHERE subtask_id=? AND ts>=?", (subtask_id, d["created_at"]))[0]["c"]
        lat = now() - d["created_at"]
        ok = outcome in ("success", "approved")
        q = quality if quality is not None else (1.0 if ok else 0.0)
        self.s.x("UPDATE routing_decisions SET outcome=?, quality=?, cost=?, latency=?, completed_at=? WHERE id=?",
                 (outcome, q, cost, lat, now(), d["id"]))
        self.s.x("INSERT INTO routing_stats(task_type,difficulty,model,level,n,success,quality_sum,cost_sum,latency_sum) "
                 "VALUES(?,?,?,?,1,?,?,?,?) ON CONFLICT(task_type,difficulty,model,level) DO UPDATE SET n=routing_stats.n+1, "
                 "success=routing_stats.success+excluded.success, quality_sum=routing_stats.quality_sum+excluded.quality_sum, "
                 "cost_sum=routing_stats.cost_sum+excluded.cost_sum, latency_sum=routing_stats.latency_sum+excluded.latency_sum",
                 (d["task_type"], d["difficulty"], d["model"] or "-", d["level"], int(ok), q, cost, lat))
        if d["model"]:
            from .providers import ProviderLayer
            ProviderLayer(self.s).record_score(d["model"], d["task_type"], q, cost)
        return {"decision_id": d["id"], "outcome": outcome, "cost": cost}

    # ---- explain / report
    def why(self, ref: str) -> dict:
        rows = self.s.q("SELECT * FROM routing_decisions WHERE subtask_id=? OR job_id=? ORDER BY created_at", (ref, ref))
        if not rows:
            raise CommonsError("no routing decisions for that subtask/job")
        out = []
        for r in rows:
            out.append({"subtask": r["subtask_id"], "type": r["task_type"], "difficulty": r["difficulty"],
                        "agent": self.s.label(r["agent_id"])[1], "model": r["model"], "effort": r["level"],
                        "params": json.loads(r["effort"] or "{}"), "mode": r["mode"], "attempt": (r["attempt"] or 0) + 1,
                        "why": r["rationale"], "alternatives": json.loads(r["alternatives"] or "[]"),
                        "est_cost": r["est_cost"], "outcome": r["outcome"], "quality": r["quality"], "cost": r["cost"],
                        "latency_s": round(r["latency"], 1) if r["latency"] else None})
        text = "\n".join(f"{d['subtask']} [{d['type']}/{d['difficulty']}] -> {d['agent']} · {d['model'] or '-'} · {d['effort']} "
                         f"(attempt {d['attempt']}, {d['outcome'] or 'open'}): {d['why']}" for d in out)
        return {"decisions": out, "text": text}

    def decisions(self, limit: int = 50) -> list[dict]:
        return [dict(r) | {"agent": self.s.label(r["agent_id"])[1], "title": (self.s.q("SELECT title FROM subtasks WHERE id=?",
                                                                                         (r["subtask_id"],)) or [{"title": ""}])[0]["title"]}
                for r in self.s.q("SELECT * FROM routing_decisions ORDER BY created_at DESC LIMIT ?", (limit,))]

    def tiers(self) -> list[dict]:
        return [{"task_type": r["task_type"], "difficulty": r["difficulty"], "model": r["model"], "effort": r["level"],
                 "n": r["n"], "success_rate": round(r["success"] / r["n"], 3) if r["n"] else None,
                 "avg_quality": round(r["quality_sum"] / r["n"], 3) if r["n"] else None,
                 "avg_cost": round(r["cost_sum"] / r["n"], 5) if r["n"] else None,
                 "avg_latency_s": round(r["latency_sum"] / r["n"], 1) if r["n"] else None}
                for r in self.s.q("SELECT * FROM routing_stats ORDER BY task_type, difficulty, level")]
