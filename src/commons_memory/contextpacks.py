"""Shared context store: content-addressed, deduplicated, layered context packs.

A pack snapshots an agent's working context so a handoff or subtask passes an *id* instead of re-sending text.
Layers (load only what you need, within max_tokens / the receiving model's window):
  L0  gist      — one paragraph
  L1  brief     — structured (goal, state, decisions, open threads, next steps)
  L2  detailed  — titled sections (notes, file excerpts, transcripts summaries)
  L3  refs      — raw references (item ids, file paths + hashes, urls), never inlined; fetch on demand
Sections are stored as content-addressed blobs (sha256), so repeated snapshots only store what changed (delta packs),
and identical sections are shared across packs/agents.

Cache-friendly assembly: `load()` emits a stable *prefix* (header + L0 + L1, deterministic ordering and formatting)
followed by the variable part. Pass `prefix` as `cache_prefix` to ProviderLayer.chat so Anthropic cache_control /
OpenAI / OpenRouter automatic prefix caching can reuse it; cached tokens are tracked in usage.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re

from .ids import iso, new_id, now
from .redact import redact
from .store import CommonsError, Store

DEFAULT_CTX = {"claude": 200_000, "gpt-4o": 128_000, "gpt-4.1": 1_000_000, "gpt-5": 400_000, "gemini": 1_000_000,
               "grok": 256_000, "llama": 128_000, "mistral": 128_000, "qwen": 128_000, "deepseek": 128_000, "o3": 200_000,
               "o4": 200_000}
LAYERS = ("L0", "L1", "L2", "L3")
BRIEF_KEYS = ("goal", "state", "decisions", "open_threads", "next_steps", "constraints")


_ENC: dict[str, object] = {}


def _tiktoken(m: str):
    try:
        import tiktoken  # optional: pip install commons-memory[tokens]
    except ImportError:
        return None
    name = "cl100k_base" if any(k in m for k in ("gpt-4-", "gpt-4 ", "gpt-3.5", "text-embedding")) or m.endswith("gpt-4") \
        else "o200k_base"
    if name not in _ENC:
        try:
            _ENC[name] = tiktoken.get_encoding(name)
        except Exception:  # noqa: BLE001 - encoding files unavailable offline
            _ENC[name] = None
    return _ENC[name]


def count_tokens(text: str, model: str | None = None, store: Store | None = None, exact: bool | None = None) -> int:
    """Token count for `model`.
    * OpenAI-family models: exact local count with tiktoken (o200k_base / cl100k_base) when installed.
    * Anthropic / Gemini: exact counts from the provider's count-tokens API when `exact` (or COMMONS_EXACT_TOKENS=1)
      and a provider key is configured (results cached by text hash) — these are network calls, so off by default.
    * Everything else: calibrated chars/token estimate (code/JSON denser than prose)."""
    if not text:
        return 0
    m = (model or "").lower()
    if any(k in m for k in ("gpt", "o1", "o3", "o4", "openai", "chatgpt")):
        enc = _tiktoken(m)
        if enc is not None:
            return len(enc.encode(text, disallowed_special=()))
    import os
    if store is not None and model and (exact if exact is not None else os.environ.get("COMMONS_EXACT_TOKENS") == "1"):
        try:
            from .providers import ProviderLayer
            n = ProviderLayer(store).count_tokens(model, text)
            if n:
                return n
        except Exception:  # noqa: BLE001 - fall back to estimate
            pass
    ratio = 3.5 if "claude" in m or "anthropic" in m else 4.0 if any(k in m for k in ("gpt", "gemini", "grok")) else 3.8
    if sum(text.count(c) for c in "{}[]();=<>") > len(text) / 40:
        ratio *= 0.85
    return max(1, int(len(text) / ratio + 0.5))


def context_window(store: Store, model: str | None) -> int:
    if not model:
        return 128_000
    r = store.q("SELECT context FROM models WHERE id=? OR model=?", (model, model.split("/", 1)[-1]))
    if r and r[0]["context"]:
        return int(r[0]["context"])
    m = model.lower()
    for k, v in DEFAULT_CTX.items():
        if k in m:
            return v
    return 128_000


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class ContextStore:
    def __init__(self, store: Store):
        self.s = store

    def _put(self, text: str) -> tuple[str, bool]:
        text, _ = redact(text)
        h = _h(text)
        new = not self.s.q("SELECT 1 FROM context_blobs WHERE hash=?", (h,))
        if new:
            self.s.x("INSERT INTO context_blobs VALUES(?,?,?,?)", (h, text, len(text), now()))
        return h, new

    def _get(self, h: str) -> str:
        r = self.s.q("SELECT content FROM context_blobs WHERE hash=?", (h,))
        return r[0]["content"] if r else ""

    @staticmethod
    def _brief_text(brief: dict | str) -> str:
        if isinstance(brief, str):
            return brief.strip()
        out = []
        for k in BRIEF_KEYS + tuple(sorted(k for k in brief if k not in BRIEF_KEYS)):  # stable ordering
            v = brief.get(k)
            if not v:
                continue
            out.append(f"## {k.replace('_', ' ').title()}")
            out += [f"- {x}" for x in v] if isinstance(v, list) else [str(v)]
        return "\n".join(out)

    # ------------------------------------------------------------------ save
    def save(self, name: str, gist: str, brief: dict | str | None = None, sections: list[dict] | dict | None = None,
             refs: list | None = None, *, agent_id: str | None = None, project: str | None = None,
             session_id: str | None = None, parent: str | None = "auto", delta: bool = False) -> dict:
        """Snapshot context. With delta=True, `sections` only contains changed/new sections (others inherit from parent;
        a section with text None is removed)."""
        if not gist or not gist.strip():
            raise CommonsError("gist (L0) is required")
        if parent == "auto":
            r = self.s.q("SELECT id FROM context_packs WHERE name=? ORDER BY created_at DESC LIMIT 1", (name,))
            parent = r[0]["id"] if r else None
        par = self._pack(parent) if parent else None
        if isinstance(sections, dict):
            sections = [{"title": k, "text": v} for k, v in sections.items()]
        sections = sections or []
        order: list[tuple[str, str]] = []
        new_chars = reused_chars = 0
        inherited = json.loads(par["sections"]) if (par and delta) else []
        given = {s_["title"]: s_.get("text") for s_ in sections}
        titles = [t for t, _ in inherited] + [s_["title"] for s_ in sections if s_["title"] not in {t for t, _ in inherited}]
        for t in titles:  # keep first-appearance order across the lineage -> stable prefix/ordering
            if t in given:
                if given[t] is None:
                    continue
                h, new = self._put(str(given[t]))
            else:
                h, new = dict(inherited)[t], False
            ln = self.s.q("SELECT chars FROM context_blobs WHERE hash=?", (h,))[0]["chars"]
            new_chars += ln if new else 0
            reused_chars += 0 if new else ln
            order.append((t, h))
        bt = self._brief_text(brief) if brief is not None else (self._get(par["brief_hash"]) if par else "")
        bh, bnew = self._put(bt)
        gist_r, _ = redact(gist.strip())
        refs = refs if refs is not None else (json.loads(par["refs"]) if par and delta else [])
        clean_refs = []
        for r_ in refs:  # L3: references only — strip any inlined bodies
            if isinstance(r_, dict):
                clean_refs.append({k: v for k, v in r_.items() if k in ("id", "path", "hash", "url", "lines", "note", "kind")})
            else:
                clean_refs.append(str(r_)[:300])
        prefix = self._prefix(name, gist_r, bt)
        tok = {"L0": count_tokens(gist_r), "L1": count_tokens(bt),
               "L2": sum(count_tokens(self._get(h)) for _, h in order), "L3": count_tokens(json.dumps(clean_refs))}
        stats = {"tokens": tok, "full_tokens": sum(tok.values()), "new_chars": new_chars + (len(bt) if bnew else 0),
                 "reused_chars": reused_chars, "sections": len(order), "delta": bool(par)}
        pid = new_id("ctx")
        self.s.x("INSERT INTO context_packs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (pid, name, agent_id, self.s.project_id(project) if project else (par["project_id"] if par else None),
                  par["id"] if par else None, session_id, now(), gist_r, bh, json.dumps(order), json.dumps(clean_refs),
                  _h(prefix), json.dumps(stats)))
        ent = self.s.upsert_entity("context_pack", f"{name}@{pid[-6:]}")
        self.s.relate(pid, ent, "is")
        if par:
            self.s.relate(pid, par["id"], "delta_of")
        if session_id:
            self.s.relate(pid, session_id, "snapshot_of")
        if agent_id:
            self.s.relate(pid, agent_id, "authored_by")
        for r_ in clean_refs:
            if isinstance(r_, dict) and r_.get("id"):
                self.s.relate(pid, r_["id"], "references")
        return {"pack_id": pid, "parent": par["id"] if par else None, **stats}

    def _pack(self, pid: str) -> dict:
        r = self.s.q("SELECT * FROM context_packs WHERE id=?", (pid,))
        if not r:
            r = self.s.q("SELECT * FROM context_packs WHERE name=? ORDER BY created_at DESC LIMIT 1", (pid,))
        if not r:
            raise CommonsError(f"unknown context pack {pid}")
        return dict(r[0])

    @staticmethod
    def _prefix(name: str, gist: str, brief: str) -> str:
        # deterministic, timestamp-free -> identical bytes across agents/loads -> provider prompt-cache hits
        return f"# Shared context: {name}\n\n## Gist (L0)\n{gist}\n\n## Brief (L1)\n{brief}\n"

    # ------------------------------------------------------------------ load / fit
    def load(self, pack: str, layer: str = "L1", max_tokens: int = 4000, model: str | None = None,
             sections: list[str] | None = None, agent_id: str | None = None) -> dict:
        if layer not in LAYERS:
            raise CommonsError("layer must be L0, L1, L2 or L3")
        p = self._pack(pack)
        self.s.check_project(p["project_id"])
        st = json.loads(p["stats"])
        brief = self._get(p["brief_hash"])
        budget = max_tokens
        if model:
            budget = min(budget, int(context_window(self.s, model) * 0.5))
        if layer == "L0":
            prefix, body = "", f"# Shared context: {p['name']}\n{p['gist']}"
        else:
            prefix, body = self._prefix(p["name"], p["gist"], brief), ""
        used = count_tokens(prefix + body, model, self.s)
        included, omitted = [], []
        if layer in ("L2", "L3"):
            for t, h in json.loads(p["sections"]):
                if sections and t not in sections:
                    omitted.append(t)
                    continue
                txt = f"\n## {t}\n{self._get(h)}\n"
                tk = count_tokens(txt, model, self.s)
                if used + tk > budget:
                    omitted.append(t)
                    continue
                body += txt
                used += tk
                included.append(t)
        if layer == "L3":
            refs = json.loads(p["refs"])
            txt = "\n## References (L3; fetch on demand with expand/read)\n" + "\n".join(
                "- " + (json.dumps(r, separators=(",", ":")) if isinstance(r, dict) else str(r)) for r in refs)
            if used + count_tokens(txt, model, self.s) <= budget:
                body += txt
                used += count_tokens(txt, model, self.s)
        if used > budget and layer != "L0":  # even the brief doesn't fit -> fall back to L0
            return self.load(pack, "L0", max_tokens, model, agent_id=agent_id) | {"fallback": "L0"}
        self.s.x("INSERT INTO context_loads(pack_id,agent_id,layer,model,tokens,full_tokens,ts) VALUES(?,?,?,?,?,?,?)",
                 (p["id"], agent_id, layer, model, used, st["full_tokens"], now()))
        return {"pack_id": p["id"], "name": p["name"], "layer": layer, "tokens": used, "budget": budget,
                "full_tokens": st["full_tokens"], "prefix": prefix, "prefix_hash": p["prefix_hash"] if prefix else None,
                "text": prefix + body, "included": included, "omitted": omitted,
                "hint": "pass `prefix` as the cache prefix; load L2 sections=[...] or L3 refs only when needed"}

    def fit(self, pack: str, model: str, reserve: int = 8000, share: float = 0.5) -> dict:
        """Pick the deepest layer that fits `share` of the model's window minus a reserve for the task + answer."""
        win = context_window(self.s, model)
        budget = max(256, int(win * share) - reserve)
        st = json.loads(self._pack(pack)["stats"])["tokens"]
        cum = {"L0": st["L0"], "L1": st["L0"] + st["L1"], "L2": st["L0"] + st["L1"] + st["L2"],
               "L3": sum(st.values())}
        best = next((lyr for lyr in ("L3", "L2", "L1", "L0") if cum[lyr] <= budget), "L0")
        return {"model": model, "context_window": win, "budget": budget, "layer": best, "layer_tokens": cum,
                "load": self.load(pack, best, budget, model)}

    # ------------------------------------------------------------------ diff / list
    def diff(self, a: str, b: str | None = None) -> dict:
        pb = self._pack(b) if b else self._pack(a)
        pa = self._pack(a) if b else (self._pack(pb["parent_id"]) if pb["parent_id"] else None)
        if not pa:
            return {"from": None, "to": pb["id"], "added": [t for t, _ in json.loads(pb["sections"])], "removed": [],
                    "changed": [], "gist_changed": True}
        sa, sb = dict(json.loads(pa["sections"])), dict(json.loads(pb["sections"]))
        changed = []
        for t in sb:
            if t in sa and sa[t] != sb[t]:
                changed.append({"title": t, "diff": "".join(difflib.unified_diff(
                    self._get(sa[t]).splitlines(True), self._get(sb[t]).splitlines(True), pa["id"], pb["id"]))[:4000]})
        bd = "".join(difflib.unified_diff(self._get(pa["brief_hash"]).splitlines(True),
                                          self._get(pb["brief_hash"]).splitlines(True), "brief", "brief"))
        return {"from": pa["id"], "to": pb["id"], "added": [t for t in sb if t not in sa],
                "removed": [t for t in sa if t not in sb], "changed": changed, "brief_diff": bd[:3000],
                "gist_changed": pa["gist"] != pb["gist"]}

    def packs(self, limit: int = 100) -> list[dict]:
        out = []
        for r in self.s.q("SELECT * FROM context_packs ORDER BY created_at DESC LIMIT ?", (limit,)):
            st = json.loads(r["stats"])
            loads = self.s.q("SELECT COUNT(*) n, COALESCE(SUM(full_tokens-tokens),0) saved, COALESCE(SUM(tokens),0) t "
                             "FROM context_loads WHERE pack_id=?", (r["id"],))[0]
            out.append({"id": r["id"], "name": r["name"], "parent": r["parent_id"], "created_at": iso(r["created_at"]),
                        "agent": self.s.label(r["agent_id"])[1] if r["agent_id"] else None, "gist": r["gist"][:200],
                        "tokens": st["tokens"], "full_tokens": st["full_tokens"], "new_chars": st["new_chars"],
                        "reused_chars": st["reused_chars"], "sections": st["sections"], "loads": loads["n"],
                        "tokens_saved": loads["saved"], "prefix_hash": (r["prefix_hash"] or "")[:12]})
        return out

    def savings(self) -> dict:
        r = self.s.q("SELECT COUNT(*) n, COALESCE(SUM(full_tokens-tokens),0) saved, COALESCE(SUM(tokens),0) loaded "
                     "FROM context_loads")[0]
        c = self.s.q("SELECT COALESCE(SUM(cached_tokens),0) c FROM usage")[0]["c"]
        b = self.s.q("SELECT COUNT(*) n, COALESCE(SUM(chars),0) ch FROM context_blobs")[0]
        refs = self.s.q("SELECT sections FROM context_packs")
        total_refs = sum(len(json.loads(x["sections"])) for x in refs)
        return {"loads": r["n"], "tokens_loaded": r["loaded"], "tokens_saved_by_layering": r["saved"],
                "provider_cached_tokens": c, "blobs": b["n"], "blob_chars": b["ch"],
                "section_refs": total_refs, "dedup_ratio": round(total_refs / b["n"], 2) if b["n"] else None}


def gist_of(text: str, n: int = 400) -> str:
    return re.sub(r"\s+", " ", text).strip()[:n]
