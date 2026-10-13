"""First-run onboarding for the hub (the dashboard's Setup page): where every key goes, who decides on credential use,
which features are on, and a checklist of what is still missing. Keys entered here are stored in the vault
(encrypted); providers and judges reference them as commons://NAME, so no key sits in an env file or the compose."""
from __future__ import annotations

import json

from . import settings
from . import vault as V
from .store import CommonsError, Store

# kind -> (label, vault auth spec, extra headers, needs a key)
PROVIDERS = {
    "typesafe": ("TypeSafe (Jev): fast yes/no judge, ~150 ms", "bearer", [], True),
    "anthropic": ("Anthropic (Claude)", "header:x-api-key", ["anthropic-version:2023-06-01"], True),
    "openai": ("OpenAI", "bearer", [], True),
    "google": ("Google (Gemini)", "header:x-goog-api-key", [], True),
    "xai": ("xAI (Grok)", "bearer", [], True),
    "openrouter": ("OpenRouter", "bearer", [], True),
    "ollama": ("Ollama (local models, no key)", "bearer", [], False),
}
BASES = {"typesafe": "https://api.typesafe.ai/v1"}


def _cred(kind: str) -> str:
    return "typesafe" if kind == "typesafe" else f"{kind}_api"


def set_provider(s: Store, kind: str, key: str = "", base_url: str = "") -> dict:
    """Store a provider's key in the vault and register the provider for LLM roles and the decision chain."""
    if kind not in PROVIDERS:
        raise CommonsError(f"provider is one of {', '.join(PROVIDERS)}")
    from .providers import KINDS, ProviderLayer
    label, auth, extra, needs_key = PROVIDERS[kind]
    base = (base_url or BASES.get(kind) or KINDS[kind]).rstrip("/")
    if needs_key:
        if not (key or "").strip():
            raise CommonsError(f"{label} needs its API key")
        V.add(s, _cred(kind), key, kind, [base], V.parse_auth(auth, extra), f"{label} API key (entered on Setup)", "high")
    if kind != "typesafe":
        pl = ProviderLayer(s)
        s.x("DELETE FROM providers WHERE name=?", (kind,))
        pl.add_provider(kind, kind, key_ref=f"commons://{_cred(kind)}" if needs_key else None, base_url=base)
    s.audit(None, "setup.provider", kind, {"base_url": base})
    return {"provider": kind, "ready": True}


def remove_provider(s: Store, kind: str) -> dict:
    V.remove(s, _cred(kind))
    s.x("DELETE FROM providers WHERE name=?", (kind,))
    return {"provider": kind, "removed": True}


def _judge_ready(s: Store, entry: str) -> bool:
    import os
    if entry == "ask":
        return True
    if entry.startswith("agent:"):
        return bool(V._agent_judges(s, entry, ""))
    if entry == "typesafe":
        return bool(os.environ.get("TYPESAFE_API_KEY")) or bool(s.q("SELECT 1 FROM vault_creds WHERE name='typesafe'"))
    from .providers import ProviderLayer
    model = ProviderLayer(s).role_model(entry[5:]) if entry.startswith("role:") else entry
    return bool(model) and bool(s.q("SELECT 1 FROM providers WHERE name=? AND enabled=1", (model.split("/")[0],)))


def status(s: Store) -> dict:
    import os
    from .handover import status as hstatus
    try:
        V._aes(s)
        vault_ok, vault_err = True, ""
    except Exception as e:  # noqa: BLE001
        vault_ok, vault_err = False, str(e)
    chain = settings.get(s, "judge_chain")
    judges = [{"entry": e, "ready": _judge_ready(s, e)} for e in chain]
    creds = V.listing(s)
    for c in creds:
        r = s.q("SELECT deposited_by FROM vault_creds WHERE name=?", (c["credential"],))
        c["from"] = s.label(r[0]["deposited_by"])[1] if r and r[0]["deposited_by"] else "admin"
    agents = []
    for a in s.q("SELECT id, name, last_seen FROM agents WHERE name NOT IN ('cli','user','librarian','commons',"
                 "'dashboard-user','reviewer') ORDER BY name"):
        h = hstatus(s, a["id"])
        agents.append({"name": a["name"], "connected": bool(a["last_seen"]), "handover_complete": h["complete"],
                       "missing": [m["item"] for m in h["missing"]]})
    providers = []
    for kind, (label, _a, _e, needs_key) in PROVIDERS.items():
        ready = (bool(os.environ.get("TYPESAFE_API_KEY")) or bool(s.q("SELECT 1 FROM vault_creds WHERE name='typesafe'"))) \
            if kind == "typesafe" else bool(s.q("SELECT 1 FROM providers WHERE name=?", (kind,)))
        providers.append({"kind": kind, "label": label, "needs_key": needs_key, "ready": ready})
    user_creds = [c for c in creds if c["credential"] not in {_cred(k) for k in PROVIDERS}]
    connected = [a for a in agents if a["connected"]]
    steps = [
        {"step": "Vault ready (keys stored encrypted)", "done": vault_ok, "detail": V.key_source(s) if vault_ok else vault_err},
        {"step": "Decision agent can answer", "done": any(j["ready"] and j["entry"] != "ask" for j in judges),
         "detail": "add a TypeSafe or LLM provider key, or keep 'ask' and decide every credential use yourself"},
        {"step": "Credentials added for your services", "done": bool(user_creds), "detail": f"{len(user_creds)} in the vault"},
        {"step": "Agents connected", "done": bool(connected), "detail": f"{len(connected)} connected"},
        {"step": "Every connected agent handed everything over", "done": bool(connected) and all(a["handover_complete"] for a in connected),
         "detail": f"{sum(a['handover_complete'] for a in connected)}/{len(connected)} complete"},
        {"step": "Features reviewed", "done": bool(s.q("SELECT 1 FROM meta WHERE key LIKE 'setting:%'")),
         "detail": "all are on by default; save once to confirm"},
    ]
    return {"steps": steps, "done": sum(x["done"] for x in steps), "total": len(steps), "judges": judges,
            "providers": providers, "credentials": user_creds, "settings": settings.listing(s), "agents": agents,
            "decisions": V.decisions(s, 15)}
