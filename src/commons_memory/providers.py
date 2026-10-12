"""Model providers, model registry, LLM roles, the distributor (LLM planner/router), probes and reallocation.

Providers: anthropic, openai, openrouter, google, xai, ollama (local), mock (tests/demos). Keys are *secret refs*
(env://, vault://, op://) resolved server-side by the broker; values never reach models, memory or logs.

Roles (set any model id, no versions are hard-coded): distributor, planner, router, pref_extractor, kb_summarizer,
skill_reviewer, grader. Unset roles fall back to `distributor`; with no distributor, Commons uses heuristics.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from typing import Any

from .ids import iso, new_id, now
from .redact import redact
from .store import CommonsError, Store
from .usage import Usage, estimate_cost

KINDS = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "xai": "https://api.x.ai/v1",
    "ollama": "http://127.0.0.1:11434",
    "mock": "mock://",
}
ROLES = ["distributor", "planner", "router", "pref_extractor", "kb_summarizer", "skill_reviewer", "grader", "librarian", "retro"]
TASK_TYPES = ["research", "plan", "code", "review", "red_team", "test", "summarize", "debate", "judge", "general"]

# Small, cheap, automatically checkable probes per task type.
PROBES: dict[str, list[tuple[str, str]]] = {
    "research": [("What is the capital city of Australia? Answer with one word.", r"(?i)canberra"),
                 ("Which element has the chemical symbol Fe? One word.", r"(?i)iron")],
    "code": [("Write a Python expression that reverses a list named xs. Reply with only the expression.", r"xs\[::-1\]|reversed\(xs\)"),
             ("In Python, what does len('abc') return? Reply with only the value.", r"\b3\b")],
    "plan": [("What is 17*23? Reply with only the number.", r"\b391\b"),
             ("Order these steps: test, deploy, write code. Reply comma-separated.", r"(?i)write code.*test.*deploy")],
    "review": [("Find the bug: def add(a, b): return a - b. Reply in one sentence.", r"(?i)subtract|minus|should (be|use) \+|a \+ b"),
               ("Is this SQL safe: f\"SELECT * FROM t WHERE id={user_input}\"? Answer yes or no.", r"(?i)\bno\b")],
    "summarize": [("Summarize in one word: 'The cat sat on the mat and the cat slept.'", r"(?i)\bcat\b"),
                  ("Give a 2-word title for: 'Steps to install and configure PostgreSQL on Ubuntu'.", r"(?i)postgres")],
}
PROBES["test"] = PROBES["code"]
PROBES["red_team"] = PROBES["review"]
PROBES["judge"] = PROBES["review"]
PROBES["debate"] = PROBES["research"]
PROBES["general"] = PROBES["research"][:1] + PROBES["plan"][:1]


def _http(method: str, url: str, headers: dict | None = None, body: Any = None, timeout: float = 60) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - operator-configured provider URL
        return json.loads(r.read() or b"{}")


class ProviderLayer:
    def __init__(self, store: Store):
        self.s = store
        from .secrets import Broker
        self.broker = Broker(store)
        self.usage = Usage(store)

    # ------------------------------------------------------------------ providers
    def add_provider(self, name: str, kind: str, key_ref: str | None = None, base_url: str | None = None,
                     config: dict | None = None) -> dict:
        if kind not in KINDS:
            raise CommonsError(f"kind must be one of {', '.join(KINDS)}")
        if key_ref:
            if "://" not in key_ref:
                raise CommonsError("key_ref must be a secret reference like env://OPENROUTER_API_KEY — never a raw key")
            if not self.s.q("SELECT 1 FROM secret_refs WHERE ref=?", (key_ref,)):
                self.broker.register(key_ref, f"API key for provider {name}")
        self.s.x("INSERT OR REPLACE INTO providers VALUES(?,?,?,?,?,?,?,?,?)",
                 (name, kind, base_url or KINDS[kind], key_ref, 1, json.dumps(config or {}), None, None, now()))
        self.s.upsert_entity("provider", name)
        if key_ref:
            self.s.relate(self.s.upsert_entity("provider", name), key_ref, "authenticates_with")
        return {"provider": name, "kind": kind, "key_ref": key_ref}

    def providers(self) -> list[dict]:
        out = []
        for r in self.s.q("SELECT * FROM providers ORDER BY name"):
            n = self.s.q("SELECT COUNT(*) c FROM models WHERE provider=?", (r["name"],))[0]["c"]
            out.append({"name": r["name"], "kind": r["kind"], "base_url": r["base_url"], "key_ref": r["key_ref"],
                        "enabled": bool(r["enabled"]), "models": n, "last_refresh": iso(r["last_refresh"]),
                        "last_error": r["last_error"]})
        return out

    def _p(self, name: str) -> dict:
        r = self.s.q("SELECT * FROM providers WHERE name=?", (name,))
        if not r:
            raise CommonsError(f"unknown provider {name}")
        return dict(r[0])

    def _key(self, p: dict) -> str | None:
        return self.broker.resolve_internal(p["key_ref"], f"provider:{p['name']}") if p["key_ref"] else None

    def count_tokens(self, model_id: str, text: str) -> int | None:
        """Exact input-token count from the provider API (Anthropic /v1/messages/count_tokens, Gemini :countTokens),
        cached by text hash; None if unsupported."""
        prov, _, model = model_id.partition("/")
        r = self.s.q("SELECT * FROM providers WHERE name=? AND enabled=1", (prov,))
        if not r:
            return None
        p = dict(r[0])
        key = "tc:" + hashlib.sha256(f"{model_id}\n{text}".encode()).hexdigest()[:32]
        c = self.s.q("SELECT tokens FROM token_count_cache WHERE key=?", (key,))
        if c:
            return c[0]["tokens"]
        kind, base = p["kind"], (p["base_url"] or KINDS.get(p["kind"]) or "").rstrip("/")
        n = None
        if kind == "anthropic":
            out = _http("POST", f"{base}/v1/messages/count_tokens", {"x-api-key": self._key(p), "anthropic-version": "2023-06-01"},
                        {"model": model, "messages": [{"role": "user", "content": text}]}, timeout=20)
            n = out.get("input_tokens")
        elif kind == "google":
            out = _http("POST", f"{base}/models/{model}:countTokens?key={self._key(p)}",
                        body={"contents": [{"parts": [{"text": text}]}]}, timeout=20)
            n = out.get("totalTokens")
        elif kind == "mock":
            n = len(text.split())  # deterministic for tests
        if n is not None:
            self.s.x("INSERT OR REPLACE INTO token_count_cache VALUES(?,?,?,?)", (key, int(n), kind, now()))
        return n

    # ------------------------------------------------------------------ model lists
    def _fetch_models(self, p: dict) -> list[dict]:
        kind, base, key = p["kind"], p["base_url"].rstrip("/"), self._key(p)
        if kind == "mock":
            return json.loads(p["config"] or "{}").get("models", [])
        if kind == "openrouter":
            data = _http("GET", f"{base}/models", {"Authorization": f"Bearer {key}"} if key else {})["data"]
            return [{"model": m["id"], "context": m.get("context_length"),
                     "cost_in": float(m.get("pricing", {}).get("prompt") or 0) * 1e6,
                     "cost_out": float(m.get("pricing", {}).get("completion") or 0) * 1e6,
                     "capabilities": m.get("architecture", {}).get("input_modalities", [])} for m in data]
        if kind in ("openai", "xai"):
            data = _http("GET", f"{base}/models", {"Authorization": f"Bearer {key}"})["data"]
            return [{"model": m["id"]} for m in data]
        if kind == "anthropic":
            data = _http("GET", f"{base}/v1/models", {"x-api-key": key, "anthropic-version": "2023-06-01"})["data"]
            return [{"model": m["id"]} for m in data]
        if kind == "google":
            data = _http("GET", f"{base}/models?key={key}")["models"]
            return [{"model": m["name"].split("/")[-1], "context": m.get("inputTokenLimit")} for m in data
                    if "generateContent" in m.get("supportedGenerationMethods", [])]
        if kind == "ollama":
            return [{"model": m["name"], "cost_in": 0, "cost_out": 0} for m in _http("GET", f"{base}/api/tags")["models"]]
        raise CommonsError(f"unsupported kind {kind}")

    def refresh_models(self, provider: str | None = None) -> dict:
        """Pull model lists from provider APIs; models never seen before are flagged NEW."""
        names = [provider] if provider else [r["name"] for r in self.s.q("SELECT name FROM providers WHERE enabled=1")]
        new, seen = [], 0
        for name in names:
            p = self._p(name)
            try:
                models = self._fetch_models(p)
            except Exception as e:  # noqa: BLE001
                self.s.x("UPDATE providers SET last_error=? WHERE name=?", (str(e)[:300], name))
                continue
            for m in models:
                mid = f"{name}/{m['model']}"
                seen += 1
                exists = self.s.q("SELECT 1 FROM models WHERE id=?", (mid,))
                if exists:
                    self.s.x("UPDATE models SET last_seen=?, context=COALESCE(?,context), cost_in=COALESCE(?,cost_in), "
                             "cost_out=COALESCE(?,cost_out), status='available' WHERE id=?",
                             (now(), m.get("context"), m.get("cost_in"), m.get("cost_out"), mid))
                else:
                    self.s.x("INSERT INTO models VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (mid, name, m["model"], m.get("context"), m.get("cost_in"), m.get("cost_out"),
                              json.dumps(m.get("capabilities") or []), m.get("speed"), now(), now(),
                              1 if self._has_baseline(name) else 0, "available", json.dumps(m)))
                    if self._has_baseline(name):
                        new.append(mid)
                    ent = self.s.upsert_entity("model", mid)
                    self.s.relate(ent, self.s.upsert_entity("provider", name), "served_by")
            self.s.x("UPDATE providers SET last_refresh=?, last_error=NULL WHERE name=?", (now(), name))
            self.s.x("INSERT OR REPLACE INTO meta VALUES(?, '1')", (f"baseline:{name}",))
        if new:
            self.s.audit(None, "models.new", ",".join(new))
        return {"providers": names, "models_seen": seen, "new_models": new}

    def _has_baseline(self, provider: str) -> bool:
        """The first refresh of a provider establishes the baseline; only later additions count as NEW."""
        return bool(self.s.q("SELECT 1 FROM meta WHERE key=?", (f"baseline:{provider}",)))

    def models(self, provider: str | None = None, new_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM models WHERE 1=1" + (" AND provider=?" if provider else "") + (" AND is_new=1" if new_only else "")
        out = []
        for r in self.s.q(sql + " ORDER BY provider, model", [provider] if provider else []):
            scores = {x["task_type"]: round(x["score_sum"] / x["n"], 3) for x in
                      self.s.q("SELECT * FROM model_scores WHERE model_id=? AND n>0", (r["id"],))}
            out.append({"id": r["id"], "provider": r["provider"], "model": r["model"], "context": r["context"],
                        "cost_in": r["cost_in"], "cost_out": r["cost_out"], "is_new": bool(r["is_new"]),
                        "first_seen": iso(r["first_seen"]), "scores": scores,
                        "agents": [self.s.label(a["agent_id"])[1] for a in
                                   self.s.q("SELECT agent_id FROM agent_models WHERE model_id=?", (r["id"],))]})
        return out

    def set_agent_models(self, agent_id: str, model_ids: list[str]) -> dict:
        self.s.x("DELETE FROM agent_models WHERE agent_id=?", (agent_id,))
        for m in model_ids:
            self.s.x("INSERT OR IGNORE INTO agent_models VALUES(?,?)", (agent_id, m))
            self.s.relate(agent_id, self.s.upsert_entity("model", m), "can_use")
        return {"agent_id": agent_id, "models": model_ids}

    # ------------------------------------------------------------------ chat
    def chat(self, model_id: str, prompt: str, system: str = "", max_tokens: int = 1024, role: str = "",
             agent_id: str | None = None, job_id: str | None = None, cache_prefix: str = "") -> dict:
        from .telemetry import span
        with span("llm.chat", model=model_id, role=role or "chat", job=job_id) as sp:
            out = self._chat(model_id, prompt, system, max_tokens, role, agent_id, job_id, cache_prefix)
            if sp is not None and isinstance(out, dict):
                for k in ("tokens_in", "tokens_out", "cached_tokens"):
                    sp.set_attribute(f"commons.{k}", int(out.get(k) or 0))
            return out

    def _chat(self, model_id: str, prompt: str, system: str = "", max_tokens: int = 1024, role: str = "",
              agent_id: str | None = None, job_id: str | None = None, cache_prefix: str = "") -> dict:
        """cache_prefix: a stable block (e.g. a context pack's L0+L1) placed first so provider prompt caches hit:
        Anthropic gets an explicit cache_control breakpoint; OpenAI/OpenRouter/xAI cache identical prefixes
        automatically. Cached input tokens are recorded in usage.cached_tokens."""
        cached = 0
        provider, _, model = model_id.partition("/")
        p = self._p(provider)
        prompt, _ = redact(prompt)  # never ship secrets to a model
        kind, base = p["kind"], p["base_url"].rstrip("/")
        if kind == "mock":
            text = _mock_chat(json.loads(p["config"] or "{}"), model, system, cache_prefix + prompt)
            tin, tout = len(system + cache_prefix + prompt) // 4, len(text) // 4
            if cache_prefix:  # simulate provider prefix caching
                import hashlib as _h
                k = f"mockcache:{model_id}:{_h.sha256(cache_prefix.encode()).hexdigest()[:24]}"
                if self.s.q("SELECT 1 FROM meta WHERE key=?", (k,)):
                    cached = len(cache_prefix) // 4
                self.s.x("INSERT OR REPLACE INTO meta VALUES(?, '1')", (k,))
        elif kind == "anthropic":
            sysblocks = ([{"type": "text", "text": system}] if system else []) + \
                ([{"type": "text", "text": cache_prefix, "cache_control": {"type": "ephemeral"}}] if cache_prefix else [])
            r = _http("POST", f"{base}/v1/messages", {"x-api-key": self._key(p), "anthropic-version": "2023-06-01"},
                      {"model": model, "max_tokens": max_tokens, "system": sysblocks or None,
                       "messages": [{"role": "user", "content": prompt}]})
            text = "".join(c.get("text", "") for c in r.get("content", []))
            u = r.get("usage", {})
            cached = u.get("cache_read_input_tokens", 0) or 0
            tin, tout = u.get("input_tokens", 0) + cached + (u.get("cache_creation_input_tokens", 0) or 0), u.get("output_tokens", 0)
        elif kind == "google":
            r = _http("POST", f"{base}/models/{model}:generateContent?key={self._key(p)}",
                      body={"contents": [{"parts": [{"text": (system + "\n\n" if system else "") + cache_prefix + prompt}]}],
                            "generationConfig": {"maxOutputTokens": max_tokens}})
            text = "".join(pt.get("text", "") for c in r.get("candidates", [])[:1] for pt in c["content"]["parts"])
            u = r.get("usageMetadata", {})
            tin, tout = u.get("promptTokenCount", 0), u.get("candidatesTokenCount", 0)
        else:  # openai-compatible: openai, openrouter, xai, ollama
            url = f"{base}/v1/chat/completions" if kind == "ollama" else f"{base}/chat/completions"
            key = self._key(p)
            msgs = ([{"role": "system", "content": system}] if system else []) + \
                ([{"role": "system", "content": cache_prefix}] if cache_prefix else []) + [{"role": "user", "content": prompt}]
            r = _http("POST", url, {"Authorization": f"Bearer {key}"} if key else {},
                      {"model": model, "messages": msgs, "max_tokens": max_tokens})
            text = r["choices"][0]["message"]["content"] or ""
            tin, tout = r.get("usage", {}).get("prompt_tokens", 0), r.get("usage", {}).get("completion_tokens", 0)
            cached = ((r.get("usage", {}).get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        m = self.s.q("SELECT cost_in, cost_out FROM models WHERE id=?", (model_id,))
        if m and m[0]["cost_in"] is not None:
            cost = ((tin - 0.9 * cached) * (m[0]["cost_in"] or 0) + tout * (m[0]["cost_out"] or 0)) / 1e6
        else:
            cost = estimate_cost(model, int(tin - 0.9 * cached), tout)  # cache reads ~10% of input price
        self.usage.report(agent_id or self.s.ensure_agent(f"provider:{provider}"), tokens_in=tin, tokens_out=tout,
                          model=model_id, cost=cost, job_id=job_id, note=f"llm:{role or 'chat'}", cached_tokens=cached)
        return {"text": text, "tokens_in": tin, "tokens_out": tout, "cost": round(cost, 6), "model": model_id,
                "cached_tokens": cached}

    # ------------------------------------------------------------------ roles
    def set_role(self, role: str, model_id: str | None) -> dict:
        if role not in ROLES:
            raise CommonsError(f"role must be one of {', '.join(ROLES)}")
        if model_id:
            self.s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"role:{role}", model_id))
        else:
            self.s.x("DELETE FROM meta WHERE key=?", (f"role:{role}",))
        return {"role": role, "model": model_id}

    def role_model(self, role: str) -> str | None:
        for k in (role, "distributor"):
            r = self.s.q("SELECT value FROM meta WHERE key=?", (f"role:{k}",))
            if r:
                return r[0]["value"]
        return None

    def roles(self) -> dict:
        return {r: {"model": self.role_model(r), "explicit": bool(self.s.q("SELECT 1 FROM meta WHERE key=?", (f"role:{r}",)))}
                for r in ROLES}

    def call_role(self, role: str, prompt: str, max_tokens: int = 1500) -> str | None:
        """Run a role prompt on its configured model; None when no model is configured (callers use heuristics)."""
        model = self.role_model(role)
        if not model:
            return None
        try:
            return self.chat(model, prompt, system=f"ROLE: {role}. You are part of Commons, a multi-agent hub. "
                                                   f"Follow the output format exactly.", max_tokens=max_tokens,
                             role=role)["text"]
        except Exception as e:  # noqa: BLE001 - fall back to heuristics, but leave a trace
            self.s.audit(None, "llm.role.error", role, str(e)[:200])
            return None

    # ------------------------------------------------------------------ scores, probes, routing
    def record_score(self, model_id: str, task_type: str, score: float, cost: float = 0.0) -> None:
        self.s.x("INSERT INTO model_scores VALUES(?,?,1,?,?,?) ON CONFLICT(model_id, task_type) DO UPDATE SET "
                 "n=model_scores.n+1, score_sum=model_scores.score_sum+excluded.score_sum, cost=model_scores.cost+excluded.cost, updated_at=excluded.updated_at",
                 (model_id, task_type, score, cost, now()))

    def model_score(self, model_id: str, task_type: str) -> tuple[float, int]:
        r = self.s.q("SELECT n, score_sum FROM model_scores WHERE model_id=? AND task_type=?", (model_id, task_type))
        if not r or not r[0]["n"]:
            return 0.5, 0
        return (r[0]["score_sum"] + 0.5) / (r[0]["n"] + 1), r[0]["n"]   # smoothed

    def probe(self, model_ids: list[str] | None = None, task_types: list[str] | None = None,
              budget: float | None = None) -> dict:
        """Benchmark models (default: NEW ones) on cheap auto-checked probes per task type within a $ budget."""
        model_ids = model_ids or [m["id"] for m in self.models(new_only=True)]
        task_types = task_types or ["research", "code", "plan", "review", "summarize"]
        budget = budget if budget is not None else float(self.s.q("SELECT COALESCE((SELECT value FROM meta WHERE "
                                                                  "key='probe_budget'),'0.50') v")[0]["v"])
        spent, results = 0.0, {}
        for mid in model_ids:
            for tt in task_types:
                ok = n = 0
                for q, rx in PROBES.get(tt, []):
                    if spent >= budget:
                        break
                    r = self.chat(mid, q, system="Answer concisely.", max_tokens=64, role="probe")
                    spent += r["cost"]
                    n += 1
                    ok += bool(re.search(rx, r["text"] or ""))
                if n:
                    self.record_score(mid, tt, ok / n)
                    results.setdefault(mid, {})[tt] = round(ok / n, 2)
            self.s.x("UPDATE models SET is_new=0 WHERE id=?", (mid,))
        self.s.audit(None, "models.probe", ",".join(model_ids), {"spent": round(spent, 6), "budget": budget})
        return {"results": results, "spent": round(spent, 6), "budget": budget, "budget_exhausted": spent >= budget}

    def candidates(self) -> dict[str, list[dict]]:
        """For each task type: (agent, model) pairs with combined scores (agent learned score x model score)."""
        from .orchestrator import Profiles
        prof = Profiles(self.s)
        out: dict[str, list[dict]] = {}
        pairs = self.s.q("SELECT am.agent_id, am.model_id, a.name, a.profile FROM agent_models am JOIN agents a "
                         "ON a.id=am.agent_id")
        for tt in TASK_TYPES:
            rows = []
            for p in pairs:
                ms, n = self.model_score(p["model_id"], tt)
                asc = prof.score(dict(self.s.q("SELECT * FROM agents WHERE id=?", (p["agent_id"],))[0]), tt)
                m = self.s.q("SELECT cost_in, cost_out FROM models WHERE id=?", (p["model_id"],))
                cost = ((m[0]["cost_in"] or 0) + (m[0]["cost_out"] or 0)) if m else 0
                rows.append({"agent": p["name"], "agent_id": p["agent_id"], "model": p["model_id"],
                             "model_score": round(ms, 3), "probes": n, "agent_score": round(asc, 3),
                             "cost_per_M": round(cost, 3), "combined": round(0.6 * ms + 0.4 * asc - 0.002 * cost, 4)})
            out[tt] = sorted(rows, key=lambda r: -r["combined"])
        return out

    def refresh_routing(self, propose: bool = True) -> dict:
        """Distributor builds the routing table (task type -> agent+model). LLM when configured, else best combined score.
        Changes become proposals; applied immediately if auto-approve is on (or the table is empty)."""
        cands = self.candidates()
        choice: dict[str, dict] = {}
        llm = self.call_role("router", "Choose the best agent+model per task type, balancing quality, cost and "
                             "learned scores. Reply ONLY with JSON {task_type: {agent, model, reason}}.\n"
                             "CANDIDATES_JSON: " + json.dumps({k: v[:6] for k, v in cands.items() if v}))
        if llm:
            try:
                parsed = json.loads(re.search(r"\{.*\}", llm, re.S).group(0))
                for tt, c in parsed.items():
                    if tt in cands and any(x["model"] == c.get("model") and x["agent"] == c.get("agent") for x in cands[tt]):
                        choice[tt] = {**c, "source": "distributor"}
            except Exception:  # noqa: BLE001
                pass
        for tt, rows in cands.items():
            if tt not in choice and rows:
                choice[tt] = {"agent": rows[0]["agent"], "model": rows[0]["model"],
                              "reason": f"best combined score {rows[0]['combined']}", "source": "heuristic"}
        auto = self.auto_approve()
        applied, proposed = [], []
        for tt, c in choice.items():
            cur = self.s.q("SELECT * FROM routing WHERE task_type=?", (tt,))
            agent = self.s.get_agent(c["agent"])
            if not agent:
                continue
            if cur and cur[0]["model_id"] == c["model"] and cur[0]["agent_id"] == agent["id"]:
                continue
            if not cur or auto or not propose:
                self._apply(tt, agent["id"], c["model"], c.get("reason", ""), c["source"])
                applied.append(tt)
            else:
                pid = self._propose(tt, cur[0]["model_id"], c["model"], agent["id"],
                                    {"reason": c.get("reason"), "source": c["source"],
                                     "candidates": cands[tt][:4]})
                if pid:
                    proposed.append(pid)
        return {"applied": applied, "proposals": proposed, "table": self.routing()}

    def _apply(self, tt, agent_id, model_id, reason, source):
        prev = self.route_for(tt)
        if prev and prev.get("model_id") and prev["model_id"] != model_id and source != "revert":
            base, _n = self.model_score(prev["model_id"], tt)
            r = self.s.q("SELECT n, score_sum FROM model_scores WHERE model_id=? AND task_type=?", (model_id, tt))
            self.s.x("INSERT INTO routing_validation VALUES(?,?,?,?,?,?,?,?)",
                     (new_id("rvl"), tt, prev["model_id"], model_id, now(), base, "watching",
                      json.dumps({"n0": r[0]["n"] if r else 0, "sum0": r[0]["score_sum"] if r else 0.0,
                                  "agent_id": prev.get("agent_id")})))
        self.s.x("INSERT OR REPLACE INTO routing VALUES(?,?,?,?,?,?)", (tt, agent_id, model_id, reason, source, now()))
        self.s.relate(self.s.upsert_entity("task_type", tt), self.s.upsert_entity("model", model_id), "routed_to")
        self.s.audit(None, "routing.apply", tt, {"agent": agent_id, "model": model_id, "source": source})

    def _propose(self, tt, from_m, to_m, agent_id, evidence) -> str | None:
        if self.s.q("SELECT 1 FROM proposals WHERE task_type=? AND to_model=? AND status='proposed'", (tt, to_m)):
            return None
        pid = new_id("prp")
        self.s.x("INSERT INTO proposals VALUES(?,?,?,?,?,?,?,?,?)",
                 (pid, tt, from_m, to_m, agent_id, json.dumps(evidence, default=str), "proposed", now(), None))
        return pid

    def decide(self, proposal_id: str, approve: bool) -> dict:
        r = self.s.q("SELECT * FROM proposals WHERE id=?", (proposal_id,))
        if not r:
            raise CommonsError("unknown proposal")
        p = r[0]
        if approve:
            self._apply(p["task_type"], p["agent_id"], p["to_model"], "approved proposal", "proposal")
        self.s.x("UPDATE proposals SET status=?, decided_at=? WHERE id=?",
                 ("applied" if approve else "rejected", now(), proposal_id))
        return {"proposal": proposal_id, "status": "applied" if approve else "rejected"}

    def validate_routing(self, min_samples: int | None = None, margin: float = 0.1) -> list[dict]:
        """A/B check after a switch: compare the new model's *post-switch* scores (real task grades and probes recorded
        since the switch) with the old model's baseline. Worse by more than `margin` -> revert (auto-approve) or a
        revert proposal; better/equal -> confirmed."""
        min_samples = min_samples or int(os.environ.get("COMMONS_ROUTING_VALIDATE_N", "5"))
        out = []
        for v in self.s.q("SELECT * FROM routing_validation WHERE status='watching'"):
            meta = json.loads(v["result"] or "{}")
            r = self.s.q("SELECT n, score_sum FROM model_scores WHERE model_id=? AND task_type=?", (v["new_model"], v["task_type"]))
            n = (r[0]["n"] if r else 0) - meta.get("n0", 0)
            if n < min_samples:
                continue
            mean = ((r[0]["score_sum"] if r else 0) - meta.get("sum0", 0)) / n
            verdict = "confirmed" if mean >= (v["baseline"] or 0) - margin else "regressed"
            meta |= {"post_mean": round(mean, 3), "post_n": n}
            if verdict == "regressed":
                ev = {"baseline": v["baseline"], "post_switch_mean": mean, "samples": n, "reason": "A/B validation regression"}
                if self.auto_approve():
                    self._apply(v["task_type"], meta.get("agent_id"), v["old_model"], "A/B regression revert", "revert")
                    verdict = "reverted"
                else:
                    self._propose(v["task_type"], v["new_model"], v["old_model"], meta.get("agent_id"), ev)
                    verdict = "revert_proposed"
            self.s.x("UPDATE routing_validation SET status=?, result=? WHERE id=?", (verdict, json.dumps(meta), v["id"]))
            self.s.audit(None, "routing.validate", v["task_type"], {"verdict": verdict} | meta)
            out.append({"task_type": v["task_type"], "new": v["new_model"], "old": v["old_model"], "verdict": verdict} | meta)
        return out

    def proposals(self, status: str | None = "proposed") -> list[dict]:
        sql = "SELECT * FROM proposals" + (" WHERE status=?" if status else "") + " ORDER BY created_at DESC"
        return [dict(r) | {"evidence": json.loads(r["evidence"] or "{}"), "created_at": iso(r["created_at"]),
                           "agent": self.s.label(r["agent_id"])[1]} for r in self.s.q(sql, [status] if status else [])]

    def routing(self) -> list[dict]:
        return [{"task_type": r["task_type"], "agent": self.s.label(r["agent_id"])[1], "model": r["model_id"],
                 "reason": r["reason"], "source": r["source"], "updated_at": iso(r["updated_at"])}
                for r in self.s.q("SELECT * FROM routing ORDER BY task_type")]

    def route_for(self, task_type: str) -> dict | None:
        r = self.s.q("SELECT * FROM routing WHERE task_type=?", (task_type,)) or \
            self.s.q("SELECT * FROM routing WHERE task_type='general'")
        return dict(r[0]) if r else None

    def auto_approve(self) -> bool:
        r = self.s.q("SELECT value FROM meta WHERE key='auto_approve_routing'")
        return bool(r and r[0]["value"] == "1")

    def set_auto_approve(self, on: bool, probe_budget: float | None = None) -> dict:
        self.s.x("INSERT OR REPLACE INTO meta VALUES('auto_approve_routing', ?)", ("1" if on else "0",))
        if probe_budget is not None:
            self.s.x("INSERT OR REPLACE INTO meta VALUES('probe_budget', ?)", (str(probe_budget),))
        return {"auto_approve": on, "probe_budget": probe_budget}

    def watch(self) -> dict:
        """One maintenance tick: refresh model lists, probe NEW models within budget, rebuild routing."""
        ref = self.refresh_models()
        pr = self.probe(ref["new_models"]) if ref["new_models"] else None
        rt = self.refresh_routing()
        return {"refresh": ref, "probe": pr, "routing": rt}


# ---------------------------------------------------------------------------- mock provider
def _mock_chat(config: dict, model: str, system: str, prompt: str) -> str:
    """Deterministic fake LLM for tests and demos. Quality per model controls probe accuracy."""
    spec = next((m for m in config.get("models", []) if m["model"] == model), {})
    quality = float(spec.get("quality", 0.7))
    role = (re.search(r"ROLE: (\w+)", system) or [None, "executor"])[1]
    if role == "planner":
        return json.dumps([
            {"key": "research", "type": "research", "title": "Research the problem", "body": prompt[:200], "deps": []},
            {"key": "build", "type": "code", "title": "Build it", "body": "Implement.", "deps": ["research"],
             "gates": ["check"]},
            {"key": "check", "type": "review", "title": "Review it", "body": "Review. VERDICT required.", "deps": ["build"]}])
    if role == "router":
        cands = json.loads(prompt.split("CANDIDATES_JSON:", 1)[1])
        return json.dumps({tt: {"agent": rows[0]["agent"], "model": rows[0]["model"], "reason": f"[{model}] top pick"}
                           for tt, rows in cands.items() if rows})
    if role == "pref_extractor":
        return "\n".join(f"- {s.strip()}" for s in re.split(r"[.!\n]", prompt) if re.search(r"(?i)\b(prefer|always|never|use)\b", s))
    if role == "retro":
        ex = prompt.split("<excerpt>", 1)[-1]
        items = []
        for sent in re.split(r"(?<=[.!?])\s+|\n+", ex):
            sent = re.sub(r"^\[(user|assistant)\]\s*", "", sent.strip())
            if re.search(r"(?i)\bi prefer\b", sent):
                items.append({"kind": "preference", "text": sent, "confidence": 0.8})
            elif re.search(r"(?i)\bdecided\b", sent):
                why = re.split(r"(?i)\bbecause\b", sent)
                items.append({"kind": "decision", "text": why[0].strip(), "rationale": why[1].strip() if len(why) > 1 else "",
                              "confidence": 0.85})
            elif re.search(r"\bTODO\b", sent):
                items.append({"kind": "leftover", "text": sent.replace("TODO:", "").strip(), "task_type": "code"})
        return json.dumps(items)
    if role == "kb_summarizer":
        first = next((ln for ln in prompt.splitlines() if ln.startswith("- ")), "")
        return f"[{model} summary] {first[2:120]}"
    if role == "librarian":
        node = (re.search(r"NODE: (.*)", prompt) or [None, "this area"])[1]
        return f"[{model}] {node} holds related work items for agents."
    if role == "skill_reviewer":
        bad = re.search(r"(?i)ignore (all|previous)|exfiltrat|curl [^|]*\|\s*(ba)?sh", prompt)
        return "VERDICT: reject\nUnsafe instructions." if bad else "VERDICT: approve\nLooks safe and useful."
    answers = {"capital city of Australia": "Canberra", "symbol Fe": "Iron", "reverses a list": "xs[::-1]",
               "len('abc')": "3", "17*23": "391", "Order these steps": "write code, test, deploy",
               "Find the bug": "It should use + instead of subtract.", "Is this SQL safe": "No",
               "Summarize in one word": "cat", "2-word title": "PostgreSQL setup"}
    for k, v in answers.items():
        if k in prompt:
            h = int(hashlib.sha256(f"{model}|{prompt}".encode()).hexdigest(), 16) % 100
            return v if h < quality * 100 else "I am not sure."
    verdict = "\nVERDICT: approve" if re.search(r"(?i)verdict", prompt) else ""
    return f"[{model}] completed: {prompt.strip().splitlines()[0][:100] if prompt.strip() else ''}{verdict}"
