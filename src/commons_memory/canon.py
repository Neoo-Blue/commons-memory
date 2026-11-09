"""Canon (v0.38): the current truth per product, for the company and its vendors, kept as Commons facts.

Four subjects sit on ONE global timeline, whatever project the writer is in (facts.py): product:<key> (what a customer
may be told), work:<key> (internal state), company, and vendor:<name>. A new value supersedes the old one and the
history stays. Owner-only predicates (company.owner_only, default OWNER_ONLY) change only through
record_decision(sets=...) or the Pulse relay (agent "pulse", carrying the owner's Pulse Studio edits); any agent may
set the others, with evidence. Copies (kits, caches, peer briefs, llms.txt, phone prompts) are rendered from Canon and
never write back. canon() renders it, changes() lists what moved, pickup() opens with it, turn() adds what changed.
"""
from __future__ import annotations

import json

from .ids import iso, now
from .store import CommonsError, _parse_time

PRODUCT = ("offer", "prices", "setup_fee", "trial", "refund", "partner_terms", "platforms", "features", "promises",
           "claims_forbidden", "not_offered")
WORK = ("status", "direction", "do_not_merge", "repos", "deploy", "phone_lines", "places")
OWNER_ONLY = ("product.prices", "product.setup_fee", "product.trial", "product.refund", "product.partner_terms",
              "product.promises", "product.claims_forbidden", "product.not_offered", "work.direction",
              "work.do_not_merge", "work.status", "company.focus", "company.standing_rules", "company.precedence",
              "company.owner_only", "vendor.terms")
PRECEDENCE = ("1 the owner's words in the current conversation; 2 Canon; 3 the system of record in its domain (Stripe "
              "for charged prices, origin/main for what shipped): a disagreement with Canon is drift to report, never "
              "a silent pick; 4 live pages, Pulse kits and guides; 5 repo docs; 6 notes, memory files, handoffs and chat")
RELAY = "pulse"             # the Pulse relay, carrying the owner's Pulse Studio edits
TRANSIENT = ("this_week",)  # Pulse's own weekly note: on the global timeline, but not part of the record or its changes
WEEK = 7 * 86400


def kind_of(subject) -> str | None:
    """product | work | company | vendor for a Canon subject, else None."""
    s = str(subject or "")
    if s == "company" or s.startswith("company:"):
        return "company"
    k, _, rest = s.partition(":")
    return k if k in ("product", "work", "vendor") and rest else None


def is_canon(subject) -> bool:
    return kind_of(subject) is not None


# ---------------------------------------------------------------------------------------------- values
def parse(v):
    """Stored text back to a value: JSON lists and objects come back as such, anything else stays as it is."""
    if isinstance(v, str) and v[:1] in ("[", "{"):
        try:
            return json.loads(v)
        except ValueError:
            pass
    return v


def dump(v) -> str:
    """One stored form per value, so equal values compare equal."""
    v = parse(v)
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def show(v) -> str:
    v = parse(v)
    if isinstance(v, list):
        return "; ".join(show(x) for x in v)
    if isinstance(v, dict):
        return "; ".join(f"{k} = {show(x)}" for k, x in v.items())
    return "" if v is None else str(v)


def _clip(t: str, n: int) -> str:
    return t if len(t) <= n else t[:n - 3] + "..."


def _list(v) -> list:
    v = parse(v)
    return v if isinstance(v, list) else ([v] if v not in (None, "") else [])


def _name(s, agent_id) -> str:
    r = s.q("SELECT name FROM agents WHERE id=?", (agent_id,)) if agent_id else []
    return r[0]["name"] if r else ""


# ---------------------------------------------------------------------------------------------- reading
def _data(s) -> dict[str, dict]:
    """{subject: {predicate: value}} of the current Canon facts (a later row wins)."""
    rows = [dict(r) for r in s.q(
        "SELECT id, subject, predicate, value FROM items WHERE kind='fact' AND superseded_by IS NULL AND "
        "(valid_to IS NULL OR valid_to>?) AND (subject='company' OR subject LIKE 'company:%' OR subject LIKE "
        "'product:%' OR subject LIKE 'work:%' OR subject LIKE 'vendor:%') ORDER BY COALESCE(valid_from, created_at)",
        (now(),))]
    hidden = s.hidden_ids([r["id"] for r in rows])
    out: dict[str, dict] = {}
    for r in rows:
        if r["id"] not in hidden and is_canon(r["subject"]) and r["predicate"] not in TRANSIENT:
            out.setdefault(r["subject"], {})[r["predicate"]] = parse(r["value"])
    return out


