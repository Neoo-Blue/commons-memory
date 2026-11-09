"""Install the Commons master instructions into each AI's always-loaded instructions, so every agent knows the routine
from its first message without the user explaining it: `commons instructions install all`.

Targets (global, per user): Claude Code ~/.claude/CLAUDE.md plus an always-relevant skill; Codex ~/.codex/AGENTS.md;
Gemini CLI and Antigravity ~/.gemini/GEMINI.md; Grok Build ~/.grok/AGENTS.md; Cursor ~/.cursor/rules/commons.mdc
(alwaysApply). The text sits between markers, so installing again updates it in place and uninstall removes only it;
the original file is copied to <file>.bak-commons once. Hosted chat apps (claude.ai, Grok, ChatGPT, Muse) can't be
written to from here: paste PROMPT.md into their custom instructions. Commons also hands every agent the same
playbook on its first pickup / turn / msg_recv, and again whenever it changes.
"""
from __future__ import annotations

import contextlib
import os
import re
import shutil
from pathlib import Path

BEGIN = "<!-- commons:begin (managed by `commons instructions`; edits inside this block are overwritten) -->"
END = "<!-- commons:end -->"
CANON_BEGIN = ("<!-- commons:canon:begin (Canon core, written by Commons at each session start from "
               "~/.commons/state/canon.json; edits inside this block are overwritten) -->")
CANON_END = "<!-- commons:canon:end -->"
CANON_FILES = ("~/.claude/CLAUDE.md", "~/.grok/AGENTS.md", "~/.gemini/GEMINI.md", "~/.codex/AGENTS.md")
TARGETS = {
    "claude-code": ["~/.claude/CLAUDE.md", "~/.claude/skills/commons/SKILL.md"],
    "codex": ["~/.codex/AGENTS.md"],
    "gemini": ["~/.gemini/GEMINI.md"],
    "antigravity": ["~/.gemini/GEMINI.md"],
    "grok": ["~/.grok/AGENTS.md"],
    "cursor": ["~/.cursor/rules/commons.mdc"],
    "opencode": ["~/.config/opencode/AGENTS.md"],
    "windsurf": ["~/.codeium/windsurf/memories/global_rules.md"],
    "zed": ["~/.config/zed/AGENTS.md"],
    "cline": ["~/Documents/Cline/Rules/commons.md"],
    "roo": ["~/.roo/rules/commons.md"],
    "kilo": ["~/.config/kilo/AGENTS.md"],
    "crush": ["~/.config/crush/CRUSH.md"],
}
HOMES = {"claude-code": "~/.claude", "codex": "~/.codex", "gemini": "~/.gemini", "antigravity": "~/.gemini",
         "grok": "~/.grok", "cursor": "~/.cursor", "opencode": "~/.config/opencode", "windsurf": "~/.codeium/windsurf",
         "zed": "~/.config/zed", "cline": "~/Documents/Cline", "roo": "~/.roo", "kilo": "~/.config/kilo",
         "crush": "~/.config/crush"}
WHOLE = ("SKILL.md", ".mdc", "/Rules/commons.md", "/rules/commons.md")  # files that are entirely Commons' own
SKILL_DESC = ("Commons, the shared memory, task and credential hub for all of the user's AIs. Use at the start of EVERY "
              "task and before answering anything about the user's projects, homelab, past work, credentials or other "
              "agents: check Commons first (pickup, search), claim the work with start_work, never redo another "
              "agent's work, use keys through use_credential, and hand off before stopping.")


def master(url: str | None = None, agent: str | None = None) -> str:
    from .playbook import INSTRUCTIONS
    where = (f" at {url}" if url else "") + (f" as agent `{agent}`" if agent else "")
    return ("## Commons: the shared memory, task and credential hub\n"
            f"This AI is connected to Commons{where}. Authenticate with the key in your MCP settings; never print or "
            "store it. If Commons is unreachable, say so once and carry on without it. Never invent tool results.\n"
            "Check Commons before you answer from scratch, before you say you don't know, and before you hand work to "
            "another agent: it usually already knows where things are and how to reach them.\n\n" + INSTRUCTIONS)


