"""Canon on this machine (v0.38): the hook client's state cache, so a session starts with the current truth even when
the hub is slow or down.

A detached pull (`commons canon pull`, started by the hooks at session start and at most every 5 minutes on prompts)
writes ~/.commons/state/canon.json (the hub's canon() plus fetched_at, ok and error: a failed pull keeps the old data
and records ok=false) and canon.md (the CANON header with the precedence, the company core, one short card per
product). Hooks read them with no hub call: a session start opens with the core, the card of the product whose
work:<key>.repos holds this checkout's origin, the last 7 days of changes to every product and the company, the open
conflict count and how far the checkout is behind origin; each prompt adds one line per change this session has not
been shown (state/sessions/<session_id>.json) and, once per session, the card of a product it names. A change is never
counted as seen before its line was shown, and Canon's part of a hook's context stays under CANON_MAX characters. Peers
and guards read the same files. Copies are one-way: nothing here writes to Commons.
"""
from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .canon import PRECEDENCE, _clip, _line, _list, _slug, before, card, kind_of, one_liner, show
from .ids import iso

STALE_S = 6 * 3600         # a cache older than this is called out at session start, and the hub keeps its own FACTS
BLOCK_STALE_S = 3 * 86400  # the instruction-file block goes once the cache is older than this
PULL_PROMPT_S = 300        # prompts start a pull at most this often
PULL_START_S = 60          # session starts within a minute of the last pull share it
WEEK_S = 7 * 86400
CHANGES_MAX = 500          # the cache keeps the newest changes of the last 7 days, at most these (each prompt reads them)
LAG_MS = 30_000            # a change that commits after a pull read Canon can be stamped up to this much earlier
MAX_BYTES = 2 * 2 ** 20    # a bigger canon.json is corrupt: the next pull replaces it
CANON_MAX = 6000           # Canon's part of a hook's context: the hub's warnings, recall and claim id keep the rest
SCAN = 16384               # a prompt is searched for product names in its first 16 KB
FIXED = ("COMPANY CORE", "- precedence: ", "- owner-only")  # the core's lines that exist with no Canon fact at all
NAMES = ("name", "names", "alias", "aliases")              # predicates that name a product (and name_*, e.g. name_zh)
# keys that are everyday words: a prompt names them only through a name fact
# ponytail: a fixed list of everyday-word keys; a dictionary check if product keys outgrow it
COMMON = ("secret", "journal", "poise", "santa", "cooli")
HUB = "canon(product) on the hub has the current truth"


def home() -> Path:
    return Path(os.environ.get("COMMONS_HOME", Path.home() / ".commons")) / "state"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)  # readers never see half a file


def read() -> dict | None:
    p = home() / "canon.json"
    try:
        if p.stat().st_size > MAX_BYTES:
            return None  # corrupt, and too big to parse on every prompt
        c = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return c if isinstance(c, dict) else None


@functools.lru_cache(maxsize=8192)  # the same few hundred times are read several times on every prompt
def _ms(at) -> int:
    from .store import _parse_time
    return int(_parse_time(str(at)) * 1000)


# ---------------------------------------------------------------------------------------------- pull
def _check(c) -> None:
    """A canon() reply the hooks can read, else ValueError: a core, products, changes with times, generated_at."""
    if not isinstance(c, dict) or "core" not in c:
        raise ValueError(f"not a canon() reply: {str(c)[:120]}")
    if not isinstance(c.get("products") or {}, dict) or not isinstance(c.get("changes") or [], list):
        raise ValueError("canon() reply: products is not an object or changes not a list")
    for x in c.get("changes") or []:
        if not isinstance(x, dict):
            raise ValueError("canon() reply: a change is not an object")
        _ms(x["at"])
    _ms(c["generated_at"])


