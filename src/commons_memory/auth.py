"""HTTP authentication (agent keys or OAuth 2.1 bearer tokens), per-project ACLs and rate limiting.

* HTTP transport: every request (reads included) needs ``Authorization: Bearer <token>`` unless
  ``COMMONS_ALLOW_ANON_READS=1``. A token is either a Commons agent key (``cmk_...``) or an OAuth 2.1 access token
  issued by your authorization server and checked by RFC 7662 introspection (``COMMONS_OAUTH_ISSUER``,
  ``COMMONS_OAUTH_INTROSPECT_URL``, client credentials as secret refs). Commons acts as an OAuth *resource server*:
  401 responses carry ``WWW-Authenticate`` with RFC 9728 protected-resource metadata, as the MCP auth spec requires.
* The authenticated principal is put in a contextvar so tools see who is calling even when no api_key argument is
  passed; per-project ACLs are then enforced on reads and writes.
* Per-principal token-bucket rate limiting (``COMMONS_RATE_PER_MIN``, default 600).
"""
from __future__ import annotations

import base64
import contextvars
import hashlib
import json
import os
import threading
import time
import urllib.parse
import urllib.request

from .ids import now
from .store import CommonsError, Store

PRINCIPAL: contextvars.ContextVar[str | None] = contextvars.ContextVar("commons_principal", default=None)
ANON = "anonymous"
ROLES = {"read": 1, "write": 2, "admin": 3}

ACL_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_acl(project_id TEXT, agent_id TEXT, role TEXT, created_at REAL,
  PRIMARY KEY(project_id, agent_id));
