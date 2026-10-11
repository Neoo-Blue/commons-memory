"""Credential broker. Memory stores *references* only (vault://, env://, op://, infisical://).

Agents request a scoped, short-lived *lease*; values are resolved only at use time
(e.g. ``commons secret-exec`` injects them into a subprocess env) and are never written to
memory, embeddings, summaries or the audit log. Every request/resolve/revoke is audited.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from typing import Protocol

from .ids import iso, new_id, now
from .store import CommonsError, Store

REF_RE = re.compile(r"^(vault|vault-dyn|env|op|infisical)://[A-Za-z0-9_./#:@?=&-]+$")
DEFAULT_TTL = 300
MAX_TTL = 3600


class SecretBackend(Protocol):
    scheme: str

    def resolve(self, ref: str) -> str: ...


class EnvBackend:
    """env://NAME -> os.environ[NAME]. Optional allow-list via COMMONS_ENV_SECRETS=NAME1,NAME2."""
    scheme = "env"

    def resolve(self, ref: str) -> str:
        name = ref.split("://", 1)[1]
        allowed = os.environ.get("COMMONS_ENV_SECRETS")
        if allowed is not None and name not in {a.strip() for a in allowed.split(",")}:
            raise CommonsError(f"env var {name} not in COMMONS_ENV_SECRETS allow-list")
        if name not in os.environ:
            raise CommonsError(f"env var {name} not set")
        return os.environ[name]


class VaultBackend:
    """HashiCorp Vault / OpenBao.

    vault://<mount>/<path>#<key>       KV v2 static secret (default)
    vault://<mount>/<path>?kv=1#<key>  KV v1
    vault-dyn://<mount>/<path>#<key>   dynamic secret (database/creds/<role>, aws/creds/<role>, ...): every broker lease
                                       gets its own backend credential + Vault lease, revoked when the broker lease is
                                       revoked or expires (reaped by the background loop).
    Auth: VAULT_ADDR/VAULT_TOKEN (or BAO_*), or AppRole via VAULT_ROLE_ID/VAULT_SECRET_ID. VAULT_NAMESPACE supported.
    """
    scheme = "vault"
    dynamic_scheme = "vault-dyn"

    def __init__(self, addr: str | None = None, token: str | None = None):
        self._addr, self._token = addr, token
        self._dyn: dict[str, dict] = {}  # broker lease id -> {data, lease_id} (process memory only)

    @property
    def addr(self) -> str:
        return (self._addr or os.environ.get("VAULT_ADDR") or os.environ.get("BAO_ADDR") or "").rstrip("/")

    @property
    def token(self) -> str | None:
        return self._token or os.environ.get("VAULT_TOKEN") or os.environ.get("BAO_TOKEN")

    @token.setter
    def token(self, v: str | None) -> None:
        self._token = v

    def _req(self, method: str, path: str, body: dict | None = None) -> dict:
        if not self.addr:
            raise CommonsError("VAULT_ADDR (or BAO_ADDR) not configured")
        if not self.token and os.environ.get("VAULT_ROLE_ID"):
            r = self._raw("POST", "/v1/auth/approle/login", {"role_id": os.environ["VAULT_ROLE_ID"],
                                                             "secret_id": os.environ.get("VAULT_SECRET_ID", "")}, auth=False)
            self.token = r["auth"]["client_token"]
        if not self.token:
            raise CommonsError("VAULT_TOKEN (or AppRole VAULT_ROLE_ID/VAULT_SECRET_ID) not configured")
        return self._raw(method, path, body)

    def _raw(self, method: str, path: str, body: dict | None = None, auth: bool = True) -> dict:
        h = {"Content-Type": "application/json"}
        if auth and self.token:
            h["X-Vault-Token"] = self.token
        if os.environ.get("VAULT_NAMESPACE"):
            h["X-Vault-Namespace"] = os.environ["VAULT_NAMESPACE"]
        req = urllib.request.Request(self.addr + path, data=json.dumps(body).encode() if body is not None else None,
                                     method=method, headers=h)
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - operator-configured URL
            raw = r.read()
        return json.loads(raw) if raw else {}

    @staticmethod
    def _parse(ref: str) -> tuple[str, str, str, dict]:
        body = ref.split("://", 1)[1]
        body, _, key = body.partition("#")
        path, _, qs = body.partition("?")
        opts = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
        mount, _, rest = path.partition("/")
        return mount, rest, key, opts

    def resolve(self, ref: str) -> str:
        mount, rest, key, opts = self._parse(ref)
        if opts.get("kv") == "1":
            data = self._req("GET", f"/v1/{mount}/{rest}")["data"]
        else:
            data = self._req("GET", f"/v1/{mount}/data/{rest}")["data"]["data"]
        return data[key] if key else json.dumps(data)

    # dynamic secrets ---------------------------------------------------------
    def issue(self, ref: str, broker_lease: str) -> dict:
        mount, rest, _key, _ = self._parse(ref)
        r = self._req("GET", f"/v1/{mount}/{rest}")
        self._dyn[broker_lease] = {"data": r.get("data") or {}, "lease_id": r.get("lease_id"),
                                   "duration": r.get("lease_duration")}
        return {"backend_lease": r.get("lease_id"), "duration": r.get("lease_duration")}

    def resolve_lease(self, ref: str, broker_lease: str) -> str:
        if broker_lease not in self._dyn:
            raise CommonsError("dynamic credential not available in this process (request a new lease)")
        data = self._dyn[broker_lease]["data"]
        key = self._parse(ref)[2]
        return data[key] if key else json.dumps(data)

    def revoke_backend(self, backend_lease: str | None, broker_lease: str | None = None) -> None:
        self._dyn.pop(broker_lease or "", None)
        if backend_lease:
            self._req("PUT", "/v1/sys/leases/revoke", {"lease_id": backend_lease})