def pull(store=None) -> dict:
    """canon() from the hub at $COMMONS_URL (else from `store`, the local database) into canon.json and canon.md, with
    the changes of the last 7 days, at most CHANGES_MAX (changes_cut counts the older ones left out). A failure keeps
    the old cache and records ok=false with the error (the key never in it). A hub older than Canon (Unknown tool) is
    a quiet state, old_hub: nothing to warn about at every session start."""
    url, key = os.environ.get("COMMONS_URL", "").rstrip("/"), ""
    try:
        if url:
            from .capture import _key
            from .worker import McpClient
            key = _key()
            c = json.loads(McpClient(url.removesuffix("/mcp") + "/mcp", key)._call("canon", {}))
        else:
            from .canon import canon
            from .store import Store
            c = canon(store if store is not None else Store())
        _check(c)
        week = [x for x in c.get("changes") or [] if _ms(x["at"]) >= (time.time() - WEEK_S) * 1000]
        c |= {"changes": week[-CHANGES_MAX:], "changes_cut": max(0, len(week) - CHANGES_MAX),
              "fetched_at": iso(time.time()), "ok": True, "error": ""}
        _write(home() / "canon.md", render(c))
    except Exception as e:  # noqa: BLE001 - hub down, too old for canon(), or refusing: keep what we had, say why
        while getattr(e, "exceptions", None):  # the MCP client wraps the real error in exception groups
            e = e.exceptions[0]
        err = f"{type(e).__name__}: {e}"
        err = " ".join((err.replace(key, "[key]") if key else err).split())[:300]  # redacted before it is cut
        if "unknown tool" in err.lower():
            c = {"fetched_at": None, "ok": False, "old_hub": True,
                 "error": "the hub is older than v0.38 and has no Canon yet: upgrade the hub"}
        else:
            c = {k: v for k, v in (read() or {"fetched_at": None}).items() if k != "old_hub"}
            c |= {"ok": False, "error": err}
    _write(home() / "canon.json", json.dumps(c, ensure_ascii=False))
    cut = time.time() - 14 * 86400  # session watermarks of sessions long over
    for p in (home() / "sessions").glob("*.json"):
        try:
            if p.stat().st_mtime < cut:
                p.unlink()
        except OSError:
            pass
    return c


def due(every: float) -> bool:
    """True, and marked as started, when no pull started in the last `every` seconds."""
    stamp = home() / "canon.pull"
    try:
        if time.time() - stamp.stat().st_mtime < every:
            return False
    except OSError:
        pass
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()
    return True


def spawn() -> None:
    """`commons canon pull`, detached: it outlives the hook, holds none of its output, and gives up after 30 s. It runs
    from the home folder, with Python's safe path (-P, 3.11+): never from the session's project, where a module named
    like one it imports would run with the agent key in its environment."""
    kw = {"creationflags": 0x208} if os.name == "nt" else {"start_new_session": True}  # DETACHED | NEW_PROCESS_GROUP
    py = [sys.executable] + (["-P"] if sys.version_info >= (3, 11) else [])
    subprocess.Popen(py + ["-m", "commons_memory", "canon", "pull"], cwd=os.path.expanduser("~"),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                     env={**os.environ, "COMMONS_CALL_TIMEOUT": "30"}, **kw)


# ---------------------------------------------------------------------------------------------- rendering
def has_canon(c: dict | None) -> bool:
    """Any Canon fact? (The hub's core states the precedence and the owner-only list even with none.)"""
    return bool(c) and bool(c.get("products") or c.get("vendors") or any(
        not x.startswith(FIXED) for x in str(c.get("core") or "").splitlines()))


def core(c: dict) -> str:
    """The CANON header carrying the precedence, then the company core without its own precedence line: <= 20 lines,
    each clipped to 240 characters (the header and the owner-only list, which only the owner sets, to 600)."""
    lines = str(c.get("core") or "").splitlines()
    prec = next((x[len(FIXED[1]):] for x in lines if x.startswith(FIXED[1])), PRECEDENCE)
    out = [f"CANON (current truth; precedence: {prec})"] + [x for x in lines if not x.startswith(FIXED[1])]
    return "\n".join(_clip(x, 600 if n == 0 or x.startswith(FIXED) else 240) for n, x in enumerate(out[:20]))


def short_card(key: str, c: dict) -> str:
    lines = card(key, (c.get("products") or {}).get(key)).splitlines()
    return "\n".join([_clip(x, 240) for x in lines[:15]] + (["- ... canon(product) has the rest"] if len(lines) > 15 else []))


def render(c: dict) -> str:
    """canon.md: the core, then one short card per product."""
    return "\n\n".join([core(c)] + [short_card(k, c) for k in sorted(c.get("products") or {})]) + "\n"


