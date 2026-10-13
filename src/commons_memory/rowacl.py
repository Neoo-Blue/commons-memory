"""Row-level access: restrict any single record (a memory, decision, fact, board item, Drive file) to named agents.

A record with no rows here stays visible to everyone who can read its project (back-compatible). Once restricted,
only the listed agents, the record's author, the local owner (stdio/CLI) and agents in COMMONS_ADMIN_AGENTS see it;
perm "write" also lets a listed agent change or delete it. It is enforced where every read already goes through one
filter (Store.hidden_ids: search listings, expand, graph reads, context around hits), so a restricted memory never
shows up for anyone else.
"""
from __future__ import annotations

import os

from .ids import now
from .store import CommonsError, Store

SCHEMA = """
CREATE TABLE IF NOT EXISTS row_acl(ref TEXT, agent_id TEXT, perm TEXT, created_at REAL, PRIMARY KEY(ref, agent_id));
CREATE INDEX IF NOT EXISTS row_acl_agent ON row_acl(agent_id);
"""
PERMS = ("read", "write")


def migrate(db) -> None:
    db.executescript(SCHEMA)


def _admin(s: Store, principal: str | None) -> bool:
    if principal is None:
        return True
    names = {x.strip() for x in os.environ.get("COMMONS_ADMIN_AGENTS", "").split(",") if x.strip()}
    a = s.get_agent(principal)
    return bool(a and a["name"] in names)


def _author(s: Store, ref: str) -> str | None:
    for sql in ("SELECT agent_id a FROM items WHERE id=?", "SELECT agent_id a FROM board WHERE id=?",
                "SELECT created_by a FROM drive_files WHERE id=?"):
        r = s.q(sql, (ref,))
        if r:
            return r[0]["a"]
    return None


def _exists(s: Store, ref: str) -> bool:
    return any(s.q(f"SELECT 1 FROM {t} WHERE id=?", (ref,)) for t in ("items", "board", "drive_files"))


def restrict(s: Store, principal: str | None, ref: str, agents: list[str], perm: str = "read") -> dict:
    """Only the listed agents (plus the author and admins) may see ref from now on. Replaces any earlier list."""
    if perm not in PERMS:
        raise CommonsError("perm is read or write")
    if not _exists(s, ref):
        raise CommonsError(f"unknown id {ref}")
    if not (_admin(s, principal) or _author(s, ref) == principal):
        raise CommonsError("only the record's author or an admin can restrict it")
    ids = []
    for name in agents or []:
        a = s.get_agent(name)
        if not a:
            raise CommonsError(f"unknown agent {name}")
        ids.append(a["id"])
    if not ids:
        raise CommonsError("name at least one agent (use unrestrict to open it again)")
    s.x("DELETE FROM row_acl WHERE ref=?", (ref,))
    for aid in ids:
        s.x("INSERT INTO row_acl VALUES(?,?,?,?)", (ref, aid, perm, now()))
    s.audit(principal, "acl.row.restrict", ref, {"agents": ids, "perm": perm})
    return {"id": ref, "visible_to": [s.label(i)[1] for i in ids], "perm": perm}


def unrestrict(s: Store, principal: str | None, ref: str) -> dict:
    if not (_admin(s, principal) or _author(s, ref) == principal):
        raise CommonsError("only the record's author or an admin can open it again")
    s.x("DELETE FROM row_acl WHERE ref=?", (ref,))
    s.audit(principal, "acl.row.unrestrict", ref, {})
    return {"id": ref, "visible_to": "everyone with access to its project"}


def hidden(s: Store, principal: str | None, ids: list[str], need: str = "read") -> set[str]:
    """The restricted ids among `ids` that principal may not read (need=write: may not change)."""
    if principal is None or not ids or _admin(s, principal):
        return set()
    out: set[str] = set()
    for i in range(0, len(ids), 400):
        ch = ids[i:i + 400]
        ph = ",".join("?" * len(ch))
        rows = s.q(f"SELECT ref, agent_id, perm FROM row_acl WHERE ref IN ({ph})", ch)
        if not rows:
            continue
        ok = {r["ref"] for r in rows if r["agent_id"] == principal and (need == "read" or r["perm"] == "write")}
        for ref in {r["ref"] for r in rows} - ok:
            if _author(s, ref) != principal:
                out.add(ref)
    return out


def check(s: Store, principal: str | None, ref: str, need: str = "read") -> None:
    if hidden(s, principal, [ref], need):
        raise CommonsError(f"access denied: {need} on {ref}")


def listing(s: Store, ref: str) -> list[dict]:
    return [{"agent": s.label(r["agent_id"])[1], "perm": r["perm"]}
            for r in s.q("SELECT agent_id, perm FROM row_acl WHERE ref=?", (ref,))]
