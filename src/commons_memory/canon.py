"""Canon (v0.38): the current truth per product, for the company and its vendors, kept as Commons facts.

Four subjects sit on ONE global timeline, whatever project the writer is in (facts.py): product:<key> (what a customer
may be told), work:<key> (internal state), company, and vendor:<name>. A new value supersedes the old one and the
history stays. Owner-only predicates (company.owner_only, default OWNER_ONLY; company.owner_only and
company.precedence always) change only through record_decision(sets=...) quoting the owner (the Pulse relay records
the owner's Pulse Studio edits that way too); any agent may set the others, with evidence. Predicates have one form
(lowercase, a-z 0-9 _), values say something, and a Canon fact, like the decision that set one, is never deleted,
restricted, filed in a project or retired: it is corrected by a newer value, or ended by fact_invalidate. Copies (kits,
caches, peer briefs, llms.txt, phone prompts) are rendered from Canon and never write back. canon() renders it,
changes() lists what moved, pickup() opens with it, turn() adds what changed.
"""
from __future__ import annotations

import json
import logging
import re

from .ids import iso, new_id, now
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
ALWAYS_OWNER = ("company.owner_only", "company.precedence")  # owner-only whatever the stored list says
TRANSIENT = ("this_week",)  # Pulse's own weekly note: on the global timeline, but not part of the record or its changes
PREDICATE = re.compile(r"[a-z][a-z0-9_]*")
OO_ENTRY = re.compile(r"(product|work|company|vendor)\.[a-z][a-z0-9_]*")  # an entry of company.owner_only
KEY = re.compile(r"[a-z0-9][a-z0-9_.-]*")  # the key of product:, work:, vendor: and company: subjects
WEEK = 7 * 86400
LAG = 30.0  # a write commits after its created_at (embedding, lock waits): news() holds its watermark this far back
SKEW = 60.0  # a client clock this far ahead still counts as now


def kind_of(subject) -> str | None:
    """product | work | company | vendor for a Canon subject, else None."""
    s = str(subject or "")
    if s == "company" or s.startswith("company:"):
        return "company"
    k, _, rest = s.partition(":")
    return k if k in ("product", "work", "vendor") and rest else None


def is_canon(subject) -> bool:
    return kind_of(subject) is not None


def check_subject(subject) -> None:
    """A Canon subject is 'company' or <kind>:<key>, the key lowercase letters, digits, _ . and - (product:santa,
    company:ops): a line break or control character in one would reach every agent's injected Canon as lines no one
    wrote. Writes check it (fact_set, fact_invalidate, record_decision's sets); other subjects pass as given."""
    s = str(subject or "")
    if is_canon(s) and s != "company" and not KEY.fullmatch(s.split(":", 1)[1]):
        kind, _, key = s.partition(":")
        raise CommonsError(f"{kind}:<key> takes a key of lowercase letters, digits, _ . and - (like product:santa), "
                           f"not {key!r}")


# ---------------------------------------------------------------------------------------------- values
def parse(v):
    """Stored text back to a value: JSON lists and objects come back as such, anything else stays as it is."""
    if isinstance(v, str) and v[:1] in ("[", "{"):
        try:
            return json.loads(v)
        except ValueError:
            pass
    return v


