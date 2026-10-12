"""Verbatim store (v0.8): ``commons sweep`` keeps one memory item per message from history files.

Extraction (retro learning, observers) can miss things; the verbatim layer makes recall independent of extraction
quality (MemPalace-style "drawers"). Messages are redacted, deduplicated by content hash (re-running is a no-op),
timestamped with the original message time, tagged ``verbatim`` + source, and grouped per conversation so
``timeline_around`` shows the surrounding turns.
"""
from __future__ import annotations

import hashlib

from .retro import PARSERS, Checkpoints
from .store import Store, now

SCHEMA = "CREATE TABLE IF NOT EXISTS sweep_seen(hash TEXT PRIMARY KEY, item_id TEXT, ts REAL);"


def guess_source(path: str) -> str:
    p = path.replace("\\", "/")
    if "/.claude/" in p:
        return "claude-code"
    if "/.codex/" in p:
        return "codex"
    if "/.gemini/" in p:
        return "gemini"
    if p.endswith("conversations.json") or "chatgpt" in p.lower():
        return "chatgpt"
    if "claude-export" in p or "claude.ai" in p:
        return "claude-ai"
    return "generic"


def sweep(s: Store, paths: list[str], project: str | None = None, source: str = "auto", dry_run: bool = False,
          min_chars: int = 12) -> dict:
    s.x(SCHEMA) if s.dialect == "sqlite" else s.x(SCHEMA.rstrip(";"))
    groups: dict[str, list[str]] = {}
    for p in paths:
        groups.setdefault(guess_source(p) if source == "auto" else source, []).append(p)
    added = dup = convs = 0
    s.defer_embed = True  # batch the embeddings at the end
    try:
        for src, ps in groups.items():
            for conv in PARSERS[src](ps, Checkpoints(s, dry=True)):
                convs += 1
                proj = project or conv.get("project")
                for n, m in enumerate(conv["messages"]):
                    text = (m.get("text") or "").strip()
                    if len(text) < min_chars:
                        continue
                    h = hashlib.sha256(f"{src}|{conv['id']}|{m['role']}|{text}".encode()).hexdigest()[:32]
                    if s.q("SELECT 1 FROM sweep_seen WHERE hash=?", (h,)):
                        dup += 1
                        continue
                    if dry_run:
                        added += 1
                        continue
                    r = s.add_item("message", f"{m['role']}: {text}"[:20000], title=f"{conv.get('title') or conv['id']} #{n}"[:120],
                                   project=proj, tags=["verbatim", src],
                                   meta={"source": src, "conversation": conv["id"], "turn": n, "role": m["role"],
                                         "path": conv.get("path")},
                                   created_at=m.get("ts") or conv.get("ts"))
                    s.x("INSERT INTO sweep_seen(hash,item_id,ts) VALUES(?,?,?)", (h, r["id"], now()))
                    added += 1
    finally:
        s.defer_embed = False
    emb = s.reindex() if added and not dry_run else {"done": 0}
    return {"conversations": convs, "added": added, "duplicates": dup, "embedded": emb.get("done", 0), "dry_run": dry_run}