def _keys(data) -> list[str]:
    return sorted({sub.split(":", 1)[1] for sub in data if kind_of(sub) in ("product", "work")})


def owner_only(s, data: dict | None = None) -> list[str]:
    """company.owner_only when the owner set it, else the default list."""
    if data is None:
        r = s.q("SELECT value FROM items WHERE kind='fact' AND subject='company' AND predicate='owner_only' AND "
                "superseded_by IS NULL AND (valid_to IS NULL OR valid_to>?) ORDER BY COALESCE(valid_from, created_at) "
                "DESC LIMIT 1", (now(),))
        v = parse(r[0]["value"]) if r else None
    else:
        v = data.get("company", {}).get("owner_only")
    return [str(x) for x in v] if isinstance(v, list) else list(OWNER_ONLY)


_CONFLICTS = ("SELECT c.id, c.a, c.b, c.subject, c.predicate, a.kind AS ka, a.subject AS sa, a.predicate AS pa, "
              "b.kind AS kb, b.subject AS sb, b.predicate AS pb FROM conflicts c LEFT JOIN items a ON a.id=c.a "
              "LEFT JOIN items b ON b.id=c.b ")


def _owner_only_in(r, oo) -> str | None:
    """The owner-only subject.predicate a conflict is about (its own, or a Canon fact on either side), else None."""
    for sub, pred, kind in ((r["subject"], r["predicate"], "fact"), (r["sa"], r["pa"], r["ka"]),
                            (r["sb"], r["pb"], r["kb"])):
        if kind == "fact" and f"{kind_of(sub)}.{pred}" in oo:
            return f"{sub}.{pred}"
    return None


def open_conflicts(s, oo=None) -> tuple[int, int]:
    """(open conflicts, of which on owner-only facts)."""
    oo = set(owner_only(s) if oo is None else oo)
    rows = s.q(_CONFLICTS + "WHERE c.status='open'")
    return len(rows), sum(1 for r in rows if _owner_only_in(r, oo))


def _slug(url) -> str:
    """owner/name of a git remote URL (https or ssh) or of "owner/name"."""
    u = str(url or "").strip().lower().rstrip("/")
    u = u[:-4] if u.endswith(".git") else u
    return "/".join([p for p in u.replace(":", "/").split("/") if p][-2:])


def _repos(data, key) -> list[str]:
    return [_slug(u) for u in _list(data.get(f"work:{key}", {}).get("repos"))]


def _repo_key(data, repo) -> str | None:
    want = _slug(repo)
    return next((k for k in _keys(data) if want in _repos(data, k)), None)


# ---------------------------------------------------------------------------------------------- rendering
def _order(d: dict, first) -> list[str]:
    return [k for k in first if k in d] + sorted(k for k in d if k not in first)


def core(data, oo) -> str:
    """The company core, at most 20 lines: precedence and the owner-only list always, then the company facts."""
    c = data.get("company", {})
    lines = ["COMPANY CORE (Canon, the current truth: it outranks notes, docs and pages)",
             f"- precedence: {show(c.get('precedence') or PRECEDENCE)}",
             f"- owner-only, changed only by record_decision(sets=...) or the Pulse relay: {', '.join(oo) or 'none'}"]
    lines += [f"- {p}: {show(c[p])}" for p in ("focus", "agents") if c.get(p) not in (None, "")]
    rules = c.get("standing_rules")
    if isinstance(rules, list):
        lines += ["- standing rules:"] + [f"  - {show(x)}" for x in rules]
    elif rules:
        lines.append(f"- standing rules: {show(rules)}")
    lines += [f"- {p}: {show(v)}" for p, v in sorted(c.items())
              if p not in ("precedence", "owner_only", "focus", "agents", "standing_rules")]
    lines += [f"- {sub}.{p}: {show(v)}" for sub in sorted(data) if sub.startswith("company:")
              for p, v in sorted(data[sub].items())]
    if len(lines) > 20:
        lines = lines[:19] + ["- ... the rest: fact_get('company')"]
    return "\n".join(lines)