def _num(v):
    """Numbers in one form (19.0 is 19), all the way down."""
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, list):
        return [_num(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _num(x) for k, x in v.items()}
    return v


def dump(v) -> str:
    """One stored form per value (the order as given, for display)."""
    v = parse(v)
    return v if isinstance(v, str) else json.dumps(_num(v), ensure_ascii=False)


def same(a, b) -> bool:
    """Equal values whatever their form: JSON text or a list, keys in any order, 19 or 19.0."""
    def key(v):
        return json.dumps(_num(parse(v)), sort_keys=True, ensure_ascii=False)
    return key(a) == key(b)


def blank(v) -> bool:
    """A value that says nothing: none, text that strips to nothing, an empty object. An empty list says something
    (Canon holds that nothing is listed)."""
    v = parse(v)
    return v is None or (isinstance(v, str) and not v.strip()) or v == {}


def predicate_of(subject, predicate) -> str:
    """A Canon predicate in its one form (stripped, lowercase), or CommonsError when it has any other character, so
    'Prices', 'prices ' and a zero-width variant never stand beside 'prices'. Other subjects keep theirs as given."""
    if not is_canon(subject):
        return predicate
    p = str(predicate or "").strip().casefold()
    if not PREDICATE.fullmatch(p):
        raise CommonsError(f"{subject}: a Canon predicate is lowercase letters, digits and _ (like setup_fee), "
                           f"not {predicate!r}")
    return p


def _one(x) -> str:
    """Text on one line: whitespace runs, line breaks and Unicode line separators included, become one space."""
    return " ".join(str(x).split())


def show(v) -> str:
    """A value on one line (whitespace runs, newlines included, become one space: no value forges a line)."""
    v = parse(v)
    if isinstance(v, list):
        return "; ".join(show(x) for x in v)
    if isinstance(v, dict):
        return "; ".join(f"{_one(k)} = {show(x)}" for k, x in v.items())
    return "" if v is None else _one(v)


def _clip(t: str, n: int) -> str:
    return t if len(t) <= n else t[:n - 3] + "..."


def _list(v) -> list:
    v = parse(v)
    return v if isinstance(v, list) else ([v] if v not in (None, "") else [])


def _name(s, agent_id) -> str:
    r = s.q("SELECT name FROM agents WHERE id=?", (agent_id,)) if agent_id else []
    return r[0]["name"] if r else ""


# ---------------------------------------------------------------------------------------------- reading
def _canon_rows(s) -> str:
    """WHERE for Canon facts that have a predicate. On SQLite as ranges on items_fact (subject, predicate): LIKE ignores
    case there, so it can't use that index, and kind='fact' alone reads every fact ever written (+kind keeps the
    planner off items_kind). Postgres keeps LIKE (a range would follow the collation's order). is_canon() still
    checks each row."""
    kinds = ("company", "product", "work", "vendor")
    if getattr(s, "dialect", "sqlite") == "sqlite":
        subj = " OR ".join(["subject='company'"] + [f"(subject>='{k}:' AND subject<'{k};')" for k in kinds])
        return f"+kind='fact' AND ({subj}) AND predicate IS NOT NULL AND predicate<>''"
    subj = " OR ".join(["subject='company'"] + [f"subject LIKE '{k}:%'" for k in kinds])
    return f"kind='fact' AND ({subj}) AND predicate IS NOT NULL AND predicate<>''"


def _data(s) -> dict[str, dict]:
    """{subject: {predicate: value}} of the current Canon facts (a later row wins)."""
    rows = [dict(r) for r in s.q(
        f"SELECT id, subject, predicate, value FROM items WHERE {_canon_rows(s)} AND superseded_by IS NULL AND "
        "(valid_to IS NULL OR valid_to>?) ORDER BY COALESCE(valid_from, created_at)", (now(),))]
    hidden = s.hidden_ids([r["id"] for r in rows])
    out: dict[str, dict] = {}
    for r in rows:
        if r["id"] not in hidden and is_canon(r["subject"]) and PREDICATE.fullmatch(r["predicate"]) and \
                r["predicate"] not in TRANSIENT:  # a predicate with no one form ('setup fee') is never a field
            out.setdefault(r["subject"], {})[r["predicate"]] = parse(r["value"])
    return out


def _keys(data) -> list[str]:
    return sorted({sub.split(":", 1)[1] for sub in data if kind_of(sub) in ("product", "work")})


def owner_only(s, data: dict | None = None) -> list[str]:
    """company.owner_only when the owner set it, else the default list; company.owner_only and company.precedence
    always, so no narrowing of the list ever lets an agent rewrite it. Entries are compared in one form:
    'Product.Prices' and ' product.trial' protect product.prices and product.trial."""
    if data is None:
        r = s.q("SELECT value FROM items WHERE kind='fact' AND subject='company' AND predicate='owner_only' AND "
                "superseded_by IS NULL AND (valid_to IS NULL OR valid_to>?) ORDER BY COALESCE(valid_from, created_at) "
                "DESC LIMIT 1", (now(),))
        v = parse(r[0]["value"]) if r else None
    else:
        v = data.get("company", {}).get("owner_only")
    oo = [str(x).strip().casefold() for x in v] if isinstance(v, list) else list(OWNER_ONLY)
    return list(dict.fromkeys(oo + list(ALWAYS_OWNER)))


_CONFLICTS = ("SELECT c.id, c.a, c.b, c.subject, c.predicate, a.kind AS ka, a.subject AS sa, a.predicate AS pa, "
              "a.meta AS ma, b.kind AS kb, b.subject AS sb, b.predicate AS pb, b.meta AS mb FROM conflicts c "
              "LEFT JOIN items a ON a.id=c.a LEFT JOIN items b ON b.id=c.b ")


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
    return [k for k in first if k in d] + sorted((k for k in d if k not in first), key=str)


def core(data, oo) -> str:
    """The company core, at most 20 lines: precedence and the owner-only list always, then the company facts, each
    clipped to 240 characters (one long value never crowds out the rest)."""
    c = data.get("company", {})
    lines = ["COMPANY CORE (Canon, the current truth: it outranks notes, docs and pages)",
             f"- precedence: {show(c.get('precedence') or PRECEDENCE)}",
             f"- owner-only, changed only by record_decision(sets=...) quoting the owner: {', '.join(oo) or 'none'}"]
    facts = [f"- {p}: {show(c[p])}" for p in ("focus", "agents") if c.get(p) not in (None, "")]
    rules = c.get("standing_rules")
    if isinstance(rules, list):
        facts += ["- standing rules:"] + [f"  - {show(x)}" for x in rules]
    elif rules:
        facts.append(f"- standing rules: {show(rules)}")
    facts += [f"- {p}: {show(v)}" for p, v in sorted(c.items(), key=lambda kv: str(kv[0]))
              if p not in ("precedence", "owner_only", "focus", "agents", "standing_rules")]
    facts += [f"- {_one(sub)}.{p}: {show(v)}" for sub in sorted(data) if sub.startswith("company:")
              for p, v in sorted(data[sub].items(), key=lambda kv: str(kv[0]))]
    lines += [_clip(x, 240) for x in facts]
    if len(lines) > 20:
        lines = lines[:19] + ["- ... the rest: fact_get('company')"]
    return "\n".join(lines)


def card(key: str, entry: dict | None) -> str:
    p, w, key = (entry or {}).get("product", {}), (entry or {}).get("work", {}), _one(key)
    lines = [f"{key} (product:{key} = what customers may be told, work:{key} = internal):"]
    lines += [f"- {k}: {show(p[k])}" for k in _order(p, PRODUCT)]
    lines += [f"- work.{k}: {show(w[k])}" for k in _order(w, WORK)]
    return "\n".join(lines if len(lines) > 1 else lines + ["- no Canon facts yet"])


def one_liner(key: str, entry: dict) -> str:
    p, w = entry.get("product", {}), entry.get("work", {})
    bits = ([show(p["offer"])] if p.get("offer") else []) + (["prices: " + show(p["prices"])] if p.get("prices") else [])
    head = _one(key) + (f" [{show(w['status'])}]" if w.get("status") else "")
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


def _meta(r) -> dict:
    try:
        m = json.loads(r["meta"] or "{}")
    except (ValueError, TypeError):
        return {}
    return m if isinstance(m, dict) else {}


def changes(s, since=None, keys: list[str] | None = None, until: float | None = None) -> list[dict]:
    """Canon changes after `since` (default: the last 7 days), oldest first: {id, at, subject, predicate, old, new, by,
    decision}; an ended value (fact_invalidate) is old -> None. keys narrows the products (company and vendors always
    come). No change: a value set again unchanged (in any form), and a backfill (a row that was not current when it
    was written)."""
    lo, hi = _since(since), until or now()
    cols = "id, subject, predicate, value, agent_id, meta, created_at, valid_to, superseded_by"
    rows = [dict(r) for r in s.q(f"SELECT {cols} FROM items WHERE kind='fact' AND created_at>? AND created_at<=? "
                                 "ORDER BY created_at", (lo, hi))]
    ended = [dict(r) for r in s.q(f"SELECT {cols} FROM items WHERE kind='fact' AND superseded_by='invalidated' AND "
                                  "updated_at>?", (lo,))]  # when it ended is in meta (updated_at may move later)
    hidden = s.hidden_ids([r["id"] for r in rows + ended])

    def wanted(r) -> bool:
        k = kind_of(r["subject"])
        return bool(k and PREDICATE.fullmatch(r["predicate"] or "") and r["predicate"] not in TRANSIENT and
                    r["id"] not in hidden and (keys is None or k not in ("product", "work") or
                                               r["subject"].split(":", 1)[1] in keys))
    out = []
    for r in rows:
        if not wanted(r) or (r["valid_to"] is not None and r["valid_to"] <= r["created_at"] and
                             r["superseded_by"] != "invalidated"):
            continue  # a backfill slots in before a later value: it was never the current one
        # the newest value it closed that is still a fact and the caller may read: a row the migration kept out of
        # Canon (a note now) or one restricted to other agents is never shown as the old value
        old = s.q("SELECT i.id, i.value FROM relations x JOIN items i ON i.id=x.dst WHERE x.src=? AND "
                  "x.type='supersedes' AND i.kind='fact' ORDER BY COALESCE(i.valid_from, i.created_at) DESC",
                  (r["id"],))
        unseen = s.hidden_ids([o["id"] for o in old]) if old else set()
        old = next((o["value"] for o in old if o["id"] not in unseen), None)
        if old is not None and same(old, r["value"]):
            continue
        out.append((r["created_at"], {"id": r["id"], "at": iso(r["created_at"]), "subject": r["subject"],
                                      "predicate": r["predicate"], "old": parse(old), "new": parse(r["value"]),
                                      "by": _name(s, r["agent_id"]) or None, "decision": _meta(r).get("decision")}))
    for r in ended:
        e = _meta(r).get("ended")
        if not wanted(r) or not isinstance(e, dict) or not isinstance(e.get("at"), (int, float)) or \
                not lo < e["at"] <= hi:
            continue
        out.append((e["at"], {"id": r["id"], "at": iso(e["at"]), "subject": r["subject"], "predicate": r["predicate"],
                              "old": parse(r["value"]), "new": None, "by": _name(s, e.get("by")) or None,
                              "decision": None}))
    return [c for _, c in sorted(out, key=lambda x: x[0])]


def canon(s, product: str | None = None, repo: str | None = None) -> dict:
    """The contract's canon(): core, products, card, changes (7 days), open_conflicts, generated_at; plus company (its
    facts), owner_only (the list in force), vendors, and `product` (the key it resolved, None when the repo matches no
    work:<key>.repos) when product or repo is given."""
    data = _data(s)
    oo = owner_only(s, data)
    prods = _products(data)
    key = (product or "").strip().lower().removeprefix("product:") or (_repo_key(data, repo) if repo else None)
    out = {"core": core(data, oo),
           "products": {key: prods.get(key, {"product": {}, "work": {}})} if key else prods,
           "company": data.get("company", {}), "owner_only": oo,
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
    new = "(ended)" if c["new"] is None else _clip(show(c["new"]), 120)
    return (f"{c['at'][:16]} {_one(c['subject'])}.{c['predicate']}: {old} -> {new} "
            f"(by {_one(c['by'] or '?')}{', ' + c['decision'] if c.get('decision') else ''})")


def before(at: str) -> str:
    """A since= that still lists the change stamped `at` (changes() lists what came after since; times are seconds)."""
    return iso(_parse_time(at) - 1)


def _event(c: dict) -> str:
    return f"{c.get('id')}|{'ended' if c.get('new') is None else 'set'}"


def news(s, agent_id: str, limit: int = 10) -> str:
    """Canon changes since this agent's last turn, each shown once (turn() appends them). The first turn shows the
    last 7 days. The watermark stays LAG seconds behind now, so a write that commits after it was stamped is still
    seen, and the changes of that window already shown are kept by id and not shown again."""
    key = f"canon_seen:{agent_id}"
    r = s.q("SELECT value FROM meta WHERE key=?", (key,))
    try:
        prev = json.loads(r[0]["value"]) if r else {}
    except (ValueError, TypeError):
        prev = {}
    prev = prev if isinstance(prev, dict) else {"t": prev} if isinstance(prev, (int, float)) else {}
    t, seen = now(), set(prev.get("ids") or [])
    every = changes(s, prev.get("t"), until=t)
    ch = [c for c in every if _event(c) not in seen]
    recent = sorted({_event(c) for c in every if _parse_time(c["at"]) >= t - LAG - 1})
    s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps({"t": t - LAG, "ids": recent})))
    if not ch:
        return ""
    lines = ["CANON CHANGES since your last turn (cite ids, don't restate values; canon(product) for the card):"]
    lines += [f"- {_line(c)}" for c in ch[-limit:]]
    if len(ch) > limit:
        lines.append(f"- and {len(ch) - limit} earlier ones: changes(since='{before(ch[0]['at'])}')")
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------- writing
def guard(s, agent_id, subject, predicate, evidence=None) -> str:
    """An agent writing Canon (fact_set, fact_invalidate): returns the predicate in its one form, and refuses a
    subject not in its one form (check_subject). Owner-only predicates are refused, whatever the agent is called: the
    owner's words arrive as record_decision(sets=...), the Pulse relay's included. When `evidence` is checked (not
    None) every other predicate needs some, but Pulse's transient this_week. agent_id None is the local owner (CLI)."""
    kind = kind_of(subject)
    if not kind:
        return predicate
    check_subject(subject)
    predicate = predicate_of(subject, predicate)
    if not agent_id:
        return predicate
    if f"{kind}.{predicate}" in owner_only(s):
        raise CommonsError(
            f"{subject}.{predicate} is owner-only (company.owner_only): only the owner changes it. Ask the owner, one "
            f"line for this item, then record their words: record_decision(what, why=<the owner's words>, "
            f"sets=[{{\"subject\": \"{subject}\", \"predicate\": \"{predicate}\", \"value\": ...}}], supersedes=[ids "
            "of notes it retires]). The Pulse relay records the owner's Pulse Studio edits the same way.")
    if evidence is not None and predicate not in TRANSIENT and not str(evidence).strip():
        raise CommonsError(f"{subject} is Canon: pass evidence (a commit, release, URL or item id showing that "
                           f"{predicate} is true now)")
    return predicate


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
    lose = ("b", "sb", "kb", "mb") if keep_id == r["a"] else ("a", "sa", "ka", "ma")
    if r[lose[2]] == "fact" and is_canon(r[lose[1]]):
        raise CommonsError(f"{r[lose[0]]} is a Canon fact: keep it, or correct Canon with fact_set(subject, predicate, "
                           "value, evidence=...), which closes the old value")
    if sets_canon({"kind": r[lose[2]], "meta": r[lose[3]]}):
        raise CommonsError(f"{r[lose[0]]} is the decision that set Canon facts, which cite it: keep it; a new "
                           "record_decision(sets=[...]) changes them")


