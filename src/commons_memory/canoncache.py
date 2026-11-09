"""Canon on this machine (v0.38): the hook client's state cache, so a session starts with the current truth even when
the hub is slow or down.

A detached pull (`commons canon pull`, started by the hooks at session start and at most every 5 minutes on prompts)
writes ~/.commons/state/canon.json (the hub's canon() plus fetched_at, ok and error: a failed pull keeps the old data
and records ok=false) and canon.md (the CANON header with the precedence, the company core, one short card per
product). Hooks read them with no hub call: a session start opens with the core, the card of the product whose
work:<key>.repos holds this checkout's origin, the last 7 days of changes, the open conflict count and how far the
checkout is behind origin; each prompt adds one line per change since the session's watermark
(state/sessions/<session_id>.json) and, once per session, the card of a product it names. Peers and guards read the
same files. Copies are one-way: nothing here writes to Commons.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .canon import PRECEDENCE, _clip, _line, _list, _slug, card, kind_of
from .ids import iso

STALE_S = 6 * 3600      # a cache older than this is called out at session start
PULL_PROMPT_S = 300     # prompts start a pull at most this often
PULL_START_S = 60       # session starts within a minute of the last pull share it
WEEK_S = 7 * 86400
FIXED = ("COMPANY CORE", "- precedence: ", "- owner-only")  # the core's lines that exist with no Canon fact at all
NAMES = ("name", "names", "alias", "aliases")              # predicates that name a product (and name_*, e.g. name_zh)
HUB = "canon(product) on the hub has the current truth"


def home() -> Path:
    return Path(os.environ.get("COMMONS_HOME", Path.home() / ".commons")) / "state"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)  # readers never see half a file


def read() -> dict | None:
    try:
        c = json.loads((home() / "canon.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return c if isinstance(c, dict) else None


def _ms(at) -> int:
    from .store import _parse_time
    return int(_parse_time(str(at)) * 1000)


# ---------------------------------------------------------------------------------------------- pull
def pull(store=None) -> dict:
    """canon() from the hub at $COMMONS_URL (else from `store`, the local database) into canon.json and canon.md. A
    failure keeps the old cache and records ok=false with the error."""
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
        if not isinstance(c, dict) or "core" not in c:
            raise ValueError(f"not a canon() reply: {str(c)[:120]}")
        c |= {"fetched_at": iso(time.time()), "ok": True, "error": ""}
        _write(home() / "canon.md", render(c))
    except Exception as e:  # noqa: BLE001 - hub down, too old for canon(), or refusing: keep what we had, say why
        while getattr(e, "exceptions", None):  # the MCP client wraps the real error in exception groups
            e = e.exceptions[0]
        err = " ".join(f"{type(e).__name__}: {e}".split())[:300]
        c = (read() or {"fetched_at": None}) | {"ok": False, "error": err.replace(key, "[key]") if key else err}
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
    """`commons canon pull`, detached: it outlives the hook, holds none of its output, and gives up after 30 s."""
    kw = {"creationflags": 0x208} if os.name == "nt" else {"start_new_session": True}  # DETACHED | NEW_PROCESS_GROUP
    subprocess.Popen([sys.executable, "-m", "commons_memory", "canon", "pull"], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                     env={**os.environ, "COMMONS_CALL_TIMEOUT": "30"}, **kw)


# ---------------------------------------------------------------------------------------------- rendering
def has_canon(c: dict | None) -> bool:
    """Any Canon fact? (The hub's core states the precedence and the owner-only list even with none.)"""
    return bool(c) and bool(c.get("products") or c.get("vendors") or any(
        not x.startswith(FIXED) for x in str(c.get("core") or "").splitlines()))


def core(c: dict) -> str:
    """The CANON header carrying the precedence, then the company core without its own precedence line: <= 20 lines."""
    lines = str(c.get("core") or "").splitlines()
    prec = next((x[len(FIXED[1]):] for x in lines if x.startswith(FIXED[1])), PRECEDENCE)
    return "\n".join(([f"CANON (current truth; precedence: {prec})"] +
                      [x for x in lines if not x.startswith(FIXED[1])])[:20])


def short_card(key: str, c: dict) -> str:
    lines = card(key, (c.get("products") or {}).get(key)).splitlines()
    return "\n".join([_clip(x, 240) for x in lines[:15]] + (["- ... canon(product) has the rest"] if len(lines) > 15 else []))


def render(c: dict) -> str:
    """canon.md: the core, then one short card per product."""
    return "\n\n".join([core(c)] + [short_card(k, c) for k in sorted(c.get("products") or {})]) + "\n"


def _changes(chs: list[dict], title: str, limit: int) -> str:
    if not chs:
        return ""
    out = [f"{title} (cite ids, never restate values; canon(product) has the card):"]
    out += [f"- {_line(x)}" for x in chs[-limit:]]
    if len(chs) > limit:
        out.append(f"- and {len(chs) - limit} earlier: changes(since='{chs[0]['at']}')")
    return "\n".join(out)


def _age(s: float) -> str:
    return f"{int(s // 60)} min" if s < 3600 else f"{int(s // 3600)} h" if s < 2 * 86400 else f"{int(s // 86400)} days"


def status(c: dict | None) -> str:
    """'' when the cache is fresh and its last pull worked; else one plain line saying what is wrong."""
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


def unseen(c: dict, st: dict) -> list[dict]:
    """The cache's changes this session has not been shown, oldest first, and the watermark moved past them:
    seen_until = the cache's generated_at (epoch ms); edge = the changes stamped that very second, which a later pull
    may add to (times are whole seconds)."""
    seen, edge = int(st.get("seen_until") or 0), set(st.get("edge") or [])
    chs = [x for x in c.get("changes") or [] if isinstance(x, dict) and x.get("at")]
    new = [x for x in chs if _ms(x["at"]) > seen or (_ms(x["at"]) == seen and _fp(x) not in edge)]
    top = max([seen] + [_ms(x["at"]) for x in chs] + ([_ms(c["generated_at"])] if c.get("generated_at") else []))
    st["seen_until"] = top
    st["edge"] = sorted({_fp(x) for x in chs if _ms(x["at"]) == top} | (edge if top == seen else set()))
    return new


def repo_keys(c: dict, remote: str) -> list[str]:
    """Products whose work:<key>.repos holds this git remote (https or ssh URL, or owner/name)."""
    want = _slug(remote) if remote else ""
    return [k for k, e in sorted((c.get("products") or {}).items())
            if want and want in {_slug(u) for u in _list(((e or {}).get("work") or {}).get("repos"))}]


def names(key: str, entry: dict | None) -> list[str]:
    """What a prompt may call a product: its key, its name/names/alias/aliases/name_* facts, and a name of three or
    more words without its last one ("Secret Santa Hotline" -> "secret santa"). Lowercase."""
    out = [key]
    for side in ((entry or {}).get("product") or {}, (entry or {}).get("work") or {}):
        for p, v in side.items():
            if p in NAMES or p.startswith("name_"):
                out += [x.strip() for x in (v.values() if isinstance(v, dict) else _list(v))
                        if isinstance(x, str) and x.strip()]
    out += [" ".join(n.split()[:-1]) for n in out if n.isascii() and len(n.split()) >= 3]
    return list(dict.fromkeys(n.lower() for n in out))


def mentioned(c: dict, text: str) -> list[str]:
    """Product keys a prompt names, in order. ASCII names match whole words; others (Chinese) match anywhere, since
    those scripts put no spaces between words. A name inside a longer one ("santa" in "secret santa") counts only for
    the longer one."""
    t, hits = " ".join((text or "").split()).lower(), []
    for k, e in (c.get("products") or {}).items():
        for n in names(k, e):
            if n.isascii() and len(n) >= 4:
                rx = r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])"
            elif not n.isascii() and len(n) >= 2:
                rx = re.escape(n)
            else:
                continue
            hits += [(m.start(), m.end(), k) for m in re.finditer(rx, t)]
    keep: list[tuple] = []
    for s, e, k in sorted(hits, key=lambda h: h[0] - h[1]):  # longest first
        if not any(s >= s2 and e <= e2 for s2, e2, _ in keep):
            keep.append((s, e, k))
    return list(dict.fromkeys(k for _, _, k in sorted(keep)))