def card(key: str, entry: dict | None) -> str:
    p, w = (entry or {}).get("product", {}), (entry or {}).get("work", {})
    lines = [f"{key} (product:{key} = what customers may be told, work:{key} = internal):"]
    lines += [f"- {k}: {show(p[k])}" for k in _order(p, PRODUCT)]
    lines += [f"- work.{k}: {show(w[k])}" for k in _order(w, WORK)]
    return "\n".join(lines if len(lines) > 1 else lines + ["- no Canon facts yet"])


def one_liner(key: str, entry: dict) -> str:
    p, w = entry.get("product", {}), entry.get("work", {})
    bits = ([show(p["offer"])] if p.get("offer") else []) + (["prices: " + show(p["prices"])] if p.get("prices") else [])
    head = key + (f" [{show(w['status'])}]" if w.get("status") else "")
    return "- " + _clip(head + (": " + " | ".join(bits) if bits else ""), 160)


def _products(data) -> dict:
    return {k: {"product": data.get(f"product:{k}", {}), "work": data.get(f"work:{k}", {})} for k in _keys(data)}


# ---------------------------------------------------------------------------------------------- API
def _since(v) -> float:
    if v in (None, ""):
        return now() - WEEK
    if isinstance(v, (int, float)) or str(v).strip().replace(".", "", 1).isdigit():
        t = float(v)
        return t / 1000 if t > 1e11 else t  # epoch ms (the contract), or seconds
    try:
        return _parse_time(str(v))
    except ValueError:
        raise CommonsError(f"since: an ISO time, epoch ms or 7d/12h, not {v!r}") from None


def changes(s, since=None, keys: list[str] | None = None, until: float | None = None) -> list[dict]:
    """Canon changes after `since` (default: the last 7 days), oldest first: {at, subject, predicate, old, new, by,
    decision}. keys narrows the products (company and vendors always come); a value set again unchanged is no change."""
    rows = [dict(r) for r in s.q("SELECT id, subject, predicate, value, agent_id, meta, created_at FROM items WHERE "
                                 "kind='fact' AND created_at>? AND created_at<=? ORDER BY created_at",
                                 (_since(since), until or now()))]
    hidden = s.hidden_ids([r["id"] for r in rows])
    out = []
    for r in rows:
        k = kind_of(r["subject"])
        if not k or r["predicate"] in TRANSIENT or r["id"] in hidden or (
                keys is not None and k in ("product", "work") and r["subject"].split(":", 1)[1] not in keys):
            continue
        old = s.q("SELECT i.value FROM relations x JOIN items i ON i.id=x.dst WHERE x.src=? AND x.type='supersedes' "
                  "ORDER BY COALESCE(i.valid_from, i.created_at) DESC LIMIT 1", (r["id"],))
        old = old[0]["value"] if old else None
        if old is not None and dump(old) == dump(r["value"]):
            continue
        out.append({"at": iso(r["created_at"]), "subject": r["subject"], "predicate": r["predicate"],
                    "old": parse(old), "new": parse(r["value"]), "by": _name(s, r["agent_id"]) or None,
                    "decision": json.loads(r["meta"] or "{}").get("decision")})
    return out


def canon(s, product: str | None = None, repo: str | None = None) -> dict:
    """The contract's canon(): core, products, card, changes (7 days), open_conflicts, generated_at; plus vendors, and
    `product` (the key it resolved, None when the repo matches no work:<key>.repos) when product or repo is given."""
    data = _data(s)
    oo = owner_only(s, data)
    prods = _products(data)
    key = (product or "").strip().lower().removeprefix("product:") or (_repo_key(data, repo) if repo else None)
    out = {"core": core(data, oo),
           "products": {key: prods.get(key, {"product": {}, "work": {}})} if key else prods,
           "vendors": {sub.split(":", 1)[1]: v for sub, v in data.items() if kind_of(sub) == "vendor"},
           "card": card(key, prods.get(key)) if key else "\n".join(one_liner(k, e) for k, e in prods.items()),
           "changes": changes(s, keys=[key] if key else None), "open_conflicts": open_conflicts(s, oo)[0],
           "generated_at": iso(now())}
    if product or repo:
        out["product"] = key
    return out