def _render(path: str, text: str) -> str:
    if path.endswith("SKILL.md"):
        return f"---\nname: commons\ndescription: {SKILL_DESC}\n---\n\n{text}\n"
    if path.endswith(".mdc"):
        return f"---\ndescription: Commons hub routine\nalwaysApply: true\n---\n\n{text}\n"
    if path.endswith(("/Rules/commons.md", "/rules/commons.md")):
        return text + "\n"
    return f"{BEGIN}\n{text}\n{END}"


@contextlib.contextmanager
def _locked():
    """One writer of the instruction files at a time on this machine (hooks of several sessions start together)."""
    try:
        import fcntl
    except ImportError:  # Windows: no flock; the atomic replace still keeps every reader safe
        yield
        return
    lock = Path(os.environ.get("COMMONS_HOME", Path.home() / ".commons")) / "instructions.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _read(path: Path) -> str:
    """The file as it is, its line endings (CRLF too) untouched."""
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


def _write(path: Path, text: str) -> None:
    """Atomic: a temp file beside the real file (a symlinked CLAUDE.md stays a symlink), then os.replace, so a session
    reading it at that moment sees the old file or the new one, never an empty or partial one. A file with a second
    name (a hard link) is written in place instead, so both names keep seeing one file."""
    real = path.resolve()
    real.parent.mkdir(parents=True, exist_ok=True)
    if real.exists() and real.stat().st_nlink > 1:
        with open(real, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return
    tmp = real.with_name(f".{real.name}.{os.getpid()}.commons-tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    with contextlib.suppress(OSError):
        shutil.copymode(real, tmp)
    os.replace(tmp, real)


def _block(lines: list[str], begin: str, end: str) -> tuple[tuple[int, int] | None, set[int]]:
    """(the first block, as the indexes of its begin and end lines, or None; the begin lines no end follows before the
    next begin). A marker counts only on a line of its own (spaces and tabs around it allowed), so a block never spans
    another begin: one whose end line was deleted is an orphan, and no later end ever pairs with it."""
    first, opened, orphans = None, None, set()
    for i, x in enumerate(lines):
        x = x.strip(" \t\r\n")
        if x == begin:
            if opened is not None:
                orphans.add(opened)
            opened = i
        elif x == end and opened is not None:
            first, opened = first or (opened, i), None
    return first, orphans | ({opened} if opened is not None else set())


def _apply(path: Path, block: str | None, whole: bool, begin: str = BEGIN, end: str = END) -> str:
    """block=None removes. whole=True: the file is ours entirely (skill, .mdc). begin/end mark the block (the playbook
    by default), each on a line of its own; a begin whose end was deleted is dropped (that line only, nothing the owner
    wrote after it) and the block written again. Locked, atomic, in the file's own line endings, with its byte order
    mark kept; a read-only file is left alone."""
    with _locked():
        old = _read(path) if path.exists() else ""
        if old and not os.access(path.resolve(), os.W_OK):
            return "read-only (left alone)"
        if whole:
            if block is None:
                if path.exists():
                    path.unlink()
                    return "removed"
                return "absent"
            _write(path, block)
            return "updated" if old else "installed"
        bom, body = ("\ufeff", old[1:]) if old.startswith("\ufeff") else ("", old)
        nl = "\r\n" if "\r\n" in body else "\n"
        lines = body.splitlines(keepends=True)
        found, orphans = _block(lines, begin, end)
        keep = [x for i, x in enumerate(lines) if i not in orphans and not (found and found[0] <= i <= found[1])]
        if block is None:
            if not found and not orphans:
                return "absent"
            _write(path, bom + "".join(keep).rstrip() + nl)
            return "removed"
        block = block.replace("\r\n", "\n").replace("\n", nl)
        if old and not path.with_name(path.name + ".bak-commons").exists():
            shutil.copy2(path, path.with_name(path.name + ".bak-commons"))
        if found:  # in place: what came before the block, the block, what came after it
            at = sum(1 for i in range(found[0]) if i not in orphans)
            tail = lines[found[1]][len(lines[found[1]].rstrip("\r\n")):]
            new = "".join(keep[:at]) + block + tail + "".join(keep[at:])
        else:
            rest = "".join(keep)
            new = rest.rstrip() + nl + nl + block + nl if rest.strip() else block + nl
        if bom + new == old:
            return "unchanged"
        _write(path, bom + new)
        return "updated" if found else "installed"


def install(harness: str = "all", url: str | None = None, agent: str | None = None, uninstall: bool = False) -> list[dict]:
    names = list(TARGETS) if harness == "all" else [harness]
    out, done = [], set()
    for h in names:
        if h not in TARGETS:
            raise ValueError(f"unknown AI {h!r}; one of {', '.join(TARGETS)} or all")
        if harness == "all" and not Path(os.path.expanduser(HOMES[h])).is_dir():
            out.append({"ai": h, "status": "skipped (not installed here)"})
            continue
        text = master(url, agent)
        for p in TARGETS[h]:
            path = Path(os.path.expanduser(p))
            if path in done:
                continue
            done.add(path)
            whole = p.endswith(WHOLE)
            status = _apply(path, None if uninstall else _render(p, text), whole)
            if uninstall and not whole:
                _apply(path, None, False, CANON_BEGIN, CANON_END)
            out.append({"ai": h, "file": str(path), "status": status})
    return out


def canon_core(text: str | None) -> list[str]:
    """Write the Canon block (text, at most 20 lines: canoncache.block, which holds no text an agent wrote) into each
    always-loaded file that carries the Commons playbook (appended once, then updated in place), so sub-agents, peers
    and the woken Claude start with it too; None removes it. Nothing outside the block's markers changes, and no value
    can forge a marker: < and > in the text become ‹ and ›. Returns the files changed."""
    block = None if text is None else "\n".join(
        [CANON_BEGIN] + [x.replace("<", "‹").replace(">", "›") for x in text.splitlines()[:20]] + [CANON_END])
    out = []
    for p in CANON_FILES:
        path = Path(os.path.expanduser(p))
        try:
            if block is not None and BEGIN not in _read(path):
                continue
            if _apply(path, block, False, CANON_BEGIN, CANON_END) in ("updated", "installed", "removed"):
                out.append(str(path))
        except (OSError, ValueError):  # missing, unreadable or not UTF-8: left alone, and the next file still gets it
            continue
    return out


def refresh() -> list[str]:
    """Bring every already-installed Commons block up to date with the current playbook (never installs new ones).
    The session-start hook calls it, so an AI's always-loaded instructions don't go stale after an upgrade."""
    from .playbook import INSTRUCTIONS
    out, seen = [], set()
    for h, paths in TARGETS.items():
        for p in paths:
            path = Path(os.path.expanduser(p))
            if path in seen or not path.exists():
                continue
            seen.add(path)
            try:
                text = _read(path)
                if INSTRUCTIONS in text or (BEGIN not in text and not p.endswith(WHOLE)):
                    continue
                head = re.search(r"connected to Commons( at (\S+))?( as agent `([^`]+)`)?", text)
                url, agent = (head.group(2), head.group(4)) if head else (None, None)
                whole = p.endswith(WHOLE)
                if whole and "Commons" not in text:
                    continue
                if _apply(path, _render(p, master(url.rstrip(".") if url else None, agent)), whole) in ("updated",
                                                                                                        "installed"):
                    out.append(str(path))
            except (OSError, ValueError):  # unreadable or not UTF-8: left alone, and the next file is still refreshed
                continue
    return out
