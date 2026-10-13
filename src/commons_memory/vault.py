"""Commons is the credential vault: it stores the user's keys (encrypted, like sesame) and a decision agent decides,
case by case, which agent may use which key, how, and whether to read or write. Agents never see a value.

Storage: each value is AES-GCM encrypted in the database (bound to its name); the master key (COMMONS_VAULT_KEY,
base64 of 32 bytes) lives outside the database in a root-only env file, so a copied database or backup holds no usable
secrets. Values are written only by the admin (`commons vault set NAME < value` on the hub), never over MCP.

Use: an agent calls use_credential(credential, method, url, justification). Hard rules first, never overridden: the key
only goes to its own service's base URLs (no exfiltration), values are never returned, responses are scrubbed of the
value. Then the decision agent judges the request against the justification, the user's asks on the board and the
agent's own work: a sound read gets a short grant; a write needs a justification the user's requests back; anything
destructive, costly or that messages people goes to the user as a board question ("Needs you"); with no judge
available it fails closed. Commons makes the call itself (the hub sits on the LAN) and returns only the response.
Every decision is logged with its scores and reason.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

from .ids import iso, new_id, now
from .store import CommonsError, Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS vault_creds(name TEXT PRIMARY KEY, service TEXT, description TEXT, base_urls TEXT, auth TEXT,
  risk TEXT, secret BLOB, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS vault_grants(id TEXT PRIMARY KEY, agent_id TEXT, cred TEXT, scope TEXT, method TEXT, url TEXT,
  decision_id TEXT, expires_at REAL, uses INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS vault_decisions(id TEXT PRIMARY KEY, agent_id TEXT, cred TEXT, method TEXT, url TEXT,
  justification TEXT, scope TEXT, verdict TEXT, scores TEXT, reason TEXT, question_id TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS vault_grants_a ON vault_grants(agent_id, cred, expires_at);
"""
READ = ("GET", "HEAD")
GRANT_S = 3600
QUESTIONS = {
    "legit": "Is the justification specific, and would this exact HTTP call plausibly serve it? (A vague reason, or a "
             "call unrelated to the reason, is no.)",
    "destructive": "Could this call delete or overwrite data, spend money, send messages or email to people, or change "
                   "access, security or account settings?",
    "user_backed": "Is this change something the user clearly asked for, according to the user's recent requests listed?",
}


def migrate(db) -> None:
    db.executescript(SCHEMA)
    from .facts import add_columns
    add_columns(db, [("vault_creds", "deposited_by", "TEXT")])


def parse_auth(spec: str, extra_headers: list[str] | None = None) -> dict:
    """bearer | basic:USER | header:NAME | header:NAME:PREFIX | query:PARAM"""
    kind, _, rest = (spec or "bearer").partition(":")
    name, _, prefix = rest.partition(":")
    auth = {"bearer": True} if kind == "bearer" else {"query": name} if kind == "query" else \
        {"basic": name} if kind == "basic" else {"header": name, **({"prefix": prefix} if prefix else {})}
    if extra_headers:
        auth["extra_headers"] = dict(h.split(":", 1) for h in extra_headers)
    return auth


def deposit(s: Store, agent_id: str, name: str, value: str, service: str, base_urls: list[str], auth: str = "bearer",
            extra_headers: list[str] | None = None, description: str = "", risk: str = "normal") -> dict:
    """An agent hands over a key it holds. It can add new ones and update its own, never replace anyone else's."""
    r = s.q("SELECT deposited_by FROM vault_creds WHERE name=?", (name,))
    if r and r[0]["deposited_by"] != agent_id:
        raise CommonsError(f"{name} is already in the vault (from {s.label(r[0]['deposited_by'])[1] if r[0]['deposited_by'] else 'the admin'}); "
                           "pick another name if this is a different key")
    out = add(s, name, value, service, base_urls, parse_auth(auth, extra_headers), description, risk)
    s.x("UPDATE vault_creds SET deposited_by=? WHERE name=?", (agent_id, name))
    s.audit(agent_id, "vault.deposit", name, {"service": service})
    return out


# ------------------------------------------------------------------------------------------------ storage
def _aes():
    import base64
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    try:
        key = base64.b64decode(os.environ.get("COMMONS_VAULT_KEY", ""), validate=True)
    except ValueError:
        key = b""
    if len(key) != 32:
        raise CommonsError("the vault is locked: set COMMONS_VAULT_KEY (base64 of 32 random bytes) in the hub's env file")
    return AESGCM(key)