def check_decision(s, sets, supersedes) -> list[dict]:
    """Validate record_decision's sets and supersedes before anything is written. A set's value must say something
    ([] does: nothing is listed); a Canon fact is never superseded by a decision: its sets correct it."""
    out = []
    for x in sets or []:
        if not isinstance(x, dict) or not str(x.get("subject") or "").strip() or \
                not str(x.get("predicate") or "").strip() or blank(x.get("value")):
            raise CommonsError('each sets item is {"subject": "product:santa", "predicate": "prices", "value": [...]}, '
                               'with a value that says something ([] for none)')
        sub = str(x["subject"]).strip()
        check_subject(sub)
        pred, value = predicate_of(sub, str(x["predicate"]).strip()), x["value"]
        if (sub, pred) == ("company", "owner_only"):  # tokens in one form, so each one protects what it names
            v = parse(value)
            value = [str(e).strip().casefold() for e in v] if isinstance(v, list) else None
            if value is None or not all(OO_ENTRY.fullmatch(e) for e in value):
                raise CommonsError('company.owner_only is a list of <kind>.<predicate>, like ["product.prices", '
                                   f'"work.status"], not {x["value"]!r}')
        out.append({"subject": sub, "predicate": pred, "value": value})
    for i in supersedes or []:
        r = s.q("SELECT project_id, kind, subject, meta FROM items WHERE id=?", (i,))
        if not r:
            raise CommonsError(f"supersedes: no item {i}")
        if r[0]["kind"] == "fact" and is_canon(r[0]["subject"]):
            raise CommonsError(f"supersedes: {i} is a Canon fact ({r[0]['subject']}); correct Canon with sets=[...]")
        if sets_canon(r[0]):  # why() explains Canon by it, for every agent
            raise CommonsError(f"supersedes: {i} is the decision that set Canon facts, which cite it: it stays as it "
                               "was recorded; this decision's own sets=[...] change them")
        s.check_project(r[0]["project_id"], "write")
        s._row_write(i)
    return out


