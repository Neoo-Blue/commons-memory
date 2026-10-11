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
import urllib.request
from typing import Protocol

from .ids import iso, new_id, now
from .store import CommonsError, Store

REF_RE = re.compile(r"^(vault|env|op|infisical)://[A-Za-z0-9_./#:@-]+$")
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
    """vault://<mount>/<path>#<key> via HashiCorp Vault / OpenBao KV v2 HTTP API.

    Uses VAULT_ADDR (or BAO_ADDR) and VAULT_TOKEN (or BAO_TOKEN). For dynamic secrets engines
    (database/, aws/) the response's lease is honoured by the broker TTL.
    """
    scheme = "vault"

    def __init__(self, addr: str | None = None, token: str | None = None):
        self.addr = (addr or os.environ.get("VAULT_ADDR") or os.environ.get("BAO_ADDR") or "").rstrip("/")
        self.token = token or os.environ.get("VAULT_TOKEN") or os.environ.get("BAO_TOKEN")

    def resolve(self, ref: str) -> str:
        if not self.addr or not self.token:
            raise CommonsError("VAULT_ADDR/VAULT_TOKEN (or BAO_*) not configured")
        path, _, key = ref.split("://", 1)[1].partition("#")
        mount, _, rest = path.partition("/")
        req = urllib.request.Request(f"{self.addr}/v1/{mount}/data/{rest}", headers={"X-Vault-Token": self.token})
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - operator-configured URL
            data = json.loads(r.read())["data"]["data"]
        return data[key] if key else json.dumps(data)


class OnePasswordBackend:
    """op://vault/item/field via the 1Password CLI (`op read`)."""
    scheme = "op"

    def resolve(self, ref: str) -> str:
        if not shutil.which("op"):
            raise CommonsError("1Password CLI 'op' not installed")
        return subprocess.run(["op", "read", ref], check=True, capture_output=True, text=True, timeout=20).stdout.rstrip("\n")


class InfisicalBackend:
    """infisical://<project>/<env>/<NAME> — adapter interface (v2). Plug in the Infisical SDK here."""
    scheme = "infisical"

    def resolve(self, ref: str) -> str:
        raise CommonsError("Infisical adapter not implemented in v1; register a custom backend")


class Broker:
    def __init__(self, store: Store, backends: list[SecretBackend] | None = None):
        self.store = store
        self.backends = {b.scheme: b for b in (backends or [EnvBackend(), VaultBackend(), OnePasswordBackend(),
                                                             InfisicalBackend()])}

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
        self.store.x("INSERT INTO leases VALUES(?,?,?,?,?,?,?,?)", (lid, ref, agent_id, scope, purpose, exp, 0, now()))
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
        value = backend.resolve(lease["ref"])
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

    def revoke(self, lease_id: str | None = None, agent_id: str | None = None, ref: str | None = None) -> dict:
        if lease_id:
            self.store.x("UPDATE leases SET revoked=1 WHERE id=?", (lease_id,))
            n = 1
        else:
            where, args = ["revoked=0"], []
            if agent_id:
                where.append("agent_id=?"); args.append(agent_id)
            if ref:
                where.append("ref=?"); args.append(ref)
            n = len(self.store.q(f"SELECT id FROM leases WHERE {' AND '.join(where)}", args))
            self.store.x(f"UPDATE leases SET revoked=1 WHERE {' AND '.join(where)}", args)
        self.store.audit(agent_id, "secret.revoke", lease_id or ref or "*", {"count": n})
        return {"revoked": n}

    def audit_log(self, limit: int = 50, ref: str | None = None) -> list[dict]:
        sql = "SELECT * FROM audit WHERE action LIKE 'secret.%'" + (" AND target=?" if ref else "") + " ORDER BY id DESC LIMIT ?"
        return [dict(r) | {"ts": iso(r["ts"])} for r in self.store.q(sql, ([ref] if ref else []) + [limit])]