def _seal(name: str, value: str) -> bytes:
    nonce = os.urandom(12)
    return nonce + _aes().encrypt(nonce, value.encode(), name.encode())  # name as AAD: a row can't be swapped


def _value(s: Store, name: str) -> str:
    r = s.q("SELECT secret FROM vault_creds WHERE name=?", (name,))
    if not r or not r[0]["secret"]:
        raise CommonsError(f"credential {name} has no value stored (commons vault set {name} on the hub)")
    blob = bytes(r[0]["secret"])
    aes = _aes()
    try:
        return aes.decrypt(blob[:12], blob[12:], name.encode()).decode()
    except Exception as e:  # noqa: BLE001 - wrong master key or tampered row
        raise CommonsError(f"credential {name} can't be decrypted with this vault key") from e


def add(s: Store, name: str, value: str, service: str, base_urls: list[str], auth: dict, description: str = "",
        risk: str = "normal") -> dict:
    """Admin only (CLI on the hub): store a credential. auth: {"header": "X-Plex-Token"} | {"query": "apikey"} |
    {"bearer": true} | {"basic": "username"} (value = password) | {"header": "Authorization", "prefix": "Token "},
    optional "extra_headers"."""
    if not re.fullmatch(r"[a-z0-9_]{2,40}", name or ""):
        raise CommonsError("name: 2-40 chars of a-z, 0-9, _")
    value = (value or "").strip()
    if not value:
        raise CommonsError("empty value (pipe it on stdin)")
    urls = [u.rstrip("/") + "/" for u in base_urls if urllib.parse.urlparse(u).scheme in ("http", "https")]
    if not urls:
        raise CommonsError("give at least one http(s) base URL the key may be sent to")
    if not any(k in auth for k in ("header", "query", "bearer", "basic")):
        raise CommonsError("auth needs header, query, bearer or basic")
    if risk not in ("low", "normal", "high"):
        raise CommonsError("risk is low, normal or high")
    s.x("INSERT OR REPLACE INTO vault_creds(name,service,description,base_urls,auth,risk,secret,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,COALESCE((SELECT created_at FROM vault_creds WHERE name=?),?),?)",
        (name, service, description, json.dumps(urls), json.dumps(auth), risk, _seal(name, value), name, now(), now()))
    s.audit(None, "vault.set", name, {"service": service, "base_urls": urls, "risk": risk})
    return {"name": name, "service": service, "base_urls": urls, "risk": risk, "stored": True}


def remove(s: Store, name: str) -> dict:
    s.x("DELETE FROM vault_creds WHERE name=?", (name,))
    s.x("DELETE FROM vault_grants WHERE cred=?", (name,))
    s.audit(None, "vault.remove", name)
    return {"name": name, "removed": True}


def listing(s: Store) -> list[dict]:
    return [{"credential": r["name"], "service": r["service"], "description": r["description"],
             "base_urls": json.loads(r["base_urls"]), "risk": r["risk"], "stored": bool(r["secret"])}
            for r in s.q("SELECT name, service, description, base_urls, risk, secret FROM vault_creds ORDER BY name")]


# ------------------------------------------------------------------------------------------------ decision agent
def _context(s: Store, agent_id: str) -> str:
    asks = [r["text"][:200] for r in s.q("SELECT text FROM board WHERE kind IN ('ask','question') "
                                         "ORDER BY updated_at DESC LIMIT 12")]
    work = [r["what"][:160] for r in s.q("SELECT what FROM work_claims WHERE agent_id=? AND status='active' "
                                         "ORDER BY started_at DESC LIMIT 5", (agent_id,))]
    said = [r["content"][:200] for r in s.q("SELECT content FROM items WHERE agent_id=? AND tags LIKE ? AND created_at>? "
                                            "ORDER BY created_at DESC LIMIT 6", (agent_id, '%"user"%', now() - 7200))]
    asks = said + asks  # what the user said to this agent in the last two hours (captured verbatim), then the board
    return ("User's recent requests:\n" + "\n".join(f"- {a}" for a in asks or ["(none recorded)"])
            + "\nThis agent's current work:\n" + "\n".join(f"- {w}" for w in work or ["(none declared)"]))