# ---------------------------------------------------------------------------------------------- git
def _git(cwd: str | None, *a: str) -> str:
    if not cwd or os.environ.get("COMMONS_HOOK_GIT") == "0":
        return ""
    try:
        r = subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def behind(cwd: str) -> str:
    """How far HEAD is behind origin's default branch, from the refs the last fetch left (no network)."""
    ref = _git(cwd, "rev-parse", "--abbrev-ref", "origin/HEAD") or next(
        (r for r in ("origin/main", "origin/master") if _git(cwd, "rev-parse", "--verify", "-q", r)), "")
    n = _git(cwd, "rev-list", "--count", f"HEAD..{ref}") if ref else ""
    if not n.isdigit() or int(n) == 0:
        return ""
    fh = _git(cwd, "rev-parse", "--git-path", "FETCH_HEAD")
    try:
        when = f" ({_age(time.time() - os.path.getmtime(os.path.join(cwd, fh)))} ago)" if fh else ""
    except OSError:
        when = ""
    return (f"GIT: this checkout is {n} commit(s) behind {ref} as of the last fetch{when}: git fetch, then rebase or "
            "merge before you build, merge or deploy.")


# ---------------------------------------------------------------------------------------------- what hooks inject
def start(c: dict | None, cwd: str | None, st: dict) -> tuple[str, str]:
    """A session start (any source: startup, resume, clear, compact), from the cache only: (the Canon lines that go
    first, the one-line notice for the owner when the cache is missing, older than 6 hours or failing). Fills `st`,
    the session's fresh state: watermark, cards shown, and whether Canon came from the cache (canon: then the hub
    leaves its FACTS out of the pack)."""
    notice = status(c)
    parts = [notice]
    st |= {"seen_until": 0, "edge": [], "cards": [], "canon": bool(c and c.get("core"))}
    remote = _git(cwd, "config", "--get", "remote.origin.url")
    if c:
        recent = unseen(c, st)
        if has_canon(c):
            keys = repo_keys(c, remote)
            weekago = (time.time() - WEEK_S) * 1000
            recent = [x for x in recent if _ms(x["at"]) >= weekago and (not keys or kind_of(x.get("subject")) not in (
                "product", "work") or str(x["subject"]).split(":", 1)[1] in keys)]
            parts += [core(c)] + [short_card(k, c) for k in keys] + [_changes(recent, "CANON CHANGES, last 7 days", 8)]
            st["cards"] = keys
        n = c.get("open_conflicts")
        if isinstance(n, int) and n > 0:
            parts.append(f"OPEN CONFLICTS in Commons: {n} (conflicts() lists them; one on an owner-only fact waits for "
                         "the owner, never resolve it yourself).")
    if remote:
        parts.append(behind(cwd))
    return "\n\n".join(x for x in parts if x), notice


def prompt(c: dict | None, st: dict, text: str, cards: bool = True) -> str:
    """A prompt, from the cache only: one line per Canon change since this session's watermark (then moved past
    them), and the card of each product the prompt names that this session has not been shown (at most 3)."""
    if not c:
        return ""
    parts = [_changes(unseen(c, st), "CANON CHANGES since your last message", 10)]
    if cards and has_canon(c):
        shown = list(st.get("cards") or [])
        for k in [k for k in mentioned(c, text) if k not in shown][:3]:
            parts.append(short_card(k, c))
            shown.append(k)
        st["cards"] = shown
    return "\n\n".join(x for x in parts if x)