def sets_canon(row) -> bool:
    """A decision whose sets wrote Canon facts: they cite it, and why() explains them by it."""
    return row["kind"] == "decision" and any(isinstance(x, dict) and is_canon(x.get("subject"))
                                             for x in _meta(row).get("sets") or [])


def pinned(row) -> bool:
    """A Canon fact, or the decision that set one (row with kind, subject, meta): never filed in a project, merged
    away or retired, so every agent's canon() and why() keep reading both."""
    return (row["kind"] == "fact" and is_canon(row["subject"])) or sets_canon(row)


def pinned_id(s, iid) -> bool:
    """Is item iid pinned, a Canon fact or the decision that set one (as it is now, not as some older suggestion saw
    it)?"""
    return any(pinned(r) for r in s.q("SELECT kind, subject, meta FROM items WHERE id=?", (iid,)))


def check_delete(row) -> None:
    """Canon keeps its history: no Canon fact, and no decision that set one, is ever deleted or rewritten in place (the
    forget tool, the dashboard, any caller); restrict() refuses them too."""
    if row["kind"] == "fact" and is_canon(row["subject"]):
        raise CommonsError("Canon facts keep their history: correct one with fact_set(..., evidence=...), or for an "
                           "owner-only fact record_decision(sets=[...])")
    if sets_canon(row):
        raise CommonsError("this decision set Canon facts, which cite it (why() explains them by it): it stays as it "
                           "was recorded. Record a new decision with sets=[...] to change them")