def _worked_on(s, agent_id, project, data) -> list[str]:
    """Product keys of the current project and of this agent's work claims in the last 14 days, newest first: a
    project is a product when it is named after one of work:<key>.repos, or after the key itself."""
    names = ([project] if project else []) + [r["name"] for r in s.q(
        "SELECT p.name FROM work_claims c JOIN projects p ON p.id=c.project_id WHERE c.agent_id=? AND c.started_at>? "
        "ORDER BY c.started_at DESC LIMIT 200", (agent_id, now() - 14 * 86400))]
    keys = _keys(data)
    by_repo = {r.rsplit("/", 1)[-1]: k for k in reversed(keys) for r in _repos(data, k)}
    out: list[str] = []
    for n in names:
        k = by_repo.get(n.lower()) or (n.lower() if n.lower() in keys else None)
        if k and k not in out:
            out.append(k)
    return out


def facts_section(s, agent_id: str, project: str | None = None, cards: int = 3) -> str:
    """pickup()'s FACTS: the company core, then the cards of the products this agent worked on in the last 14 days
    (at most `cards`, the rest as one-liners), or every product's one-liner when none; then the open conflict count.
    Empty when there is no Canon and no open conflict."""
    data = _data(s)
    oo = owner_only(s, data)
    n, n_owner = open_conflicts(s, oo)
    if not data and not n:
        return ""
    lines = ["FACTS (Canon: read canon(product) before work on a product and after a compaction; changes(since) "
             "shows what moved)"]
    if data:
        prods = _products(data)
        mine = _worked_on(s, agent_id, project, data)
        lines += core(data, oo).splitlines()
        for k in mine[:cards]:
            lines += card(k, prods[k]).splitlines()
        rest = mine[cards:] if mine else list(prods)
        if rest:
            lines += ["PRODUCTS (canon(product) for the card):"] + [one_liner(k, prods[k]) for k in rest]
    if n:
        lines.append(f"OPEN CONFLICTS: {n}, {n_owner} on owner-only facts (ask the owner; never resolve those "
                     "yourself). conflicts() lists them.")
    return "\n".join(lines)


def _line(c: dict) -> str:
    old = "(none)" if c["old"] is None else _clip(show(c["old"]), 80)
    return (f"{c['at'][:16]} {c['subject']}.{c['predicate']}: {old} -> {_clip(show(c['new']), 120)} "
            f"(by {c['by'] or '?'}{', ' + c['decision'] if c['decision'] else ''})")


def news(s, agent_id: str, limit: int = 10) -> str:
    """Canon changes since this agent's last turn, each shown once (turn() appends them). The first turn shows the
    last 7 days."""
    key = f"canon_seen:{agent_id}"
    r = s.q("SELECT value FROM meta WHERE key=?", (key,))
    t = now()
    ch = changes(s, float(r[0]["value"]) if r else None, until=t)
    s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, str(t)))
    if not ch:
        return ""
    lines = ["CANON CHANGES since your last turn (cite ids, don't restate values; canon(product) for the card):"]
    lines += [f"- {_line(c)}" for c in ch[-limit:]]
    if len(ch) > limit:
        lines.append(f"- and {len(ch) - limit} earlier ones: changes(since='{ch[0]['at']}')")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------- writing
def guard(s, agent_id, subject, predicate, evidence=None) -> None:
    """An agent writing Canon: owner-only predicates are refused, and when `evidence` is checked (not None), every other
    predicate needs some. agent_id None is the local owner (CLI); the Pulse relay carries the owner's own edits."""
    kind = kind_of(subject)
    if not kind or not agent_id or _name(s, agent_id).lower() == RELAY:
        return
    if f"{kind}.{predicate}" in owner_only(s):
        raise CommonsError(
            f"{subject}.{predicate} is owner-only (company.owner_only): only the owner changes it. Ask the owner, one "
            f"line for this item, then record their words: record_decision(what, why=<the owner's words>, "
            f"sets=[{{\"subject\": \"{subject}\", \"predicate\": \"{predicate}\", \"value\": ...}}], supersedes=[ids "
            "of notes it retires]). The owner's Pulse Studio edits arrive through the Pulse relay.")
    if evidence is not None and not str(evidence).strip():
        raise CommonsError(f"{subject} is Canon: pass evidence (a commit, release, URL or item id showing that "
                           f"{predicate} is true now)")