def judge(s: Store, agent_id: str, cred: dict, method: str, url: str, justification: str, body: str = "") -> dict:
    """{verdict: allow|deny|ask_user, scope, scores, via, reason}."""
    scope = "read" if method in READ else "write"
    state = (f"An AI agent named {s.label(agent_id)[1]} asks to use the user's {cred['service']} credential "
             f"({cred['description'] or cred['name']}).\nRequested call: {method} {url}\n"
             + (f"Request body: {body[:1500]}\n" if body else "")
             + f"Agent's justification: {justification}\n\n{_context(s, agent_id)}")
    qs = QUESTIONS if scope == "write" else {"legit": QUESTIONS["legit"]}
    scores, via = None, None
    from . import typesafe
    if typesafe.enabled():
        try:
            scores, via = typesafe.noul(state, qs), "typesafe"
        except Exception as e:  # noqa: BLE001 - try the LLM role next
            s.audit(None, "vault.judge.error", cred["name"], str(e)[:200])
    if scores is None:
        from .providers import ProviderLayer
        out = ProviderLayer(s).call_role("gatekeeper", state + "\n\nFor each question give the probability (0-1) that "
                                         "the answer is yes, as JSON with these keys:\n" + json.dumps(qs), max_tokens=200)
        if out:
            try:
                j = json.loads(out[out.find("{"): out.rfind("}") + 1])
                scores, via = {k: float(j[k]) for k in qs}, "llm"
            except (ValueError, KeyError, TypeError):
                scores = None
    if scores is None:
        return {"verdict": "ask_user", "scope": scope, "scores": {}, "via": None,
                "reason": "no decision model is available on the hub, so the user decides"}
    legit = scores.get("legit", 0)
    if legit < 0.3:
        return {"verdict": "deny", "scope": scope, "scores": scores, "via": via,
                "reason": "the justification doesn't support this call; say exactly what you need it for"}
    if scope == "read":
        if legit >= 0.7 or cred["risk"] == "low":
            return {"verdict": "allow", "scope": scope, "scores": scores, "via": via, "reason": "a read with a sound reason"}
        return {"verdict": "ask_user", "scope": scope, "scores": scores, "via": via, "reason": "the reason is unclear"}
    if scores.get("destructive", 1) >= 0.3 or cred["risk"] == "high":
        return {"verdict": "ask_user", "scope": scope, "scores": scores, "via": via,
                "reason": "a change that could delete data, cost money, message people or touch security: the user decides"}
    if legit >= 0.7 and scores.get("user_backed", 0) >= 0.7:
        return {"verdict": "allow", "scope": scope, "scores": scores, "via": via,
                "reason": "a non-destructive change the user asked for (this one call only)"}
    return {"verdict": "ask_user", "scope": scope, "scores": scores, "via": via,
            "reason": "the user's requests don't clearly cover this change"}


def _in_scope(cred: dict, url: str) -> bool:
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or "@" in p.netloc or ".." in p.path:
        return False
    return any(url.startswith(b) or url.rstrip("/") + "/" == b for b in json.loads(cred["base_urls"]))


def _grant(s: Store, agent_id: str, cred: str, method: str, url: str) -> dict | None:
    scope = "read" if method in READ else "write"
    for g in s.q("SELECT * FROM vault_grants WHERE agent_id=? AND cred=? AND expires_at>? ORDER BY expires_at DESC",
                 (agent_id, cred, now())):
        if g["scope"] == "read" and scope == "read":
            return dict(g)
        if g["scope"] == "write" and g["method"] == method and g["url"] == url and not g["uses"]:
            return dict(g)  # a write grant covers exactly one call
    return None


def call(s: Store, cred: dict, method: str, url: str, body: str = "", content_type: str = "", timeout: float = 30) -> dict:
    """Make the HTTP call with the key attached; return the status and a scrubbed body."""
    value, auth = _value(s, cred["name"]), json.loads(cred["auth"])
    headers = {"Accept": "application/json", "User-Agent": "commons-vault"} | (auth.get("extra_headers") or {})
    if auth.get("query"):
        url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode({auth["query"]: value})
    elif auth.get("bearer"):
        headers["Authorization"] = "Bearer " + value
    elif auth.get("basic"):
        import base64
        headers["Authorization"] = "Basic " + base64.b64encode(f"{auth['basic']}:{value}".encode()).decode()
    else:
        headers[auth["header"]] = auth.get("prefix", "") + value
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body.encode() if body else None, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read(400_000)
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read(20_000)
    except (urllib.error.URLError, OSError) as e:
        return {"status": 0, "error": str(getattr(e, "reason", e)).replace(value, "[credential]")[:300]}
    from .redact import redact
    text = raw.decode("utf-8", "replace").replace(value, "[credential]")
    return {"status": status, "body": redact(text)[0][:60_000], "truncated": len(raw) >= 400_000}