"""


# ---------------------------------------------------------------------------- ACLs
class ACL:
    """A project with no ACL rows is open (back-compatible). Once any row exists, only listed agents (role
    read < write < admin) may access it. principal None = local owner (stdio/CLI) and bypasses ACLs."""

    def __init__(self, store: Store):
        self.s = store

    def grant(self, project: str, agent: str, role: str = "read") -> dict:
        if role not in ROLES:
            raise CommonsError("role must be read|write|admin")
        pid = self.s.project_id(project)
        a = self.s.get_agent(agent)
        aid = a["id"] if a else (agent if agent == ANON else self.s.ensure_agent(agent))
        self.s.x("INSERT OR REPLACE INTO project_acl VALUES(?,?,?,?)", (pid, aid, role, now()))
        self.s.audit(PRINCIPAL.get(), "acl.grant", pid, {"agent": aid, "role": role})
        return {"project": project, "agent": aid, "role": role}

    def revoke(self, project: str, agent: str) -> dict:
        pid = self.s.project_id(project)
        a = self.s.get_agent(agent)
        self.s.x("DELETE FROM project_acl WHERE project_id=? AND agent_id=?", (pid, a["id"] if a else agent))
        return {"revoked": True}

    def list(self, project: str | None = None) -> list[dict]:
        rows = self.s.q("SELECT * FROM project_acl" + (" WHERE project_id=?" if project else ""),
                        (self.s.project_id(project),) if project else ())
        return [dict(r) | {"project": self.s.project_name(r["project_id"]), "agent": self.s.label(r["agent_id"])[1]}
                for r in rows]

    def denied_projects(self, principal: str | None, need: str = "read") -> list[str]:
        if principal is None:
            return []
        rows = self.s.q("SELECT project_id, agent_id, role FROM project_acl")
        restricted: dict[str, dict[str, str]] = {}
        for r in rows:
            restricted.setdefault(r["project_id"], {})[r["agent_id"]] = r["role"]
        return [p for p, m in restricted.items() if ROLES.get(m.get(principal, ""), 0) < ROLES[need]]

    def check(self, principal: str | None, project_id: str | None, need: str = "read") -> None:
        if project_id and project_id in self.denied_projects(principal, need):
            raise CommonsError(f"access denied: {need} on project {self.s.project_name(project_id) or project_id}")


# ---------------------------------------------------------------------------- rate limiting
class RateLimiter:
    def __init__(self, per_min: int | None = None):
        self.rate = per_min if per_min is not None else int(os.environ.get("COMMONS_RATE_PER_MIN", "600"))
        self.b: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self.rate <= 0:
            return True
        with self.lock:
            tokens, ts = self.b.get(key, (float(self.rate), time.monotonic()))
            t = time.monotonic()
            tokens = min(float(self.rate), tokens + (t - ts) * self.rate / 60.0)
            if tokens < 1:
                self.b[key] = (tokens, t)
                return False
            self.b[key] = (tokens - 1, t)
            return True


# ---------------------------------------------------------------------------- OAuth 2.1 (resource server)
class OAuthVerifier:
    """RFC 7662 token introspection, with a short cache. Configure via env or constructor."""

    def __init__(self, store: Store, issuer: str | None = None, introspect_url: str | None = None,
                 client_id_ref: str | None = None, client_secret_ref: str | None = None, audience: str | None = None):
        self.s = store
        self.issuer = issuer or os.environ.get("COMMONS_OAUTH_ISSUER")
        self.url = introspect_url or os.environ.get("COMMONS_OAUTH_INTROSPECT_URL")
        self.cid_ref = client_id_ref or os.environ.get("COMMONS_OAUTH_CLIENT_ID_REF")
        self.csec_ref = client_secret_ref or os.environ.get("COMMONS_OAUTH_CLIENT_SECRET_REF")
        self.audience = audience or os.environ.get("COMMONS_OAUTH_AUDIENCE")
        self.cache: dict[str, tuple[float, dict]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def _cred(self, ref: str | None) -> str:
        if not ref:
            return ""
        from .secrets import Broker
        return Broker(self.s).resolve_internal(ref, "oauth introspection")

    def verify(self, token: str) -> dict | None:
        if not self.enabled:
            return None
        h = hashlib.sha256(token.encode()).hexdigest()
        c = self.cache.get(h)
        if c and c[0] > time.time():
            return c[1]
        body = urllib.parse.urlencode({"token": token, "token_type_hint": "access_token"}).encode()
        hdr = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        cid, sec = self._cred(self.cid_ref), self._cred(self.csec_ref)
        if cid:
            hdr["Authorization"] = "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode()
        try:
            with urllib.request.urlopen(urllib.request.Request(self.url, data=body, headers=hdr), timeout=10) as r:  # noqa: S310
                info = json.loads(r.read())
        except Exception:  # noqa: BLE001
            return None
        ok = info.get("active") and (not self.issuer or info.get("iss") in (None, self.issuer)) and (
            not self.audience or self.audience in (info.get("aud") if isinstance(info.get("aud"), list) else [info.get("aud")]))
        res = info if ok else None
        exp = min(float(info.get("exp") or time.time() + 60), time.time() + 300)
        self.cache[h] = (exp, res)  # type: ignore[assignment]
        return res

    def principal(self, info: dict) -> str:
        """Map an OAuth subject to a Commons agent (created on first use, named oauth:<sub or client_id>)."""
        sub = info.get("sub") or info.get("client_id") or info.get("username") or "unknown"
        return self.s.ensure_agent(f"oauth:{sub}")


# ---------------------------------------------------------------------------- ASGI middleware
class AuthMiddleware:
    """Wraps the MCP streamable-HTTP ASGI app."""

    def __init__(self, app, store: Store, resource_url: str = "", allow_anon_reads: bool | None = None,
                 limiter: RateLimiter | None = None, oauth: OAuthVerifier | None = None):
        self.app, self.s = app, store
        self.resource_url = resource_url
        self.anon = allow_anon_reads if allow_anon_reads is not None else os.environ.get("COMMONS_ALLOW_ANON_READS") == "1"
        self.limiter = limiter or RateLimiter()
        self.oauth = oauth or OAuthVerifier(store)

    async def _send_json(self, send, status: int, obj: dict, extra: list | None = None):
        body = json.dumps(obj).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
                    + (extra or [])})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        if path == "/.well-known/oauth-protected-resource":
            meta = {"resource": self.resource_url or "commons", "bearer_methods_supported": ["header"],
                    "authorization_servers": [self.oauth.issuer] if self.oauth.issuer else [],
                    "resource_documentation": "https://github.com/Neoo-Blue/commons-memory#security-model"}
            return await self._send_json(send, 200, meta)
        if path.startswith("/.well-known/agent"):  # A2A agent card is public discovery metadata
            return await self.app(scope, receive, send)
        hdrs = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        tok = hdrs.get("authorization", "")
        tok = tok[7:].strip() if tok.lower().startswith("bearer ") else (hdrs.get("x-api-key") or "")
        principal = None
        if tok:
            a = self.s.authenticate(tok)
            if a:
                principal = a["id"]
            else:
                info = self.oauth.verify(tok)
                if info:
                    principal = self.oauth.principal(info)
        if principal is None and not self.anon:
            www = f'Bearer resource_metadata="{self.resource_url.rstrip("/")}/.well-known/oauth-protected-resource"' \
                if self.resource_url else 'Bearer realm="commons"'
            self.s.audit(None, "http.unauthorized", path, {"client": (scope.get("client") or ["?"])[0]})
            return await self._send_json(send, 401, {"error": "unauthorized",
                                                     "hint": "send Authorization: Bearer <agent key or OAuth token>"},
                                         [(b"www-authenticate", www.encode())])
        key = principal or f"ip:{(scope.get('client') or ['?'])[0]}"
        if not self.limiter.allow(key):
            return await self._send_json(send, 429, {"error": "rate limited"}, [(b"retry-after", b"5")])
        tok_ctx = PRINCIPAL.set(principal or ANON)
        try:
            return await self.app(scope, receive, send)
        finally:
            PRINCIPAL.reset(tok_ctx)
