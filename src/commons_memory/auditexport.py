"""Signed audit export: the audit log as tamper-evident JSONL anyone can verify without a secret.

Every row carries the hash of the row before it, so removing, reordering or editing any row breaks the chain. The last
line signs the chain's final hash with the hub's Ed25519 key (created once at COMMONS_HOME/audit_signing.key, 0600, or
COMMONS_AUDIT_KEY_FILE). The export embeds the public key; `commons audit-verify FILE --key FINGERPRINT` pins the key
you expect, so a re-signed forgery fails too.

The live audit table is a hash chain too: each row stores prev and hash when it is inserted, and `commons audit-chain`
rechecks it. Someone with write access to the database can rewrite the whole chain or cut the newest rows; a signed
export pins the head at that moment, so compare against your last export.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

from .ids import iso, now
from .store import CommonsError, Store, _parse_time


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def chain_hash(prev, ts, agent_id, action, target, detail) -> str:
    """sha256 of one live audit row. detail is the text stored in the table, not a second encoding."""
    body = {"prev": prev, "ts": ts, "agent_id": agent_id, "action": action, "target": target, "detail": detail}
    return hashlib.sha256(_canon(body)).hexdigest()


def _key_path() -> Path:
    return Path(os.environ.get("COMMONS_AUDIT_KEY_FILE")
                or Path(os.environ.get("COMMONS_HOME", Path.home() / ".commons")) / "audit_signing.key")


def _private():
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as e:  # pragma: no cover - the hub image has it (vault)
        raise CommonsError("signed audit exports need the cryptography package: pip install 'commons-memory[vault]'") from e
    p = _key_path()
    if p.exists():
        return serialization.load_pem_private_key(p.read_bytes(), password=None)
    k = Ed25519PrivateKey.generate()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()))
    return k


def _pub_raw(k) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def fingerprint(pub_raw: bytes) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(pub_raw).digest()).decode().rstrip("=")


def public_key() -> dict:
    raw = _pub_raw(_private())
    return {"public_key": base64.b64encode(raw).decode(), "fingerprint": fingerprint(raw)}


def export(s: Store, since: str | None = None, until: str | None = None) -> str:
    """The audit rows in [since, until] as signed JSONL (header, rows, signature)."""
    k = _private()
    raw = _pub_raw(k)
    where, args = [], []
    if since:
        where.append("ts>=?"), args.append(_parse_time(since))
    if until:
        where.append("ts<=?"), args.append(_parse_time(until))
    rows = s.q("SELECT * FROM audit" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id", args)
    from . import __version__
    header = {"type": "commons-audit-export", "commons_version": __version__, "exported_at": iso(now()),
              "since": since, "until": until, "count": len(rows), "public_key": base64.b64encode(raw).decode(),
              "fingerprint": fingerprint(raw)}
    prev = hashlib.sha256(_canon(header)).hexdigest()
    lines = [json.dumps(header)]
    for r in rows:
        rec = {"id": r["id"], "ts": iso(r["ts"]), "agent": s.label(r["agent_id"])[1] if r["agent_id"] else None,
               "action": r["action"], "target": r["target"], "detail": r["detail"], "prev": prev}
        prev = hashlib.sha256(_canon(rec)).hexdigest()
        lines.append(json.dumps(rec | {"hash": prev}))
    sig = k.sign(bytes.fromhex(prev))
    lines.append(json.dumps({"type": "signature", "last_hash": prev, "count": len(rows),
                             "signature": base64.b64encode(sig).decode()}))
    return "\n".join(lines) + "\n"


def verify(text: str, pin: str | None = None) -> dict:
    """Check the chain and the signature (and, with pin, that the signing key's fingerprint is the expected one)."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    lines = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
    if len(lines) < 2 or lines[0].get("type") != "commons-audit-export" or lines[-1].get("type") != "signature":
        return {"ok": False, "error": "not a Commons signed audit export"}
    header, sig, rows = lines[0], lines[-1], lines[1:-1]
    raw = base64.b64decode(header["public_key"])
    if pin and fingerprint(raw) != pin:
        return {"ok": False, "error": f"signed by {fingerprint(raw)}, not the pinned key {pin}"}
    prev = hashlib.sha256(_canon(header)).hexdigest()
    for n, r in enumerate(rows, 1):
        h = r.pop("hash", None)
        if r.get("prev") != prev or hashlib.sha256(_canon(r)).hexdigest() != h:
            return {"ok": False, "error": f"chain broken at row {n} (audit id {r.get('id')}): edited, removed or reordered"}
        prev = h
    if len(rows) != header["count"] or sig["count"] != header["count"] or sig["last_hash"] != prev:
        return {"ok": False, "error": "row count or final hash doesn't match the signature"}
    try:
        Ed25519PublicKey.from_public_bytes(raw).verify(base64.b64decode(sig["signature"]), bytes.fromhex(prev))
    except InvalidSignature:
        return {"ok": False, "error": "signature invalid"}
    return {"ok": True, "rows": len(rows), "fingerprint": fingerprint(raw), "pinned": bool(pin),
            "from": rows[0]["ts"] if rows else None, "to": rows[-1]["ts"] if rows else None}


def verify_live(store: Store) -> dict:
    """Recompute the live chain in id order, in batches so a long log is not one giant query. Appends during the walk
    are harmless; a prune that moves the anchor mid-walk would look like a gap, so the walk starts over (3 tries)."""
    for _ in range(3):
        anchor = store.q("SELECT value FROM meta WHERE key='audit_anchor'")
        r = _walk(store)
        if r["ok"] or store.q("SELECT value FROM meta WHERE key='audit_anchor'") == anchor:
            return r
    return r


def _walk(store: Store) -> dict:
    anc = store.q("SELECT value FROM meta WHERE key='audit_anchor'")
    prev = (anc[0]["value"] or "") if anc else ""
    head = ""
    n = 0
    last_id = 0
    while True:
        rows = store.q("SELECT id, ts, agent_id, action, target, detail, prev, hash FROM audit "
                       "WHERE id>? ORDER BY id LIMIT 2000", (last_id,))
        if not rows:
            break
        for r in rows:
            n += 1
            last_id = r["id"]
            head = r["hash"] or ""
            if r["prev"] != prev:
                return {"ok": False, "rows": n, "head": head, "bad_id": r["id"],
                        "reason": "prev does not match the previous hash"}
            expect = chain_hash(prev, r["ts"], r["agent_id"], r["action"], r["target"], r["detail"])
            if r["hash"] != expect:
                return {"ok": False, "rows": n, "head": head, "bad_id": r["id"],
                        "reason": "hash does not match row contents"}
            prev = r["hash"]
    return {"ok": True, "rows": n, "head": head, "bad_id": None, "reason": ""}
