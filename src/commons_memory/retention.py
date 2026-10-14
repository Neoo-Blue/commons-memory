"""Retention, pruning, backups and full-fidelity dump/restore.

prune(): usage rows older than N days are rolled up into `usage_daily` (totals stay correct) and their graph nodes
are removed; old context packs (except the latest per name and packs used by running jobs) are deleted and
unreferenced blobs are garbage-collected; old audit rows, MCP call logs, caches and loads are trimmed.
backup(): consistent snapshot (SQLite online backup, or a JSONL dump for Postgres), optional encryption
(AES-GCM via `cryptography`, key from COMMONS_BACKUP_KEY), rotation.
dump()/load(): every table as JSONL, portable between SQLite and Postgres.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import time
from pathlib import Path

from .ids import now
from .store import Store

DEFAULTS = {"usage_days": 90, "context_days": 30, "audit_days": 365, "calls_days": 30, "cache_days": 7}
SKIP_TABLES = {"items_fts", "sqlite_sequence"}


def _env_days(k: str) -> float:
    return float(os.environ.get(f"COMMONS_RETAIN_{k.upper()}", DEFAULTS[k]))


def _trim_audit(s: Store, cutoff: float) -> int:
    """Delete only the oldest unbroken run of aged audit rows and keep the last deleted hash as the chain's anchor, so
    the rows that stay are never rewritten (a row with a skewed clock just waits for a later prune)."""
    with s.tx():
        s._lock_audit_chain()
        lo = s.q("SELECT MIN(id) m FROM audit WHERE ts IS NULL OR ts>=?", (cutoff,))[0]["m"]
        cut, args = ("id<?", (lo,)) if lo is not None else ("1=1", ())
        last = s.q(f"SELECT hash FROM audit WHERE {cut} ORDER BY id DESC LIMIT 1", args)
        if not last:
            return 0
        n = s.q(f"SELECT COUNT(*) n FROM audit WHERE {cut}", args)[0]["n"]
        s.x("INSERT OR REPLACE INTO meta VALUES('audit_anchor', ?)", (last[0]["hash"] or "",))
        s.x(f"DELETE FROM audit WHERE {cut}", args)
        return n


def prune(s: Store, usage_days: float | None = None, context_days: float | None = None,
          audit_days: float | None = None, dry_run: bool = False) -> dict:
    out: dict = {}
    t = now()
    # ---- usage rollup
    ud = usage_days if usage_days is not None else _env_days("usage_days")
    cut = t - ud * 86400
    rows = s.q("SELECT * FROM usage WHERE ts<?", (cut,))
    out["usage_rolled_up"] = len(rows)
    if rows and not dry_run:
        agg: dict[tuple, list] = {}
        for r in rows:
            day = time.strftime("%Y-%m-%d", time.gmtime(r["ts"]))
            k = (day, r["agent_id"] or "", r["project_id"] or "", r["model"] or "")
            a = agg.setdefault(k, [0, 0, 0.0, 0.0, 0, 0])
            a[0] += r["tokens_in"] or 0; a[1] += r["tokens_out"] or 0; a[2] += r["cost"] or 0
            a[3] += r["seconds"] or 0; a[4] += 1; a[5] += (r["cached_tokens"] or 0) if "cached_tokens" in r.keys() else 0
        with s.lock:
            for (day, ag, pr, mo), a in agg.items():
                ts = time.mktime(time.strptime(day, "%Y-%m-%d")) - time.timezone
                ex = s.q("SELECT * FROM usage_daily WHERE day=? AND agent_id=? AND project_id=? AND model=?", (day, ag, pr, mo))
                if ex:
                    s.x("UPDATE usage_daily SET tokens_in=tokens_in+?, tokens_out=tokens_out+?, cost=cost+?, seconds=seconds+?,"
                        " calls=calls+?, cached_tokens=cached_tokens+? WHERE day=? AND agent_id=? AND project_id=? AND model=?",
                        (*a, day, ag, pr, mo))
                else:
                    s.x("INSERT INTO usage_daily VALUES(?,?,?,?,?,?,?,?,?,?,?)", (day, ts, ag, pr, mo, *a))
            ids = [r["id"] for r in rows]
            for i in range(0, len(ids), 500):
                ch = ids[i:i + 500]
                nodes = [f"usg_{x}" for x in ch]
                ph = ",".join("?" * len(ch))
                s.x(f"DELETE FROM relations WHERE src IN ({ph}) OR dst IN ({ph})", nodes + nodes)
                s.x(f"DELETE FROM usage WHERE id IN ({ph})", ch)
    # ---- context packs
    cd = context_days if context_days is not None else _env_days("context_days")
    ccut = t - cd * 86400
    keep = {r["id"] for r in s.q("SELECT id FROM context_packs p WHERE created_at = "
                                 "(SELECT MAX(created_at) FROM context_packs q WHERE q.name=p.name)")}
    keep |= {r["value"] for r in s.q("SELECT m.value FROM meta m JOIN jobs j ON m.key = 'job_ctx:' || j.id "
                                     "WHERE j.status='running'")}
    old = [r["id"] for r in s.q("SELECT id FROM context_packs WHERE created_at<?", (ccut,)) if r["id"] not in keep]
    out["context_packs_deleted"] = len(old)
    if not dry_run:
        for pid in old:
            s.x("DELETE FROM context_packs WHERE id=?", (pid,))
            s.x("DELETE FROM context_loads WHERE pack_id=?", (pid,))
            s.x("DELETE FROM relations WHERE src=? OR dst=?", (pid, pid))
        s.x("UPDATE context_packs SET parent_id=NULL WHERE parent_id IS NOT NULL AND parent_id NOT IN (SELECT id FROM context_packs)")
    used = set()
    for r in s.q("SELECT brief_hash, sections FROM context_packs"):
        used.add(r["brief_hash"])
        used |= {h for _, h in json.loads(r["sections"] or "[]")}
    orphans = [r["hash"] for r in s.q("SELECT hash FROM context_blobs") if r["hash"] not in used]
    out["context_blobs_gc"] = len(orphans)
    if not dry_run:
        for h in orphans:
            s.x("DELETE FROM context_blobs WHERE hash=?", (h,))
    # ---- logs and caches
    ad = audit_days if audit_days is not None else _env_days("audit_days")
    trims = [("audit", "ts", ad), ("mcp_calls", "ts", _env_days("calls_days")), ("mcp_cache", "ts", _env_days("cache_days")),
             ("research_cache", "ts", _env_days("cache_days") * 4), ("context_loads", "ts", cd)]
    for table, col, days in trims:
        try:
            n = s.q(f"SELECT COUNT(*) n FROM {table} WHERE {col}<?", (t - days * 86400,))[0]["n"]
        except Exception:  # noqa: BLE001 - optional tables
            continue
        out[f"{table}_trimmed"] = n
        if n and not dry_run:
            if table == "audit":
                out["audit_trimmed"] = _trim_audit(s, t - days * 86400)
            else:
                s.x(f"DELETE FROM {table} WHERE {col}<?", (t - days * 86400,))
    if not dry_run:
        s.x("INSERT OR REPLACE INTO meta VALUES('last_prune', ?)", (json.dumps(out | {"ts": t}),))
        s.audit(None, "retention.prune", "", out)
    return out | {"dry_run": dry_run}


# ---------------------------------------------------------------------------- dump / load
def _tables(s: Store) -> list[str]:
    if s.dialect == "postgres":
        rows = s.q("SELECT table_name AS name FROM information_schema.tables WHERE table_schema=current_schema() "
                   "AND table_type='BASE TABLE'")
    else:
        rows = s.q("SELECT name FROM sqlite_master WHERE type='table'")
    return sorted(r["name"] for r in rows if r["name"] not in SKIP_TABLES and not r["name"].startswith(("items_fts", "sqlite_")))


def _enc(v):
    return {"$b64": base64.b64encode(v).decode()} if isinstance(v, (bytes, bytearray)) else v


def _dec(v):
    return base64.b64decode(v["$b64"]) if isinstance(v, dict) and "$b64" in v else v


def dump(s: Store, path: str) -> dict:
    n = 0
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "wt", encoding="utf-8") as f:
        f.write(json.dumps({"$commons_dump": 1, "ts": now(), "dialect": s.dialect}) + "\n")
        for t in _tables(s):
            cols = None
            for r in s.q(f"SELECT * FROM {t}"):
                cols = cols or [c for c in r.keys() if c not in ("v", "tsv")]
                f.write(json.dumps({"t": t, "r": {c: _enc(r[c]) for c in cols}}, default=str) + "\n")
                n += 1
    return {"path": path, "rows": n}


def load(s: Store, path: str) -> dict:
    """Restore a dump (idempotent: existing ids are kept). Also accepts the portable export-memory format."""
    opener = gzip.open if path.endswith(".gz") else open
    counts: dict[str, int] = {}
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            d = json.loads(line)
            if "$commons_dump" in d:
                continue
            if "t" in d:
                t, r = d["t"], {k: _dec(v) for k, v in d["r"].items()}
                cols = list(r)
                s.x(f"INSERT OR IGNORE INTO {t}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", [r[c] for c in cols])
                counts[t] = counts.get(t, 0) + 1
            elif d.get("kind") in ("memory", "fact", "decision", "note", "handoff", "checkpoint") and d.get("content"):
                if not s.q("SELECT 1 FROM items WHERE id=?", (d.get("id"),)):
                    s.add_item(d["kind"], d["content"], title=d.get("title") or "", project=d.get("project"),
                               tags=d.get("tags"))
                    counts["items(portable)"] = counts.get("items(portable)", 0) + 1
    # rebuild keyword index + embeddings for restored items
    have = {r["item_id"] for r in s.q("SELECT item_id FROM items_fts")}
    for r in s.q("SELECT id,title,summary,content,tags FROM items"):
        if r["id"] not in have:
            s.x("INSERT INTO items_fts(item_id,title,summary,content,tags) VALUES(?,?,?,?,?)",
                (r["id"], r["title"], r["summary"], r["content"], " ".join(json.loads(r["tags"] or "[]"))))
    if s.dialect == "postgres":
        for t in _tables(s):
            if s.db._table(t)[2]:
                s.db.conn.execute(f"SELECT setval(pg_get_serial_sequence('{t}','id'), COALESCE((SELECT MAX(id) FROM {t}),0)+1, false)")
        s.sync_vectors()
    return counts


# ---------------------------------------------------------------------------- backups
def _key() -> bytes | None:
    k = os.environ.get("COMMONS_BACKUP_KEY")
    return hashlib.sha256(k.encode()).digest() if k else None


def encrypt_file(src: str, dst: str, key: bytes) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = os.urandom(12)
    data = Path(src).read_bytes()
    Path(dst).write_bytes(b"CMNSENC1" + nonce + AESGCM(key).encrypt(nonce, data, b"commons-backup"))


def decrypt_file(src: str, dst: str, key: bytes) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = Path(src).read_bytes()
    if not raw.startswith(b"CMNSENC1"):
        raise ValueError("not an encrypted Commons backup")
    Path(dst).write_bytes(AESGCM(key).decrypt(raw[8:20], raw[20:], b"commons-backup"))


def backup(s: Store, out_dir: str | None = None, keep: int | None = None, encrypt: bool | None = None) -> dict:
    d = Path(out_dir or os.environ.get("COMMONS_BACKUP_DIR") or Path.home() / ".commons" / "backups")
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    if s.dialect == "postgres":
        path = str(d / f"commons-{stamp}.jsonl.gz")
        dump(s, path)
    else:
        import sqlite3
        path = str(d / f"commons-{stamp}.db")
        dst = sqlite3.connect(path)
        with s.lock:
            s.db.backup(dst)
        dst.close()
    key = _key()
    if (encrypt is None and key) or encrypt:
        if not key:
            raise ValueError("set COMMONS_BACKUP_KEY to encrypt backups")
        encrypt_file(path, path + ".enc", key)
        os.remove(path)
        path += ".enc"
    keep = keep if keep is not None else int(os.environ.get("COMMONS_BACKUP_KEEP", "7"))
    olds = sorted(p for p in d.glob("commons-*") if p.is_file())
    for p in olds[:-keep] if keep > 0 else []:
        p.unlink()
    s.x("INSERT OR REPLACE INTO meta VALUES('last_backup', ?)", (json.dumps({"path": path, "ts": now()}),))
    s.audit(None, "backup", path, {"encrypted": path.endswith(".enc")})
    return {"path": path, "encrypted": path.endswith(".enc"), "kept": min(len(olds), keep)}
