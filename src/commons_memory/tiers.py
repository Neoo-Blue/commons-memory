"""Hot / warm / cold memory tiers (v0.9).

- hot: pinned, used in the last week, used repeatedly this month, or a decision from the last two weeks. pickup()
  hands the hot tier to every agent at check-in, so they start with what matters now.
- warm: everything else; searched normally.
- cold: not used for 90 days and either superseded, a low-value kind (observation, checkpoint, session summary) or
  older than 180 days. Ranked lower so it stops crowding results, never deleted, and promoted back on first use.

Every item returned by search / expand / get_items counts as an access. `retier()` runs in the background
(COMMONS_EVERY_TIERS, default hourly); pin and archive override it.
"""
from __future__ import annotations

from .ids import now
from .store import CommonsError, Store

COLUMNS = [("items", "tier", "TEXT"), ("items", "access_count", "INTEGER"), ("items", "last_access", "REAL"),
           ("items", "pinned", "INTEGER")]
DAY = 86400.0


def add_columns(db) -> None:
    from .facts import add_columns as add
    add(db, COLUMNS)


def touch(s: Store, ids: list[str]) -> None:
    """Record an access; a cold item that gets used is warm again right away."""
    ids = [i for i in ids if isinstance(i, str)]
    for b in range(0, len(ids), 400):
        ch = ids[b:b + 400]
        s.x(f"UPDATE items SET access_count=COALESCE(access_count,0)+1, last_access=?, "
            f"tier=CASE WHEN tier='cold' THEN 'warm' ELSE tier END WHERE id IN ({','.join('?' * len(ch))})", [now()] + ch)


def retier(s: Store) -> dict:
    t = now()
    s.x("UPDATE items SET tier='hot' WHERE COALESCE(pinned,0)=1 OR last_access>=? OR "
        "(COALESCE(access_count,0)>=3 AND last_access>=?) OR (kind='decision' AND created_at>=? AND superseded_by IS NULL)",
        (t - 7 * DAY, t - 30 * DAY, t - 14 * DAY))
    s.x("UPDATE items SET tier='cold' WHERE COALESCE(pinned,0)=0 AND COALESCE(last_access, created_at)<? AND "
        "(superseded_by IS NOT NULL OR kind IN ('observation','checkpoint') OR tags LIKE '%\"session-summary\"%' "
        "OR created_at<?)", (t - 90 * DAY, t - 180 * DAY))
    s.x("UPDATE items SET tier='warm' WHERE tier IS NULL OR (tier='hot' AND COALESCE(pinned,0)=0 AND "
        "COALESCE(last_access,0)<? AND NOT (COALESCE(access_count,0)>=3 AND last_access>=?) AND NOT "
        "(kind='decision' AND created_at>=? AND superseded_by IS NULL))", (t - 7 * DAY, t - 30 * DAY, t - 14 * DAY))
    return {r["tier"]: r["n"] for r in s.q("SELECT tier, COUNT(*) n FROM items GROUP BY tier")}


def set_tier(s: Store, iid: str, tier: str) -> dict:
    """pin (hot, sticky) / warm / archive (cold)."""
    tier = {"pin": "hot", "archive": "cold"}.get(tier, tier)
    if tier not in ("hot", "warm", "cold"):
        raise CommonsError("tier must be hot (pin), warm or cold (archive)")
    r = s.q("SELECT project_id FROM items WHERE id=?", (iid,))
    if not r:
        raise CommonsError(f"unknown id {iid}")
    s.check_project(r[0]["project_id"], "write")
    if tier == "cold":  # archive sticks until the item is used again (forget the accesses that made it hot)
        s.x("UPDATE items SET tier='cold', pinned=0, last_access=NULL, access_count=0 WHERE id=?", (iid,))
    else:
        s.x("UPDATE items SET tier=?, pinned=? WHERE id=?", (tier, 1 if tier == "hot" else 0, iid))
    return {"id": iid, "tier": tier, "pinned": tier == "hot"}


def hot(s: Store, project: str | None = None, limit: int = 8) -> list[dict]:
    """The hot set for check-in: pinned first, then most recently used."""
    pid = s.find_project(project) if project else None
    where, args = "tier='hot' AND superseded_by IS NULL", []
    if pid:
        where += " AND (project_id=? OR project_id IS NULL)"
        args.append(pid)
    denied = s.denied_projects()
    if denied:
        where += f" AND (project_id IS NULL OR project_id NOT IN ({','.join('?' * len(denied))}))"
        args += denied
    return [dict(r) for r in s.q(f"SELECT id, kind, summary FROM items WHERE {where} "
                                 f"ORDER BY COALESCE(pinned,0) DESC, COALESCE(last_access, created_at) DESC LIMIT ?", args + [limit])]
