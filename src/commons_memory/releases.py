"""Release channel: when a new Commons version starts, every agent hears what changed and what it must adapt.

The changelog ships inside the package. Each release entry may carry a bullet that starts with "**For agents:**":
those lines are the adaptations agents must make. On startup Commons broadcasts the new entries once (message type
"release"); whats_new() returns what an agent hasn't seen yet, and pickup() reminds it until it has.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import __version__
from .store import Store


def changelog() -> str:
    try:
        from importlib.resources import files
        p = files("commons_memory") / "CHANGELOG.md"
        if p.is_file():
            return p.read_text()
    except Exception:  # noqa: BLE001
        pass
    dev = Path(__file__).resolve().parents[2] / "CHANGELOG.md"  # running from a source checkout
    return dev.read_text() if dev.exists() else ""


def _key(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def entries() -> list[dict]:
    out = []
    for m in re.finditer(r"^## v(\d+\.\d+\.\d+): (.*?)\n(.*?)(?=^## v|\Z)", changelog(), re.S | re.M):
        ver, title, body = m.group(1), m.group(2).strip(), m.group(3).strip()
        changed = [re.sub(r"\s+", " ", re.sub(r"\*\*", "", b)).strip()[:220]
                   for b in re.split(r"\n(?=- )", body) if b.startswith("- ") and "For agents:" not in b]
        adapt = []
        for b in re.split(r"\n(?=- )", body):
            if b.startswith("- **For agents:**"):
                adapt += [re.sub(r"\s+", " ", x).strip(" -") for x in re.split(r"\n\s*-\s", b.replace("- **For agents:**", "", 1)) if x.strip()]
        out.append({"version": ver, "title": title, "changed": [c.lstrip("- ") for c in changed], "adapt": adapt})
    return out


def _since(after: str | None) -> list[dict]:
    cur = _key(__version__)
    return [e for e in entries() if _key(e["version"]) <= cur and (not after or _key(e["version"]) > _key(after))]


def _render(es: list[dict]) -> str:
    parts = []
    for e in es:
        parts.append(f"v{e['version']}: {e['title']}\nWhat changed:\n" + "\n".join(f"- {c}" for c in e["changed"][:8])
                     + ("\nWhat you must adapt:\n" + "\n".join(f"- {a}" for a in e["adapt"]) if e["adapt"] else ""))
    return "\n\n".join(parts)


def announce(s: Store) -> dict | None:
    """Once per deployed version: tell every agent what changed since the last announced version."""
    last = s.q("SELECT value FROM meta WHERE key='release:announced'")
    last = last[0]["value"] if last else None
    if last == __version__:
        return None
    es = _since(last)[:5] or [e for e in entries() if e["version"] == __version__]
    s.x("INSERT OR REPLACE INTO meta VALUES('release:announced', ?)", (__version__,))
    if not es:
        return None
    from .messaging import Messaging
    Messaging(s).send(None, "*", f"Commons v{__version__} is live on the hub.\n\n{_render(es)}\n\nCall whats_new() any "
                      "time for this list; the playbook you follow is updated too.", mtype="release",
                      subject=f"Commons v{__version__}: what changed and what to adapt", by="release notes")
    s.audit(None, "release.announce", __version__, {"versions": [e["version"] for e in es]})
    return {"announced": __version__, "versions": [e["version"] for e in es]}


def whats_new(s: Store, agent_id: str | None) -> dict:
    key = f"release_seen:{agent_id}"
    seen = s.q("SELECT value FROM meta WHERE key=?", (key,))
    seen = seen[0]["value"] if seen else None
    es = _since(seen)[:8] if seen else [e for e in entries() if e["version"] == __version__]
    if agent_id:
        s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, __version__))
    return {"current": __version__, "since": seen, "releases": es}


def nudge(s: Store, agent_id: str | None) -> str:
    if not agent_id:
        return ""
    seen = s.q("SELECT value FROM meta WHERE key=?", (f"release_seen:{agent_id}",))
    if seen and seen[0]["value"] == __version__:
        return ""
    return f"COMMONS UPDATED to v{__version__}: call whats_new() for what changed and what you must adapt.\n\n"