def check_resolve(s, conflict_id: str, keep_id: str) -> None:
    """Agents never resolve a conflict on an owner-only fact (the owner's decision settles it), and never retire a
    Canon fact by keeping the other side: fact_set corrects it, closing the old value on its timeline."""
    r = s.q(_CONFLICTS + "WHERE c.id=?", (conflict_id,))
    if not r:
        return
    r = r[0]
    hit = _owner_only_in(r, set(owner_only(s)))
    if hit:
        raise CommonsError(f"{conflict_id} is on {hit}, an owner-only fact: agents never resolve it. Ask the owner, "
                           "then record_decision(why=<their words>, sets=[...]), which settles it.")
    lose = ("b", "sb", "kb") if keep_id == r["a"] else ("a", "sa", "ka")
    if r[lose[2]] == "fact" and is_canon(r[lose[1]]):
        raise CommonsError(f"{r[lose[0]]} is a Canon fact: keep it, or correct Canon with fact_set(subject, predicate, "
                           "value, evidence=...), which closes the old value")


def check_decision(s, sets, supersedes) -> list[dict]:
    """Validate record_decision's sets and supersedes before anything is written."""
    out = []
    for x in sets or []:
        if not isinstance(x, dict) or not str(x.get("subject") or "").strip() or \
                not str(x.get("predicate") or "").strip() or x.get("value") in (None, ""):
            raise CommonsError('each sets item is {"subject": "product:santa", "predicate": "prices", "value": [...]}')
        out.append({"subject": str(x["subject"]).strip(), "predicate": str(x["predicate"]).strip(), "value": x["value"]})
    for i in supersedes or []:
        r = s.q("SELECT project_id FROM items WHERE id=?", (i,))
        if not r:
            raise CommonsError(f"supersedes: no item {i}")
        s.check_project(r[0]["project_id"], "write")
        s._row_write(i)
    return out


def _settle(s, rows, agent_id, decision) -> None:
    """Mark conflicts resolved by a decision; an item stays flagged only while another conflict on it is open."""
    for c in rows:
        s.x("UPDATE conflicts SET status='resolved' WHERE id=?", (c["id"],))
    for i in {x for c in rows for x in (c["a"], c["b"])}:
        if not s.q("SELECT 1 FROM conflicts WHERE status='open' AND (a=? OR b=?)", (i, i)):
            s.x("UPDATE items SET conflict=0 WHERE id=?", (i,))
    if rows:
        s.audit(agent_id, "conflict.resolve", decision, {"by_decision": decision, "conflicts": [c["id"] for c in rows]})


def _parts(v) -> list[str]:
    v = parse(v)
    return [show(x) for x in (v if isinstance(v, list) else v.values() if isinstance(v, dict) else [v])]


def _stale(s, done: list[dict], exclude: set) -> list[str]:
    """Notes that still state a value a decision just replaced. Candidates: start_work's related() search (meaning)
    plus a keyword search for the old value (the FTS index: a LIKE scan of every note would hold the store for
    seconds on a big hub); kept only when the note's text contains a piece of the old value that the new one lacks."""
    from .work import related
    out: list[str] = []
    for d in done:
        if d["old"] is None or dump(d["old"]) == dump(d["value"]):
            continue
        new = show(d["value"]).lower()
        parts = [p for p in _parts(d["old"]) if len(p) >= 4 and p.lower() not in new]
        if not parts:
            continue
        hits = [h["id"] for h in related(s, f"{d['subject']} {d['predicate']} " + " ".join(parts), limit=20)]
        hits += s.rank(" ".join(parts), mode="keyword", kinds=["memory", "fact", "decision", "document", "handoff"],
                       limit=50)[0] or []
        for i in dict.fromkeys(hits):
            r = s.q("SELECT kind, subject, predicate, content, tags FROM items WHERE id=? AND superseded_by IS NULL", (i,))
            if not r or i in exclude or i in out or '"conversation"' in (r[0]["tags"] or "") or (
                    r[0]["kind"] == "fact" and r[0]["subject"] == d["subject"] and r[0]["predicate"] == d["predicate"]):
                continue
            if any(p.lower() in (r[0]["content"] or "").lower() for p in parts):
                out.append(i)
    return out