class OnePasswordBackend:
    """op://vault/item/field via the 1Password CLI (`op read`)."""
    scheme = "op"

    def resolve(self, ref: str) -> str:
        if not shutil.which("op"):
            raise CommonsError("1Password CLI 'op' not installed")
        return subprocess.run(["op", "read", ref], check=True, capture_output=True, text=True, timeout=20).stdout.rstrip("\n")


class InfisicalBackend:
    """infisical://<projectId>/<environment>/<optional/folder/path/>NAME via the Infisical REST API.

    Auth: Universal Auth machine identity (INFISICAL_CLIENT_ID + INFISICAL_CLIENT_SECRET -> POST
    /api/v1/auth/universal-auth/login) or a ready token (INFISICAL_TOKEN). Host: INFISICAL_HOST
    (default https://app.infisical.com; use https://eu.infisical.com or your self-hosted URL).
    Reads with GET /api/v4/secrets/{name}?projectId=&environment=&secretPath= .
    """
    scheme = "infisical"

    def __init__(self, host: str | None = None):
        self.host = (host or os.environ.get("INFISICAL_HOST") or "https://app.infisical.com").rstrip("/")
        self._tok: tuple[str, float] | None = None

    def _token(self) -> str:
        if os.environ.get("INFISICAL_TOKEN"):
            return os.environ["INFISICAL_TOKEN"]
        if self._tok and self._tok[1] > now() + 30:
            return self._tok[0]
        cid, sec = os.environ.get("INFISICAL_CLIENT_ID"), os.environ.get("INFISICAL_CLIENT_SECRET")
        if not cid or not sec:
            raise CommonsError("set INFISICAL_TOKEN or INFISICAL_CLIENT_ID/INFISICAL_CLIENT_SECRET (universal auth)")
        req = urllib.request.Request(f"{self.host}/api/v1/auth/universal-auth/login", method="POST",
                                     data=json.dumps({"clientId": cid, "clientSecret": sec}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
            d = json.loads(r.read())
        self._tok = (d["accessToken"], now() + float(d.get("expiresIn") or 600))
        return self._tok[0]

    def resolve(self, ref: str) -> str:
        import urllib.parse
        parts = ref.split("://", 1)[1].split("/")
        if len(parts) < 3:
            raise CommonsError("infisical ref must be infisical://<projectId>/<env>/[path/]NAME")
        project, env, name, folder = parts[0], parts[1], parts[-1], "/" + "/".join(parts[2:-1])
        qs = urllib.parse.urlencode({"projectId": project, "environment": env, "secretPath": folder or "/"})
        req = urllib.request.Request(f"{self.host}/api/v4/secrets/{urllib.parse.quote(name)}?{qs}",
                                     headers={"Authorization": f"Bearer {self._token()}"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
                return json.loads(r.read())["secret"]["secretValue"]
        except urllib.error.HTTPError as e:
            raise CommonsError(f"infisical: HTTP {e.code} for {name}") from None


_VAULT = VaultBackend()  # process-wide: dynamic credentials live only in this process's memory


class Broker:
    def __init__(self, store: Store, backends: list[SecretBackend] | None = None):
        self.store = store
        self.backends = {b.scheme: b for b in (backends or [EnvBackend(), _VAULT, OnePasswordBackend(),
                                                             InfisicalBackend()])}
        for b in list(self.backends.values()):
            if getattr(b, "dynamic_scheme", None):
                self.backends[b.dynamic_scheme] = b

    def register(self, ref: str, description: str = "", project: str | None = None,
                 allowed_agents: list[str] | None = None, max_ttl: int = MAX_TTL, agent_id: str | None = None) -> dict:
        if not REF_RE.match(ref):
            raise CommonsError("ref must look like vault://mount/path#key, env://NAME, op://vault/item/field")
        self.store.x("INSERT OR REPLACE INTO secret_refs VALUES(?,?,?,?,?,?,?)",
                     (ref, description, self.store.project_id(project), json.dumps(allowed_agents or []),
                      min(max_ttl, MAX_TTL), agent_id, now()))
        self.store.audit(agent_id, "secret.register", ref)
        from .graphlinks import link_secret_ref
        link_secret_ref(self.store, ref)
        return {"ref": ref, "registered": True}

    def list_refs(self, project: str | None = None) -> list[dict]:
        rows = self.store.q("SELECT * FROM secret_refs")
        pid = self.store.project_id(project) if project else None
        return [{"ref": r["ref"], "description": r["description"], "allowed_agents": json.loads(r["allowed_agents"]),
                 "max_ttl": r["max_ttl"]} for r in rows if not pid or r["project_id"] in (None, pid)]

    def request(self, ref: str, agent_id: str, scope: str = "read", purpose: str = "", ttl: int = DEFAULT_TTL) -> dict:
        r = self.store.q("SELECT * FROM secret_refs WHERE ref=?", (ref,))
        if not r:
            self.store.audit(agent_id, "secret.request.denied", ref, {"reason": "unregistered"})
            raise CommonsError("secret ref not registered (use secret_register first)")
        allowed = json.loads(r[0]["allowed_agents"] or "[]")
        agent = self.store.get_agent(agent_id) or {}
        if allowed and agent_id not in allowed and agent.get("name") not in allowed:
            self.store.audit(agent_id, "secret.request.denied", ref, {"reason": "agent not allowed"})
            raise CommonsError("agent not allowed for this secret")
        ttl = max(1, min(int(ttl), r[0]["max_ttl"] or MAX_TTL))
        lid = new_id("lea")
        exp = now() + ttl
        self.store.x("INSERT INTO leases(id,ref,agent_id,scope,purpose,expires_at,revoked,created_at) VALUES(?,?,?,?,?,?,?,?)", (lid, ref, agent_id, scope, purpose, exp, 0, now()))
        scheme = ref.split("://", 1)[0]
        b = self.backends.get(scheme)
        if scheme == "vault-dyn" and b is not None:  # fresh backend credential per lease
            info = b.issue(ref, lid)
            self.store.x("UPDATE leases SET backend_lease=? WHERE id=?", (info.get("backend_lease"), lid))
        from .graphlinks import link_lease
        link_lease(self.store, lid)
        self.store.audit(agent_id, "secret.lease", ref, {"lease": lid, "scope": scope, "ttl": ttl, "purpose": purpose})
        return {"lease_id": lid, "ref": ref, "scope": scope, "expires_at": iso(exp),
                "usage": f"commons secret-exec {lid} --as VARNAME -- <command>  (value injected into env, never shown)"}

    def _lease(self, lease_id: str) -> dict:
        r = self.store.q("SELECT * FROM leases WHERE id=?", (lease_id,))
        if not r:
            raise CommonsError("unknown lease")
        return dict(r[0])

    def resolve(self, lease_id: str, agent_id: str | None = None) -> str:
        lease = self._lease(lease_id)
        if lease["revoked"]:
            self.store.audit(agent_id, "secret.resolve.denied", lease["ref"], {"lease": lease_id, "reason": "revoked"})
            raise CommonsError("lease revoked")
        if now() > lease["expires_at"]:
            self.store.audit(agent_id, "secret.resolve.denied", lease["ref"], {"lease": lease_id, "reason": "expired"})
            raise CommonsError("lease expired")
        if agent_id and agent_id != lease["agent_id"]:
            self.store.audit(agent_id, "secret.resolve.denied", lease["ref"], {"lease": lease_id, "reason": "wrong agent"})
            raise CommonsError("lease bound to a different agent")
        scheme = lease["ref"].split("://", 1)[0]
        backend = self.backends.get(scheme)
        if not backend:
            raise CommonsError(f"no backend for {scheme}://")
        value = backend.resolve_lease(lease["ref"], lease_id) if scheme == "vault-dyn" else backend.resolve(lease["ref"])
        self.store.audit(lease["agent_id"], "secret.resolve", lease["ref"], {"lease": lease_id})
        return value

    def resolve_internal(self, ref: str, purpose: str) -> str:
        """Server-side use of a registered ref (e.g. provider API keys). The value never leaves the process,
        is never returned to a model, and every use is audited."""
        if not self.store.q("SELECT 1 FROM secret_refs WHERE ref=?", (ref,)):
            raise CommonsError(f"secret ref {ref} not registered")
        scheme = ref.split("://", 1)[0]
        backend = self.backends.get(scheme)
        if not backend:
            raise CommonsError(f"no backend for {scheme}://")
        value = backend.resolve(ref)
        self.store.audit(None, "secret.resolve.internal", ref, {"purpose": purpose})
        return value

    def _revoke_backend(self, rows) -> None:
        for r in rows:
            if (r["ref"] or "").startswith("vault-dyn://") and self.backends.get("vault-dyn"):
                try:
                    self.backends["vault-dyn"].revoke_backend(r["backend_lease"], r["id"])
                    self.store.audit(None, "secret.backend_revoke", r["ref"], {"lease": r["id"]})
                except Exception as e:  # noqa: BLE001
                    self.store.audit(None, "secret.backend_revoke.error", r["ref"], {"lease": r["id"], "error": str(e)[:200]})

    def reap_expired(self) -> int:
        """Revoke backend credentials of expired dynamic leases (called by the background loop)."""
        rows = self.store.q("SELECT * FROM leases WHERE revoked=0 AND expires_at<?", (now(),))
        self._revoke_backend(rows)
        for r in rows:
            self.store.x("UPDATE leases SET revoked=1 WHERE id=?", (r["id"],))
        return len(rows)

    def revoke(self, lease_id: str | None = None, agent_id: str | None = None, ref: str | None = None) -> dict:
        if lease_id:
            self._revoke_backend(self.store.q("SELECT * FROM leases WHERE id=? AND revoked=0", (lease_id,)))
            self.store.x("UPDATE leases SET revoked=1 WHERE id=?", (lease_id,))
            n = 1
        else:
            where, args = ["revoked=0"], []
            if agent_id:
                where.append("agent_id=?"); args.append(agent_id)
            if ref:
                where.append("ref=?"); args.append(ref)
            rows = self.store.q(f"SELECT * FROM leases WHERE {' AND '.join(where)}", args)
            self._revoke_backend(rows)
            n = len(rows)
            self.store.x(f"UPDATE leases SET revoked=1 WHERE {' AND '.join(where)}", args)
        self.store.audit(agent_id, "secret.revoke", lease_id or ref or "*", {"count": n})
        return {"revoked": n}

    def audit_log(self, limit: int = 50, ref: str | None = None) -> list[dict]:
        sql = "SELECT * FROM audit WHERE action LIKE 'secret.%'" + (" AND target=?" if ref else "") + " ORDER BY id DESC LIMIT ?"
        return [dict(r) | {"ts": iso(r["ts"])} for r in self.store.q(sql, ([ref] if ref else []) + [limit])]
