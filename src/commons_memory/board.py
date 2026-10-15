"""Project board: what the user asked for, the targets and expectations behind it, the actions, and what was
delivered, per project and layer (`path`, e.g. "ui/dashboard"). It lives in the hub, not in an agent's context, so
it survives compaction and agent switches, and every agent sees the open items first in pickup().

Rules that keep agents honest:
- an ask is the user's words verbatim and is never edited (amend with a new ask whose parent is the old one);
- re-recording an open ask doesn't duplicate it: it bumps `repeats`, the signal that the user had to say it twice;
- `done` needs evidence (a commit, file, benchmark, item id, URL) and `dropped` needs a reason;
- every change is audited.
"""
from __future__ import annotations

import re

from .ids import iso, new_id, now
from .store import CommonsError, Store

KINDS = ("ask", "target", "expectation", "action", "delivered", "question")
STATUSES = ("open", "doing", "blocked", "done", "dropped")
SCHEMA = """
CREATE TABLE IF NOT EXISTS board(
  id TEXT PRIMARY KEY, project_id TEXT, path TEXT DEFAULT '', kind TEXT, text TEXT, parent TEXT,
  metric TEXT, goal TEXT, current TEXT, status TEXT DEFAULT 'open', evidence TEXT, source TEXT,
  repeats INTEGER DEFAULT 0, agent_id TEXT, created_at REAL, updated_at REAL, closed_at REAL);
CREATE INDEX IF NOT EXISTS board_proj ON board(project_id, status);
"""
OPEN = ("open", "doing", "blocked")


def migrate(db) -> None:
    db.executescript(SCHEMA)