def _close(s, rows, status: str) -> None:
    """Close conflicts ({id, a, b} rows); an item stays flagged only while another conflict on it is open."""
    for c in rows:
        s.x("UPDATE conflicts SET status=? WHERE id=?", (status, c["id"]))
    for i in {x for c in rows for x in (c["a"], c["b"])}:
        if not s.q("SELECT 1 FROM conflicts WHERE status='open' AND (a=? OR b=?)", (i, i)):
            s.x("UPDATE items SET conflict=0 WHERE id=?", (i,))


def _settle(s, rows, agent_id, by) -> None:
    """Mark conflicts resolved by a decision or a corrected Canon fact (`by`)."""
    _close(s, rows, "resolved")
    if rows:
        s.audit(agent_id, "conflict.resolve", by, {"by": by, "conflicts": [c["id"] for c in rows]})


def settle_set(s, subject, predicate, closed: list[str], agent_id, by) -> None:
    """A new current value (Canon, or one a decision set) settles the open conflicts on its subject.predicate and on
    the values it closed."""
    ph = ",".join("?" * len(closed)) or "NULL"
    _settle(s, s.q(f"SELECT id, a, b FROM conflicts WHERE status='open' AND ((subject=? AND predicate=?) OR "
                   f"a IN ({ph}) OR b IN ({ph}))", [subject, predicate] + closed + closed), agent_id, by)


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
        if d["old"] is None or same(d["old"], d["value"]):
            continue
        new = show(d["value"]).lower()
        parts = [p for p in _parts(d["old"]) if len(p) >= 4 and p.lower() not in new]
        if not parts:
            continue
        hits = [h["id"] for h in related(s, f"{d['subject']} {d['predicate']} " + " ".join(parts), limit=20)]
        hits += s.rank(" ".join(parts), mode="keyword", kinds=["memory", "fact", "decision", "document", "handoff"],
                       limit=50)[0] or []
        for i in dict.fromkeys(hits):
            r = s.q("SELECT kind, subject, predicate, content, tags, meta FROM items WHERE id=? AND "
                    "superseded_by IS NULL", (i,))
            if not r or i in exclude or i in out or '"conversation"' in (r[0]["tags"] or "") or pinned(r[0]) or (
                    r[0]["kind"] == "fact" and r[0]["subject"] == d["subject"] and r[0]["predicate"] == d["predicate"]):
                continue  # Canon, and the decision that set it, are corrected by a newer value, never superseded
            if any(p.lower() in (r[0]["content"] or "").lower() for p in parts):
                out.append(i)
    return out