def block(c: dict | None) -> str | None:
    """The block for the always-loaded instruction files: only what the owner controls (the precedence, the owner-only
    list, and the focus and standing rules while they are owner-only), dated by the pull, and where the rest is. None
    (the block goes) with no Canon, no list from the hub, or a cache more than 3 days old."""
    if not has_canon(c) or not isinstance(c.get("owner_only"), list):
        return None
    try:
        if time.time() - _ms(c["fetched_at"]) / 1000 > BLOCK_STALE_S:
            return None
    except (KeyError, ValueError, TypeError):
        return None
    co, oo = c.get("company") if isinstance(c.get("company"), dict) else {}, [str(x) for x in c["owner_only"]]
    lines = [f"CANON CORE (set by the owner; as of {str(c['fetched_at'])[:10]}): precedence: "
             f"{show(co.get('precedence') or PRECEDENCE)}",
             f"- owner-only, changed only by record_decision(sets=...) quoting the owner: {', '.join(oo)}"]
    if "company.focus" in oo and not (co.get("focus") in (None, "") or co.get("focus") == []):
        lines.append(f"- focus: {show(co['focus'])}")
    rules = co.get("standing_rules") if "company.standing_rules" in oo else None
    if isinstance(rules, list) and rules:
        lines += ["- standing rules:"] + [f"  - {show(x)}" for x in rules]
    elif rules:
        lines.append(f"- standing rules: {show(rules)}")
    lines = lines[:19] + ["- everything else (products, agents, vendors, changes): canon() on the Commons hub, or "
                          "~/.commons/state/canon.md"]
    return "\n".join(_clip(x, 600 if n < 2 else 240) for n, x in enumerate(lines))


def _subject_key(x: dict) -> str | None:
    sub = str(x.get("subject") or "")
    return sub.split(":", 1)[1] if kind_of(sub) in ("product", "work") else None


def _changes(chs: list[dict], title: str, limit: int, first=(), cut: int = 0) -> tuple[str, list[dict]]:
    """(text, the changes it shows): the newest `limit` changes, the products in `first` leading, then how many
    earlier ones there are (with `cut`, those the cache left out) and a since= that lists them. Lines are clipped, so
    the text stays under ~3.5k."""
    if not chs:
        return "", []
    shown, rest = chs[-limit:], chs[:-limit]
    order = sorted(shown, key=lambda x: (_subject_key(x) not in first, _ms(x["at"])))
    out = [f"{title} (cite ids, never restate values; canon(product) has the card):"]
    out += [_clip(f"- {_line(x)}", 320) for x in order]
    if rest or cut:
        since = "7d" if cut else before(rest[0]["at"])
        out.append(f"- and {len(rest) + cut} earlier: changes(since='{since}')"
                   + ("; the next message shows them" if rest and not cut else ""))
    return "\n".join(out), shown


def _age(s: float) -> str:
    return f"{int(s // 60)} min" if s < 3600 else f"{int(s // 3600)} h" if s < 2 * 86400 else f"{int(s // 86400)} days"


def status(c: dict | None) -> str:
    """'' when the cache is fresh and its last pull worked (or the hub simply predates Canon); else one plain line
    saying what is wrong."""
    if c and c.get("old_hub"):
        return ""
    if not c:
        return f"Commons Canon cache is missing (~/.commons/state/canon.json): a pull has started; until it lands, {HUB}."
    if c.get("ok") is False:
        have = f"this is Canon as of {c['fetched_at']}" if c.get("fetched_at") else "there is no Canon here yet"
        return f"Commons Canon cache: the last pull failed ({_clip(str(c.get('error') or '?'), 160)}), so {have}; {HUB}."
    try:
        age = time.time() - _ms(c["fetched_at"]) / 1000
    except (KeyError, ValueError, TypeError):
        age = None
    if age is not None and age <= STALE_S:
        return ""
    return f"Commons Canon cache is {f'{_age(age)} old' if age is not None else 'undated'}: a refresh has started; {HUB}."


def fresh(c: dict | None) -> bool:
    """The cache can stand in for the hub's FACTS: it has a core, its last pull worked, and it is under 6 hours old."""
    return bool(c and c.get("core")) and c.get("ok") is True and status(c) == ""


# ---------------------------------------------------------------------------------------------- sessions
def _sfile(sid: str) -> Path:
    return home() / "sessions" / (re.sub(r"[^A-Za-z0-9._-]", "_", sid or "default")[:120] + ".json")


