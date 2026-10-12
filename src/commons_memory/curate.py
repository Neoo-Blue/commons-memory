"""AI-managed import (v0.9): a manager model reviews imported memories before they enter shared memory.

`commons import <source> --curate [--model M] [--budget USD]` sends items in batches to the `curator` role (any
provider: Claude, OpenRouter, a local model...). For each item the manager decides keep / drop / merge, rewrites a
self-contained title and summary, picks kind, project and tags, and pulls out user preferences. Confident keeps go
straight in, uncertain ones wait in the review queue (dashboard Retro learning page, `commons learn queue`), drops
and merges are recorded so a re-run skips them. A cheap pass removes exact duplicates first; a dry run prints the
estimated tokens and cost; the budget is a hard stop (unreviewed items are left for the next run).
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import re
import threading

from .store import CommonsError, Store

SYSTEM = """You are the memory manager for Commons, the long-term memory shared by several AI agents that work for one person.
You review candidate memories imported from another tool and decide what belongs in shared memory.
KEEP: durable facts about projects, systems and infrastructure; decisions with their reasons; root causes and fixes; user preferences and rules; how-tos and gotchas; where things live.
DROP: transient status ("build passed", "monitoring", "task launched", "waiting"), restated progress, content with nothing reusable, duplicates of a better item.
MERGE: an item that repeats another item in this batch: set "merge_into" to the better item's index.
For each KEEP write: a self-contained title (max 90 chars) and a summary of at most 25 words usable with no other context; kind (memory|decision|fact|procedure); project (keep the given one unless it is clearly wrong; null if none); up to 4 lowercase tags; confidence 0-1 that it is worth keeping (0.9+: clearly durable and useful later; 0.6-0.8: useful; below 0.5: doubtful, a human should decide). If the item states a stable rule or preference of the user, also give it as "preference": one imperative sentence.
Reply with JSON only, one entry per input index, in this shape:
{"items":[{"i":0,"a":"keep","k":"memory","t":"title","s":"summary","p":"project","g":["tag"],"c":0.9,"pref":null},{"i":1,"a":"drop"},{"i":2,"a":"merge","into":0}]}"""

_S, _N = {"type": "string"}, {"type": "null"}
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["items"], "properties": {"items": {
    "type": "array", "items": {"type": "object", "additionalProperties": False,
                               "required": ["i", "a", "into", "k", "t", "s", "p", "g", "c", "pref"],
                               "properties": {"i": {"type": "integer"}, "a": {"type": "string", "enum": ["keep", "drop", "merge"]},
                                              "into": {"anyOf": [{"type": "integer"}, _N]},
                                              "k": {"anyOf": [{"type": "string", "enum": ["memory", "decision", "fact", "procedure"]}, _N]},
                                              "t": {"anyOf": [_S, _N]}, "s": {"anyOf": [_S, _N]}, "p": {"anyOf": [_S, _N]},
                                              "g": {"type": "array", "items": _S}, "c": {"type": "number"},
                                              "pref": {"anyOf": [_S, _N]}}}}}}
REVIEW_CHARS = 1200  # the manager sees this much of each item; the full text is stored when kept


def _fp(it: dict) -> str:
    return hashlib.sha1(" ".join(f"{it.get('title', '')} {it.get('content', '')}".lower().split()).encode()).hexdigest()


def _parse(text: str) -> list[dict]:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ValueError("no JSON object in curator reply")
    return json.loads(m.group(0)).get("items") or []


def estimate(items: list[dict], model: str, batch: int = 30) -> dict:
    from .usage import estimate_cost
    tin = sum(min(len(i.get("content") or ""), REVIEW_CHARS) + len(i.get("title") or "") + 60 for i in items) // 4
    tin += (len(items) // batch + 1) * (len(SYSTEM) // 4)
    tout = len(items) * 60  # measured ~55-60 output tokens per item with thinking off (2026-10, Haiku 5.5)
    return {"items": len(items), "batches": len(items) // batch + 1, "tokens_in": tin, "tokens_out": tout,
            "est_cost": round(estimate_cost(model.split("/", 1)[-1], tin, tout), 2), "model": model}


def curate(s: Store, items: list[dict], *, model: str | None = None, batch: int = 30, budget: float | None = None,
           auto_approve: float = 0.6, workers: int = 4, agent_id: str | None = None, dry_run: bool = False,
           sink=None, known_fn=None, log=lambda m: None) -> dict:
    """`s` provides the model (provider layer, usage accounting). Decisions go to `sink(items_with_status)`, by
    default importers.import_items on `s`; pass a remote sink to curate into another hub."""
    from . import importers as I
    from .providers import ProviderLayer
    sink = sink or (lambda xs: I.import_items(s, xs, agent_id=agent_id))
    known_fn = known_fn or (lambda srcs: I.known(s, srcs))
    pl = ProviderLayer(s)
    model = model or pl.role_model("curator")
    if not model:
        raise CommonsError("no curator model: `commons models role curator <provider/model>` or pass --model "
                           "(e.g. anthropic/claude-haiku-5-5 after `commons models add anthropic ...`)")
    # skip what an earlier run already imported, dropped or queued, and exact duplicates
    seen = known_fn([i["src"] for i in items if i.get("src")])
    todo, dups, fps = [], [], set()
    for it in items:
        if it.get("src") in seen:
            continue
        f = _fp(it)
        if f in fps:
            dups.append(dict(it, status="duplicate"))
            continue
        fps.add(f)
        todo.append(it)
    est = estimate(todo, model, batch)
    if dry_run:
        return est | {"duplicates_removed": len(dups), "already_done": len(items) - len(todo) - len(dups)}
    dup = len(dups)
    if dups:
        sink(dups)
    lock = threading.Lock()
    tot = {"kept": 0, "queued": 0, "dropped": 0, "merged": 0, "preferences": 0, "cost": 0.0, "tokens_in": 0,
           "tokens_out": 0, "batches": 0, "failed_batches": 0, "duplicates_removed": dup}
    stop = threading.Event()

    def review(chunk: list[dict]) -> None:
        if stop.is_set():
            return
        payload = [{"i": n, "title": it.get("title", "")[:200], "content": (it.get("content") or "")[:REVIEW_CHARS],
                    "project": it.get("project"), "tags": (it.get("tags") or [])[:6]} for n, it in enumerate(chunk)]
        try:
            r = {}
            r = pl.chat(model, json.dumps(payload, ensure_ascii=False), system="ROLE: curator.", cache_prefix=SYSTEM,
                        max_tokens=200 * len(chunk) + 2000, role="curator", agent_id=agent_id, effort="minimal",
                        schema=SCHEMA)
            decisions = {d.get("i"): d for d in _parse(r["text"])}
        except Exception as e:  # noqa: BLE001 - leave the batch for the next run
            with lock:
                tot["failed_batches"] += 1
                tot["cost"] += (r or {}).get("cost", 0)  # a reply we couldn't use was still billed
            log(f"batch failed: {str(e)[:160]}")
            return
        out_items, counts = [], {"kept": 0, "queued": 0, "dropped": 0, "merged": 0}
        acts = {n: (decisions.get(n) or {"a": "keep", "c": 0.5}) for n in range(len(chunk))}  # unanswered -> human
        for n, d in acts.items():  # pass 1: fold merged items into their targets
            tgt = d.get("into")
            if (d.get("a") or d.get("action")) == "merge" and isinstance(tgt, int) and 0 <= tgt < len(chunk) and tgt != n \
                    and (acts[tgt].get("a") or "keep") == "keep":
                chunk[tgt] = dict(chunk[tgt], content=(chunk[tgt].get("content") or "") +
                                  f"\n\n(merged: {chunk[n].get('title') or ''}) {chunk[n].get('content') or ''}"[:2000])
            elif (d.get("a") or d.get("action")) == "merge":
                d["a"] = "drop"  # merge into nothing / into a dropped item = drop
        for n, it in enumerate(chunk):  # pass 2: emit decisions
            d = acts[n]
            act = d.get("a") or d.get("action")
            if act in ("drop", "merge"):
                out_items.append(dict(it, status=act))
                counts["dropped" if act == "drop" else "merged"] += 1
                continue
            kind = {"fact": "memory", "procedure": "memory"}.get(d.get("k"), d.get("k")) if d.get("k") in (
                "memory", "decision", "fact", "procedure") else it.get("kind", "memory")
            out = dict(it, kind=kind, title=(d.get("t") or it.get("title") or "")[:120], summary=d.get("s") or "",
                       project=d.get("p") if d.get("p") not in ("", None) else it.get("project"),
                       tags=sorted(set((it.get("tags") or [])[:2] + [str(t).lower() for t in (d.get("g") or [])[:4]]
                                       + ([d["k"]] if d.get("k") in ("fact", "procedure") else []))),
                       preference=d.get("pref") or it.get("preference"))
            conf = float(d.get("c") or 0.5)
            out_items.append(dict(out, confidence=conf, method=f"curator:{model}",
                                  status="keep" if conf >= auto_approve else "review"))
            counts["kept" if conf >= auto_approve else "queued"] += 1
        res = sink(out_items) if out_items else {}
        with lock:
            for k_, v_ in counts.items():
                tot[k_] += v_
            tot["preferences"] += res.get("preferences", 0)
            tot["cost"] += r.get("cost", 0)
            tot["tokens_in"] += r.get("tokens_in", 0)
            tot["tokens_out"] += r.get("tokens_out", 0)
            tot["batches"] += 1
            if budget is not None and tot["cost"] >= budget:
                stop.set()
            n_done = tot["batches"]
        if n_done % 10 == 0:
            log(f"{n_done}/{est['batches']} batches, ${tot['cost']:.2f}, kept {tot['kept']} queued {tot['queued']} "
                f"dropped {tot['dropped']} merged {tot['merged']}")

    chunks = [todo[k:k + batch] for k in range(0, len(todo), batch)]
    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(review, chunks))
    tot["cost"] = round(tot["cost"], 4)
    tot["stopped_by_budget"] = stop.is_set()
    tot["unreviewed"] = len(todo) - tot["kept"] - tot["queued"] - tot["dropped"] - tot["merged"]
    tot["model"] = model
    return tot