def use(s: Store, agent_id: str, credential: str, method: str, url: str, justification: str, body: str = "",
        content_type: str = "") -> dict:
    method = (method or "GET").upper()
    r = s.q("SELECT name, service, description, base_urls, auth, risk FROM vault_creds WHERE name=?", (credential,))
    if not r:
        raise CommonsError(f"unknown credential {credential!r}; credentials() lists what exists")
    cred = dict(r[0])
    if not _in_scope(cred, url):
        s.audit(agent_id, "vault.refused", credential, {"url": url[:300], "why": "outside its base URLs"})
        raise CommonsError(f"{credential} may only be sent to {', '.join(json.loads(cred['base_urls']))}")
    if len((justification or "").strip()) < 15:
        raise CommonsError("justify the request: what you need this call for (the user's ask or your task)")
    from .redact import redact
    justification = redact(justification)[0][:1000]
    g = _grant(s, agent_id, credential, method, url)
    d = ({"verdict": "allow", "scope": g["scope"], "scores": {}, "via": "grant", "reason": f"within grant {g['id']}"}
         if g else judge(s, agent_id, cred, method, url, justification, body))
    did, qid = new_id("vdc"), None
    if d["verdict"] == "ask_user":
        from . import board as B
        qid = B.add(s, "question", f"[vault {did}] Allow {s.label(agent_id)[1]} to {method} {url[:200]} with your "
                                   f"{cred['service']} key? Their reason: {justification[:300]} (Commons: {d['reason']}). "
                                   "Answer yes or no.", project="commons-dev", path="vault", agent_id=agent_id)["id"]
    s.x("INSERT INTO vault_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (did, agent_id, credential, method, url[:1000], justification, d["scope"], d["verdict"], json.dumps(d["scores"]),
         d["reason"], qid, now()))
    s.audit(agent_id, f"vault.{d['verdict']}", credential, {"decision": did, "method": method, "url": url[:300],
                                                            "via": d.get("via")})
    if d["verdict"] == "deny":
        return {"verdict": "denied", "decision_id": did, "reason": d["reason"]}
    if d["verdict"] == "ask_user":
        return {"verdict": "waiting_for_user", "decision_id": did, "question_id": qid, "reason": d["reason"],
                "do": "The user has the question under Needs you; the answer arrives in msg_recv. Then call again."}
    if not g:
        g = {"id": new_id("vgr")}
        s.x("INSERT INTO vault_grants(id,agent_id,cred,scope,method,url,decision_id,expires_at) VALUES(?,?,?,?,?,?,?,?)",
            (g["id"], agent_id, credential, d["scope"], method, url[:1000], did,
             now() + (GRANT_S if d["scope"] == "read" else 900)))
    s.x("UPDATE vault_grants SET uses=uses+1 WHERE id=?", (g["id"],))
    return {"verdict": "allowed", "decision_id": did, "reason": d["reason"]} | call(s, cred, method, url, body, content_type)


def answered(s: Store, question_text: str, answer: str) -> None:
    """The user answered a vault question on the board: yes -> a grant for exactly that request."""
    m = re.search(r"\[vault (vdc_[0-9a-f]+)\]", question_text or "")
    if not m:
        return
    d = s.q("SELECT * FROM vault_decisions WHERE id=?", (m.group(1),))
    if not d:
        return
    d = dict(d[0])
    yes = bool(re.match(r"\s*(y|yes|ok|okay|approve|allow|sure|go)\b", answer or "", re.I))
    s.x("UPDATE vault_decisions SET verdict=?, reason=? WHERE id=?",
        ("user_allowed" if yes else "user_denied", f"user answered: {answer[:200]}", d["id"]))
    if yes:
        s.x("INSERT INTO vault_grants(id,agent_id,cred,scope,method,url,decision_id,expires_at) VALUES(?,?,?,?,?,?,?,?)",
            (new_id("vgr"), d["agent_id"], d["cred"], d["scope"], d["method"], d["url"], d["id"],
             now() + (GRANT_S if d["scope"] == "read" else 3600)))
    s.audit(None, "vault.user_" + ("allow" if yes else "deny"), d["cred"], {"decision": d["id"]})


def decisions(s: Store, limit: int = 50) -> list[dict]:
    return [{"id": r["id"], "agent": s.label(r["agent_id"])[1], "credential": r["cred"],
             "call": f"{r['method']} {r['url'][:120]}", "verdict": r["verdict"], "reason": r["reason"],
             "justification": r["justification"][:200], "at": iso(r["ts"])}
            for r in s.q("SELECT * FROM vault_decisions ORDER BY ts DESC LIMIT ?", (limit,))]