def session(sid: str) -> dict:
    try:
        d = json.loads(_sfile(sid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def save(sid: str, st: dict) -> None:
    _write(_sfile(sid), json.dumps(st, ensure_ascii=False))


def _fp(x: dict) -> str:
    return f"{x.get('at')}|{x.get('subject')}|{x.get('predicate')}|{json.dumps(x.get('new'), sort_keys=True)}"


def _all(c: dict) -> list[dict]:
    chs = [x for x in c.get("changes") or [] if isinstance(x, dict) and x.get("at")]
    return sorted(chs, key=lambda x: _ms(x["at"]))


def unseen(c: dict, st: dict) -> list[dict]:
    """The cache's changes this session has not been shown, oldest first: at or after the watermark (seen_until, epoch
    ms) and not among those shown since it (seen)."""
    floor, seen = int(st.get("seen_until") or 0), set(st.get("seen") or [])
    return [x for x in _all(c) if _ms(x["at"]) >= floor and _fp(x) not in seen]


def mark(c: dict, st: dict, shown: list[dict]) -> None:
    """Count `shown` as seen. The watermark moves to the oldest change still not shown, else to the pull's time less
    LAG_MS (a change that commits late can carry an earlier time); the changes shown after it are kept by fingerprint,
    so none is shown twice and none is skipped."""
    seen = set(st.get("seen") or []) | {_fp(x) for x in shown}
    left = [_ms(x["at"]) for x in unseen(c, st) if _fp(x) not in seen]
    chs = _all(c)
    top = _ms(c["generated_at"]) if c.get("generated_at") else max([_ms(x["at"]) for x in chs], default=0)
    floor = max(int(st.get("seen_until") or 0), min([top - LAG_MS] + left))
    st["seen_until"] = floor
    st["seen"] = sorted(f for f in seen if _ms(f.split("|", 1)[0]) >= floor)


def repo_keys(c: dict, remote: str) -> list[str]:
    """Products whose work:<key>.repos holds this git remote (https or ssh URL, or owner/name)."""
    want = _slug(remote) if remote else ""
    return [k for k, e in sorted((c.get("products") or {}).items())
            if want and want in {_slug(u) for u in _list(((e or {}).get("work") or {}).get("repos"))}]


def names(key: str, entry: dict | None) -> list[str]:
    """What a prompt may call a product, lowercase: its key (unless the key is an everyday word, COMMON) and its
    name/names/alias/aliases/name_* facts."""
    out = [] if key.lower() in COMMON else [key]
    for side in ((entry or {}).get("product") or {}, (entry or {}).get("work") or {}):
        for p, v in side.items():
            if p in NAMES or p.startswith("name_"):
                out += [x.strip() for x in (v.values() if isinstance(v, dict) else _list(v))
                        if isinstance(x, str) and x.strip()]
    return list(dict.fromkeys(n.lower() for n in out))


def mentioned(c: dict, text: str) -> list[str]:
    """Product keys a prompt names, in order, from its first 16 KB. ASCII names match whole words ('_' and '-' are part
    of a word); others (Chinese) match anywhere, since those scripts put no spaces between words. Each name is looked
    for once, each key keeps its longest hit, and a key whose hit sits inside another product's longer one ("santa"
    in "secret santa hotline") counts only for that one."""
    t, hits = " ".join((text or "")[:SCAN].split()).lower(), {}
    for k, e in (c.get("products") or {}).items():
        for n in names(k, e):
            if len(n) < (4 if n.isascii() else 2) or n not in t:
                continue
            m = re.search(r"(?<![a-z0-9_-])" + re.escape(n) + r"(?![a-z0-9_-])", t) if n.isascii() else \
                re.search(re.escape(n), t)
            if m and (k not in hits or m.end() - m.start() > hits[k][1] - hits[k][0]):
                hits[k] = m.span()
    keep = [(s, k) for k, (s, e) in hits.items()
            if not any(k2 != k and s2 <= s and e <= e2 and e2 - s2 > e - s for k2, (s2, e2) in hits.items())]
    return [k for _, k in sorted(keep)]


# ---------------------------------------------------------------------------------------------- git
def _git(cwd: str | None, *a: str, until: float | None = None) -> str:
    """One git call, bounded by 2 s and by `until` (time.monotonic()): none starts once that has passed."""
    if not cwd or os.environ.get("COMMONS_HOOK_GIT") == "0":
        return ""
    left = 2.0 if until is None else min(2.0, until - time.monotonic())
    if left < 0.05:
        return ""
    try:
        r = subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True, timeout=left)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def behind(cwd: str, until: float | None = None) -> str:
    """How far HEAD is behind origin's default branch, from the refs the last fetch left (no network)."""
    ref = _git(cwd, "rev-parse", "--abbrev-ref", "origin/HEAD", until=until) or next(
        (r for r in ("origin/main", "origin/master") if _git(cwd, "rev-parse", "--verify", "-q", r, until=until)), "")
    n = _git(cwd, "rev-list", "--count", f"HEAD..{ref}", until=until) if ref else ""
    if not n.isdigit() or int(n) == 0:
        return ""
    fh = _git(cwd, "rev-parse", "--git-path", "FETCH_HEAD", until=until)
    try:
        when = f" ({_age(time.time() - os.path.getmtime(os.path.join(cwd, fh)))} ago)" if fh else ""
    except OSError:
        when = ""
    return (f"GIT: this checkout is {n} commit(s) behind {ref} as of the last fetch{when}: git fetch, then rebase or "
            "merge before you build, merge or deploy.")


# ---------------------------------------------------------------------------------------------- what hooks inject
def _fit(text: str, room: int) -> str:
    """text, or as many of its first lines as fit in room characters."""
    return text if len(text) <= room else text[:max(0, room)].rsplit("\n", 1)[0] if room > 0 else ""


def start(c: dict | None, cwd: str | None, st: dict, until: float | None = None) -> tuple[str, str]:
    """A session start (any source: startup, resume, clear, compact), from the cache only: (the Canon lines that go
    first, the one-line notice for the owner when the cache is missing, older than 6 hours or failing). Fills `st`,
    the session's fresh state: watermark, cards shown, and whether the hub may leave its FACTS out (canon: only for a
    fresh cache whose last pull worked). The last 7 days of changes cover every product and the company, this
    checkout's product first, at most 8 lines; the rest are not marked seen, so the next message shows them. The
    whole stays under CANON_MAX: the notice, changes, conflicts and git line whole, then the core, then this
    checkout's card, then one line for each other product of this checkout. Git stops at `until`."""
    notice = status(c)
    st |= {"seen_until": 0, "seen": [], "cards": [], "canon": fresh(c)}
    remote = _git(cwd, "config", "--get", "remote.origin.url", until=until)
    keys = repo_keys(c, remote) if c else []
    text, conflicts = "", ""
    if c:
        weekago = (time.time() - WEEK_S) * 1000
        recent = unseen(c, st)
        text, shown = _changes([x for x in recent if _ms(x["at"]) >= weekago], "CANON CHANGES, last 7 days", 8, keys,
                               int(c.get("changes_cut") or 0))
        mark(c, st, shown + [x for x in recent if _ms(x["at"]) < weekago])
        n = c.get("open_conflicts")
        if isinstance(n, int) and n > 0:
            conflicts = (f"OPEN CONFLICTS in Commons: {n} (conflicts() lists them; one on an owner-only fact waits for "
                         "the owner, never resolve it yourself).")
    git = behind(cwd, until) if remote else ""
    room = CANON_MAX - sum(len(x) + 2 for x in (notice, text, conflicts, git) if x)
    head, card_text, liners = "", "", ""
    if has_canon(c):
        head = _fit(core(c), room)
        room -= len(head) + 2
        if keys:
            whole = short_card(keys[0], c)
            card_text = whole if len(whole) + 2 <= room else ""
            room -= len(card_text) + 2 if card_text else 0
            st["cards"] = [keys[0]] if card_text else []
            liners = _fit("\n".join(one_liner(k, c["products"][k]) for k in keys[1:]), room)
    return "\n\n".join(x for x in (notice, head, card_text, liners, text, conflicts, git) if x), notice


def prompt(c: dict | None, st: dict, text: str, cards: bool = True) -> str:
    """A prompt, from the cache only: one line per Canon change this session has not been shown (at most 10, then
    counted as seen), and the card of each product the prompt names that this session has not been shown (at most 3,
    while they fit under CANON_MAX)."""
    if not c:
        return ""
    ch, shown = _changes(unseen(c, st), "CANON CHANGES not yet shown in this session", 10)
    mark(c, st, shown)
    parts = [ch]
    if cards and has_canon(c):
        have, room = list(st.get("cards") or []), CANON_MAX - sum(len(x) + 2 for x in parts if x)
        for k in [k for k in mentioned(c, text) if k not in have][:3]:
            t = short_card(k, c)
            if len(t) + 2 <= room:
                parts.append(t)
                have.append(k)
                room -= len(t) + 2
        st["cards"] = have
    return "\n\n".join(x for x in parts if x)