def _words(t: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", (t or "").lower()))


def add(s: Store, kind: str, text: str, *, project: str | None = None, path: str = "", parent: str | None = None,
        metric: str | None = None, goal: str | None = None, current: str | None = None, source: str | None = None,
        status: str = "open", evidence: str | None = None, agent_id: str | None = None) -> dict:
    if kind not in KINDS:
        raise CommonsError(f"kind must be one of {', '.join(KINDS)}")
    if not (text or "").strip():
        raise CommonsError("board_add needs `text` (for an ask: the user's words, verbatim)")
    if status not in STATUSES:
        raise CommonsError(f"status must be one of {', '.join(STATUSES)}")
    pid = s.project_id(project) if project else None
    path = (path or "").strip("/ ")
    if kind == "ask":  # the same open ask again: count it instead of adding a twin
        w = _words(text)
        for r in s.q("SELECT id, text FROM board WHERE kind='ask' AND project_id IS ? AND status IN ('open','doing','blocked')",
                     (pid,)):
            o = _words(r["text"])
            if w and o and len(w & o) / len(w | o) >= 0.6:
                s.x("UPDATE board SET repeats=repeats+1, updated_at=? WHERE id=?", (now(), r["id"]))
                s.audit(agent_id, "board.repeat", r["id"], {"text": text[:300]})
                return get(s, r["id"]) | {"repeated": True,
                                          "hint": "the user already asked this and it is still open: do it now"}
    if parent and not s.q("SELECT 1 FROM board WHERE id=?", (parent,)):
        raise CommonsError(f"unknown parent {parent}")
    if status == "done" and not (evidence or "").strip():
        raise CommonsError("an item can only start as done with evidence")
    if kind == "question":  # an agent needs the user: it blocks until answered and leads the Board's "Needs you"
        status = "blocked"
    bid, t = new_id("brd"), now()
    s.x("INSERT INTO board(id,project_id,path,kind,text,parent,metric,goal,current,status,evidence,source,repeats,agent_id,"
        "created_at,updated_at,closed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)",
        (bid, pid, path, kind, text.strip(), parent, metric, goal, current, status, evidence,
         source or ("user" if kind == "ask" else s.label(agent_id)[1] if agent_id else None), agent_id, t, t,
         t if status in ("done", "dropped") else None))
    s.audit(agent_id, "board.add", bid, {"kind": kind, "path": path})
    notify_mentions(s, bid, text, agent_id)
    if kind == "question":  # reach the user wherever they are, with one-tap answers
        try:
            if agent_id:
                from .status import put as status_put
                status_put(s, agent_id, "needs_you", text)
            from .phone import push_question
            push_question(s, get(s, bid))
        except Exception:  # noqa: BLE001
            pass
    return get(s, bid)


def update(s: Store, item_id: str, *, status: str | None = None, current: str | None = None, evidence: str | None = None,
           goal: str | None = None, path: str | None = None, text: str | None = None, agent_id: str | None = None) -> dict:
    r = s.q("SELECT * FROM board WHERE id=?", (item_id,))
    if not r:
        raise CommonsError(f"unknown board item {item_id}")
    r = dict(r[0])
    if text is not None and r["kind"] == "ask":
        raise CommonsError("asks are the user's words and are never edited; add a new ask with parent=" + item_id)
    if status and status not in STATUSES:
        raise CommonsError(f"status must be one of {', '.join(STATUSES)}")
    ev = evidence if evidence is not None else r["evidence"]
    if status == "done" and not (ev or "").strip():
        raise CommonsError("done needs evidence: what proves it (commit, file, benchmark, item id, URL)")
    if status == "dropped" and not (evidence or "").strip():
        raise CommonsError("dropped needs a reason in `evidence`")
    if status == "done" and r["kind"] == "ask":
        kids = s.q("SELECT id, text FROM board WHERE parent=? AND status IN ('open','doing','blocked')", (item_id,))
        if kids:
            raise CommonsError(f"{len(kids)} item(s) under this ask are still open (first: {kids[0]['id']} "
                               f"{kids[0]['text'][:60]}); finish or drop them first")
    sets = {k: v for k, v in (("status", status), ("current", current), ("evidence", evidence), ("goal", goal),
                              ("path", (path or "").strip("/ ") if path is not None else None), ("text", text)) if v is not None}
    if not sets:
        return get(s, item_id)
    if status in ("done", "dropped"):
        sets["closed_at"] = now()
    elif status:
        sets["closed_at"] = None
    sets["updated_at"] = now()
    s.x(f"UPDATE board SET {', '.join(f'{k}=?' for k in sets)} WHERE id=?", (*sets.values(), item_id))
    s.audit(agent_id, "board.update", item_id, {k: v for k, v in sets.items() if k not in ("updated_at", "closed_at")})
    new_text = " ".join(x for x in (text, current, evidence) if x)
    if new_text:
        old = " ".join(x or "" for x in (r.get("text"), r.get("current"), r.get("evidence")))
        notify_mentions(s, item_id, new_text, agent_id, already=set(MENTION.findall(old)))
    return get(s, item_id)


def answer(s: Store, item_id: str, text: str, by: str = "user") -> dict:
    """The user answers an agent's question: the answer goes to the asking agent's inbox and the item closes."""
    r = s.q("SELECT * FROM board WHERE id=?", (item_id,))
    if not r or r[0]["kind"] != "question":
        raise CommonsError(f"{item_id} is not a question on the board")
    if not (text or "").strip():
        raise CommonsError("write the answer")
    r = dict(r[0])
    if r["status"] == "done":  # answered already, or replaced by a newer question: an old answer must not count
        raise CommonsError(f"{item_id} is already closed")
    s.x("UPDATE board SET status='done', evidence=?, updated_at=?, closed_at=? WHERE id=?",
        (f"answer from {by}: {text.strip()}"[:4000], now(), now(), item_id))
    if r["agent_id"]:
        from .messaging import Messaging
        Messaging(s).send(None, r["agent_id"], f"Answer to your question {item_id} ({r['text'][:120]}):\n{text.strip()}",
                          mtype="message", subject="answer: " + r["text"][:60], meta={"board": item_id}, by="the user's answer on the board")
    from .vault import answered
    answered(s, r["text"], text)  # a credential request the decision agent passed to the user
    from .mcphub import answered as mcp_answered
    mcp_answered(s, r["text"], text)  # an upstream MCP tool call waiting for approval
    if re.match(r"\s*\[(gate|rework) sub_", r["text"]):  # a workflow step only the owner can pass
        from .orchestrator import Orchestrator
        Orchestrator(s).owner_answered(r["text"], text, item_id)
    s.audit(None, "board.answer", item_id, {"by": by})
    return get(s, item_id)


def needs_user(s: Store, project: str | None = None) -> list[dict]:
    """Open questions for the user, each with what it is holding up (open items under the same ask)."""
    where, args = "kind='question' AND status IN ('open','doing','blocked')", []
    if project:
        where += " AND project_id IS ?"
        args.append(s.project_id(project))
    out = []
    for r in s.q(f"SELECT id FROM board WHERE {where} ORDER BY created_at", args):
        d = get(s, r["id"])
        held = s.q("SELECT COUNT(*) n FROM board WHERE parent=? AND id<>? AND status IN ('open','doing','blocked')",
                   (d["parent"], d["id"])) if d.get("parent") else [{"n": 0}]
        d["holding_up"] = held[0]["n"]
        d["blocks"] = get(s, d["parent"])["text"][:160] if d.get("parent") else None
        d["choices"] = choices(d["text"])
        out.append(d)
    return out


YESNO = re.compile(r"(?i)(should|shall|can|could|may|might|do|does|did|is|are|was|were|will|would|have|has|am)\b")


def choices(text: str) -> list[str]:
    """Up to 4 one-tap answers read from a question's wording: "X or Y?" -> [X, Y], "A, B, or C?" -> [A, B, C], a
    yes/no question -> ["Yes", "No"], anything less clear -> []. Approval questions have their own page: none."""
    t = (text or "").strip()
    if t.startswith(("[vault ", "[mcp ", "[gate ", "[rework ")):
        return []
    qs = [p for p in re.split(r"(?<=[.!?])\s+|\n", t) if "?" in p]
    if not qs:
        return []
    q = qs[-1][:qs[-1].rindex("?")].rsplit(":", 1)[-1].strip()  # the last question; "Which one: A or B?" -> "A or B"
    parts = [p.strip(" '\"`;") for p in re.split(r",\s+(?:or\s+)?|\s+or\s+", q, flags=re.I)]
    if not re.search(r"(?i)\bor\b", q):
        return ["Yes", "No"] if YESNO.match(q) or YESNO.match(q.rsplit(", ", 1)[-1]) else []
    if parts[-1].lower() in ("not", "no"):  # "Should we ship it or not?"
        return ["Yes", "No"]
    words = lambda p: len(p.split())  # noqa: E731
    opts = [parts[-1]]
    for p in reversed(parts[:-1]):  # list items are about as long as the last one; a longer piece holds the first
        if words(p) <= words(parts[-1]) + 1:  # option behind its question words ("Should we use React")
            opts.insert(0, p)
            continue
        opts.insert(0, " ".join(p.split()[-max(map(words, opts)):]))
        break
    opts = [re.sub(r"(?i)^to\s+", "", o) for o in opts]
    if not 2 <= len(opts) <= 4 or any(not o or words(o) > 6 or len(o) > 40 for o in opts) \
            or any(YESNO.match(o) for o in opts[1:]):  # "deploy now or should I wait?" reads both ways
        return []
    return [o[0].upper() + o[1:] for o in opts]

MENTION = re.compile(r"(?<![\w.])@([A-Za-z0-9][A-Za-z0-9_.-]{0,63})")


def notify_mentions(s: Store, item_id: str, text: str, agent_id: str | None = None, already: set | None = None) -> list[str]:
    """Wake every registered agent @mentioned in a board item (once per new mention; never the author)."""
    from .messaging import emit
    woke = []
    for name in dict.fromkeys(MENTION.findall(text or "")):
        name = name.rstrip(".-")
        if already and name in already:
            continue
        a = s.get_agent(name)
        if not a or a["id"] == agent_id or a["name"] in ("user", "cli"):
            continue
        emit(s, a["id"], "board_mention", ref=item_id, preview=text, sender=s.label(agent_id)[1] if agent_id else None)
        woke.append(a["name"])
    return woke


def get(s: Store, item_id: str) -> dict:
    r = s.q("SELECT * FROM board WHERE id=?", (item_id,))
    if not r:
        raise CommonsError(f"unknown board item {item_id}")
    d = dict(r[0])
    d["project"] = s.project_name(d.pop("project_id")) if d.get("project_id") else None
    for k in ("created_at", "updated_at", "closed_at"):
        d[k] = iso(d[k]) if d[k] else None
    return d


def view(s: Store, project: str | None = None, path: str = "", status: str = "open", limit: int = 200,
         max_tokens: int = 1500) -> dict:
    """Layer-by-layer board: asks with their targets/actions/deliveries nested under them. status: open (open, doing,
    blocked) | done | dropped | all."""
    where, args = ["1=1"], []
    if project:
        where.append("project_id IS ?")
        args.append(s.project_id(project))
    if path:
        where.append("(path=? OR path LIKE ?)")
        args += [path.strip("/"), path.strip("/") + "/%"]
    rows = [dict(r) for r in s.q(f"SELECT * FROM board WHERE {' AND '.join(where)} ORDER BY project_id, path, created_at "
                                 f"LIMIT ?", (*args, limit * 3))]
    gone = s.hidden_ids([r["id"] for r in rows])  # board items restricted to other agents
    rows = [r for r in rows if r["id"] not in gone]
    keep = (lambda r: r["status"] in OPEN) if status == "open" else (lambda r: True) if status == "all" else \
        (lambda r: r["status"] == status)
    byid = {r["id"]: r for r in rows}
    kids: dict[str | None, list[dict]] = {}
    for r in rows:
        kids.setdefault(r["parent"] if r["parent"] in byid else None, []).append(r)
    mark = {"open": "[ ]", "doing": "[~]", "blocked": "[!]", "done": "[x]", "dropped": "[-]"}
    lines, shown, counts = [], [], {}
    if any(r["kind"] == "question" and r["status"] in OPEN for r in rows):
        lines.append("NEEDS THE USER: questions marked [?] block their work until answered")
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    def line(r, depth):
        m = "[?]" if r["kind"] == "question" and r["status"] in OPEN else mark.get(r["status"], "[ ]")
        bits = [f"{'  ' * depth}{m} {r['id']} {r['kind']}"]
        if r["kind"] == "target" and (r["metric"] or r["goal"]):
            bits.append(f"{r['metric'] or ''} goal {r['goal'] or '?'} now {r['current'] or '?'}:")
        bits.append(r["text"][:240])
        if r["repeats"]:
            bits.append(f"(asked {r['repeats'] + 1}x)")
        if r["status"] in ("done", "dropped") and r["evidence"]:
            bits.append(f"-> {r['evidence'][:120]}")
        return " ".join(bits)

    memo: dict[str, bool] = {}

    def live(r):  # shown when it, or anything under it, matches the filter
        if r["id"] not in memo:
            memo[r["id"]] = keep(r) or any(live(k) for k in kids.get(r["id"], []))
        return memo[r["id"]]

    def walk(parent, depth):
        for r in kids.get(parent, []):
            if live(r):
                lines.append(line(r, depth))
                shown.append(r["id"])
                walk(r["id"], depth + 1)
    last = object()
    for r in kids.get(None, []):
        head = (s.project_name(r["project_id"]) if r["project_id"] else "-") + (f" / {r['path']}" if r["path"] else "")
        if not live(r):
            continue
        if head != last:
            lines.append(f"## {head}")
            last = head
        lines.append(line(r, 0))
        shown.append(r["id"])
        walk(r["id"], 1)
    text = "\n".join(lines) or "Board is empty for this filter."
    from .ids import estimate_tokens
    while estimate_tokens(text) > max_tokens and "\n" in text:
        text = text.rsplit("\n", 1)[0]
    return {"text": text, "ids": shown, "counts": counts}
