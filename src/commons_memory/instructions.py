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

import os
import re
import shutil
from pathlib import Path

BEGIN = "<!-- commons:begin (managed by `commons instructions`; edits inside this block are overwritten) -->"
END = "<!-- commons:end -->"
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
    from .server import INSTRUCTIONS
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


def _apply(path: Path, block: str | None, whole: bool) -> str:
    """block=None removes. whole=True: the file is ours entirely (skill, .mdc)."""
    old = path.read_text() if path.exists() else ""
    if whole:
        if block is None:
            if path.exists():
                path.unlink()
                return "removed"
            return "absent"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(block)
        return "updated" if old else "installed"
    pat = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.S)
    if block is None:
        if not pat.search(old):
            return "absent"
        path.write_text(pat.sub("", old).rstrip() + "\n")
        return "removed"
    if old and not path.with_name(path.name + ".bak-commons").exists():
        shutil.copy2(path, path.with_name(path.name + ".bak-commons"))
    new = pat.sub(lambda _m: block, old) if pat.search(old) else (old.rstrip() + "\n\n" + block + "\n" if old else block + "\n")
    if new == old:
        return "unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new)
    return "updated" if pat.search(old) else "installed"


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
            out.append({"ai": h, "file": str(path), "status": status})
    return out


def refresh() -> list[str]:
    """Bring every already-installed Commons block up to date with the current playbook (never installs new ones).
    The session-start hook calls it, so an AI's always-loaded instructions don't go stale after an upgrade."""
    from .server import INSTRUCTIONS
    out, seen = [], set()
    for h, paths in TARGETS.items():
        for p in paths:
            path = Path(os.path.expanduser(p))
            if path in seen or not path.exists():
                continue
            seen.add(path)
            text = path.read_text()
            if INSTRUCTIONS in text or (BEGIN not in text and not p.endswith(WHOLE)):
                continue
            head = re.search(r"connected to Commons( at (\S+))?( as agent `([^`]+)`)?", text)
            url, agent = (head.group(2), head.group(4)) if head else (None, None)
            whole = p.endswith(WHOLE)
            if whole and "Commons" not in text:
                continue
            _apply(path, _render(p, master(url.rstrip(".") if url else None, agent)), whole)
            out.append(str(path))
    return out