def apply_decision(s, decision: str, sets: list[dict], supersedes, agent_id, project) -> dict:
    """record_decision's sets and supersedes: each set becomes the current fact (closing the old value, history kept)
    and settles open conflicts on it; each superseded item is retired by the decision. related = notes that still
    state an old value, for the caller to supersede."""
    from .facts import Facts
    f, done = Facts(s), []
    for x in sets:
        r = f.set(x["subject"], x["predicate"], x["value"], project=project, agent_id=agent_id, decision=decision)
        done.append({"id": r["id"], "subject": x["subject"], "predicate": x["predicate"], "value": parse(dump(x["value"])),
                     "old": parse(r.get("old"))})
        closed = r.get("closed") or []  # conflicts on this fact, or on the values it closed, are settled by it
        ph = ",".join("?" * len(closed)) or "NULL"
        _settle(s, s.q(f"SELECT id, a, b FROM conflicts WHERE status='open' AND ((subject=? AND predicate=?) OR "
                       f"a IN ({ph}) OR b IN ({ph}))", [x["subject"], x["predicate"]] + closed + closed),
                agent_id, decision)
    gone, t = [], now()
    for i in dict.fromkeys(supersedes or []):
        if i != decision and s.q("SELECT 1 FROM items WHERE id=? AND superseded_by IS NULL", (i,)):
            s.x("UPDATE items SET superseded_by=?, valid_to=CASE WHEN kind='fact' THEN COALESCE(valid_to, ?) "
                "ELSE valid_to END WHERE id=?", (decision, t, i))
            s.relate(decision, i, "supersedes", {"at": t}, agent_id=agent_id)
            gone.append(i)
    if gone:
        ph = ",".join("?" * len(gone))
        _settle(s, s.q(f"SELECT id, a, b FROM conflicts WHERE status='open' AND (a IN ({ph}) OR b IN ({ph}))",
                       gone + gone), agent_id, decision)
    return {"set": done, "superseded": gone,
            "related": _stale(s, done, {decision, *gone, *(d["id"] for d in done)})}


def migrate(s) -> dict:
    """v0.38, once per database: Canon facts leave their projects for the one global timeline (Pulse wrote
    product:<key>.this_week in project 'pulse'), and where two projects each held a current value of the same
    subject.predicate, the newest stays current and closes the others."""
    if s.q("SELECT 1 FROM meta WHERE key='canon_global'"):
        return {}
    rows = [dict(r) for r in s.q("SELECT id, subject, predicate, project_id, valid_from, valid_to, superseded_by, "
                                 "created_at FROM items WHERE kind='fact' AND subject IS NOT NULL")]
    rows = [r for r in rows if is_canon(r["subject"])]
    moved, closed, cur = [r for r in rows if r["project_id"]], [], {}
    for r in rows:
        if r["superseded_by"] is None and r["valid_to"] is None:
            cur.setdefault((r["subject"], r["predicate"]), []).append(r)
    with s.tx():
        for r in moved:
            s.x("UPDATE items SET project_id=NULL WHERE id=?", (r["id"],))
            s.x("DELETE FROM relations WHERE src=? AND type='in_project'", (r["id"],))
        for group in cur.values():
            group.sort(key=lambda r: r["valid_from"] or r["created_at"])
            keep = group[-1]
            at = keep["valid_from"] or keep["created_at"]
            for o in group[:-1]:
                s.x("UPDATE items SET valid_to=?, superseded_by=? WHERE id=?", (at, keep["id"], o["id"]))
                s.relate(keep["id"], o["id"], "supersedes", {"at": at})
                closed.append(o["id"])
        s.x("INSERT OR REPLACE INTO meta VALUES('canon_global', ?)", (str(now()),))
    for pid in {r["project_id"] for r in moved}:
        s.kb_dirty(pid)
    if moved or closed:
        s.audit(None, "canon.migrate", "", {"moved": len(moved), "closed": closed})
    return {"moved": len(moved), "closed": closed}