def apply_decision(s, decision: str, sets: list[dict], supersedes, agent_id, project) -> dict:
    """record_decision's sets and supersedes, as check_decision passed them: each set becomes the current fact (closing
    the old value, history kept; Facts.set settles the open conflicts on it); each superseded note is retired by the
    decision. related() then finds the notes that still state an old value."""
    from .facts import Facts
    f, done = Facts(s), []
    for x in sets:
        r = f.set(x["subject"], x["predicate"], x["value"], project=project, agent_id=agent_id, decision=decision)
        done.append({"id": r["id"], "subject": x["subject"], "predicate": x["predicate"], "value": parse(dump(x["value"])),
                     "old": parse(r.get("old"))})
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
    return {"set": done, "superseded": gone}


def related(s, decision: str, out: dict) -> list[str]:
    """Notes that still state a value the decision replaced, for the caller to supersede (run after the commit: it
    searches)."""
    done = out.get("set") or []
    return _stale(s, done, {decision, *(out.get("superseded") or []), *(d["id"] for d in done)})


def migrate(s) -> dict:
    """v0.38, once per database: Canon-subject facts join the one global timeline, in one form. A fact joins when it is
    unfiled or sits in a project Pulse wrote in (its project 'pulse', or one where the agent pulse wrote Canon facts)
    that every agent may read (no project ACL), is restricted to no named agents (restrict()), has a predicate with one
    form ('Prices' and 'prices ' are prices), and, for an owner-only predicate, was written by the owner (no agent) or
    Pulse: no other agent's v0.37 value becomes the owner's word. Any other Canon-subject fact (a client's 'company'
    facts in a restricted project, an agent's company.focus, a predicate with no one form like 'setup fee', or none)
    stays where it is as a note (kind memory), out of Canon, for the owner to decide, and its open conflicts with no
    Canon fact on either side are dismissed: one that pairs a Canon fact with a note stays open, and the owner's
    decision on that field settles it. Where two rows held a current value of one subject.predicate, the newest stays
    current and closes the others; two different values of an owner-only fact are the owner's to choose between, so
    each closed one gets an open conflict with the kept one. A Canon value closed by a row that is a note now, with no
    later Canon value of its field, ends at the migration (changes() shows old -> null, as canon() has no value), and on
    an owner-only field gets an open conflict with that note. One transaction that checks the once-flag again itself,
    so two processes opening the database at once migrate once. The audit row (canon.migrate) lists every moved id with
    its old project, every renamed predicate, every row left as a note, every dismissed conflict and every ended value
    with the note that had closed it, so each step can be undone; the log names the notes."""
    def when(r) -> float:
        return r["valid_from"] or r["created_at"] or 0.0
    if s.q("SELECT 1 FROM meta WHERE key='canon_global'"):  # every later start: no write lock to wait for
        return {}
    with s.tx():
        s._lock_audit_chain()  # Postgres: one migrator at a time (SQLite's BEGIN IMMEDIATE already is)
        if s.q("SELECT 1 FROM meta WHERE key='canon_global'"):  # another process migrated while this one waited
            return {}
        rows = [dict(r) for r in s.q("SELECT id, subject, predicate, value, project_id, agent_id, valid_from, "
                                     "valid_to, superseded_by, created_at, meta FROM items WHERE kind='fact' AND "
                                     "subject IS NOT NULL")]
        rows = [r for r in rows if is_canon(r["subject"])]
        acl = {r["project_id"] for r in s.q("SELECT DISTINCT project_id FROM project_acl")}
        hidden = {r["ref"] for r in s.q("SELECT DISTINCT ref FROM row_acl")}  # records restricted to named agents
        pulse = {r["id"] for r in s.q("SELECT id FROM agents WHERE lower(name)='pulse'")}
        pulses = {r["project_id"] for r in rows if r["agent_id"] in pulse and r["predicate"]} | \
            {s.find_project("pulse")}
        guarded = set(OWNER_ONLY) | set(owner_only(s))  # the list as any row said it: wider, never narrower
        moved, renamed, kept, closed, opened, ended, cur, fields = [], [], [], [], [], [], {}, {}
        for r in rows:
            p, pid = str(r["predicate"] or "").strip().casefold(), r["project_id"]
            theirs = f"{kind_of(r['subject'])}.{p}" in guarded and r["agent_id"] and r["agent_id"] not in pulse
            if not PREDICATE.fullmatch(p) or r["id"] in hidden or (pid and (pid in acl or pid not in pulses)) or theirs:
                kept.append([r["id"], pid])
                s.x("UPDATE items SET kind='memory' WHERE id=?", (r["id"],))
                continue
            if p != r["predicate"]:
                renamed.append([r["id"], r["predicate"]])
                s.x("UPDATE items SET predicate=? WHERE id=?", (p, r["id"]))
            if pid:
                moved.append([r["id"], pid])
                s.x("UPDATE items SET project_id=NULL WHERE id=?", (r["id"],))
                s.x("DELETE FROM relations WHERE src=? AND type='in_project'", (r["id"],))
            fields.setdefault((r["subject"], p), []).append(r)
            if r["superseded_by"] is None and r["valid_to"] is None:
                cur.setdefault((r["subject"], p), []).append(r)
        notes = {i for i, _ in kept}
        left = {r["id"] for group in fields.values() for r in group}  # the Canon facts the migration leaves
        # a note takes its open conflicts with it, unless a Canon fact is on the other side: the owner's decision on
        # that field settles those
        drop = [c for c in s.q("SELECT id, a, b FROM conflicts WHERE status='open'")
                if {c["a"], c["b"]} & notes and not {c["a"], c["b"]} & left]
        _close(s, drop, "dismissed")
        oo = set(owner_only(s))  # as the rows say it now
        t = now()
        for (sub, pred), group in fields.items():  # a value closed by a row that is a note now, with no later Canon
            for r in group:                          # value of its field: it ends, so changes() agrees with canon()
                n = r["superseded_by"]
                if n not in notes or any(when(o) > when(r) for o in group):
                    continue
                m = _meta(r) | {"ended": {"at": t, "by": None, "evidence": f"successor {n} kept as a note"}}
                s.x("UPDATE items SET superseded_by='invalidated', valid_to=COALESCE(valid_to, ?), updated_at=?, "
                    "meta=? WHERE id=?", (t, t, json.dumps(m, ensure_ascii=False), r["id"]))
                ended.append([r["id"], n])
                if f"{kind_of(sub)}.{pred}" in oo:  # the owner's field has no value now: theirs to set again
                    opened.append(new_id("cfl"))
                    s.x("INSERT INTO conflicts VALUES(?,?,?,?,?,?,?)", (opened[-1], r["id"], n, sub, pred, "open", t))
                    s.x("UPDATE items SET conflict=1 WHERE id IN (?,?)", (r["id"], n))
        for (sub, pred), group in cur.items():
            group.sort(key=when)
            keep = group[-1]
            at = when(keep) or now()
            for o in group[:-1]:
                s.x("UPDATE items SET valid_to=?, superseded_by=? WHERE id=?", (at, keep["id"], o["id"]))
                s.relate(keep["id"], o["id"], "supersedes", {"at": at})
                closed.append(o["id"])
                if f"{kind_of(sub)}.{pred}" in oo and not same(o["value"], keep["value"]):
                    opened.append(new_id("cfl"))
                    s.x("INSERT INTO conflicts VALUES(?,?,?,?,?,?,?)", (opened[-1], keep["id"], o["id"], sub, pred,
                                                                       "open", now()))
                    s.x("UPDATE items SET conflict=1 WHERE id IN (?,?)", (keep["id"], o["id"]))
        s.x("INSERT OR REPLACE INTO meta VALUES('canon_global', ?)", (str(now()),))
        out = {"moved": moved, "renamed": renamed, "kept": kept, "closed": closed, "conflicts": opened,
               "dismissed": [c["id"] for c in drop], "ended": ended}
        if any(out.values()):
            s.audit(None, "canon.migrate", "", out)
    for pid in {p for _, p in moved + kept if p}:
        s.kb_dirty(pid)
    log = logging.getLogger("commons.canon")
    if moved or renamed or closed or ended:
        log.warning("Canon migration: %d facts moved onto the global timeline, %d predicates put in one form, %d "
                    "older values closed, %d values ended (a row kept as a note had closed them), %d conflicts opened "
                    "for the owner (audit canon.migrate lists them)", len(moved), len(renamed), len(closed),
                    len(ended), len(opened))
    if kept:
        what = {r["id"]: f"{r['subject']}.{r['predicate'] or ''}" for r in rows}
        log.warning("Canon migration: %d Canon-subject facts stay where they were, as notes and out of Canon (in a "
                    "project with an ACL or one Pulse never wrote in, restricted to named agents, with no one-form "
                    "predicate, or owner-only and written by neither the owner nor Pulse), and %d open conflicts on "
                    "them are dismissed; the owner keeps, deletes or records each into Canon with "
                    "record_decision(sets=...): %s", len(kept), len(drop),
                    "; ".join(f"{i} {what[i]} ({s.project_name(p) or 'no project'})" for i, p in kept[:50]) +
                    (f"; {len(kept) - 50} more in the audit row" if len(kept) > 50 else ""))
    return out
