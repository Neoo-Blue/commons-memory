"""Retrieval benchmarks: LongMemEval and LoCoMo (``commons bench``).

Measures *retrieval* only (no LLM answering): every question gets a fresh Commons store holding its haystack,
then ``Store.rank`` (the exact ranking behind ``search``) is asked the question and we check whether the evidence
session/turn is in the top-k. Results are per-question JSONL plus a summary, so numbers are reproducible.

- LongMemEval (Wu et al. 2024, ``longmemeval_s_cleaned.json``): evidence = ``answer_session_ids``;
  metrics recall_any@k / recall_all@k at session level (abstention questions have no evidence and are skipped).
- LoCoMo (Maharana et al. 2024, ``locomo10.json``): evidence = dialog ids (``D1:3``); turn-level and session-level
  recall@k; category 5 (adversarial/unanswerable) reported separately.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import statistics
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor

KS = (1, 3, 5, 10, 20)


def _lme_date(s: str) -> float:
    m = re.match(r"(\d{4})/(\d{2})/(\d{2})(?:\s*\(\w+\))?\s*(\d{2}):(\d{2})", s or "")
    if not m:
        return time.time()
    y, mo, d, h, mi = map(int, m.groups())
    return dt.datetime(y, mo, d, h, mi, tzinfo=dt.timezone.utc).timestamp()


def _locomo_date(s: str) -> float:
    for fmt in ("%I:%M %p on %d %B, %Y", "%I:%M %p on %d %b, %Y"):
        try:
            return dt.datetime.strptime(s.strip(), fmt).replace(tzinfo=dt.timezone.utc).timestamp()
        except ValueError:
            pass
    return time.time()


def _store(embedder: str):
    from .store import Store
    d = tempfile.mkdtemp(prefix="cbench-")
    s = Store(os.path.join(d, "b.db"), embedder=embedder)
    s.defer_embed = True
    return s, d


def _cleanup(d):
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def _rank_groups(s, query, as_of, group_of: dict, k: int, rerank: bool):
    ids, _, _ = s.rank(query, limit=200, as_of=as_of)
    if rerank:
        from .rerank import rerank_ids
        ids = rerank_ids(s, query, ids, top=int(os.environ.get("COMMONS_RERANK_TOP", "30")), how="cross-encoder")
    seen, out = set(), []
    for i in ids:
        g = group_of.get(i)
        if g and g not in seen:
            seen.add(g); out.append(g)
        if len(out) >= k:
            break
    return out, ids


# ---------------------------------------------------------------------------------------------- LongMemEval
def _lme_one(args):
    q, embedder, gran, rerank = args
    os.environ["COMMONS_TRUST_WEIGHTING"] = "0"
    s, d = _store(embedder)
    try:
        group_of = {}
        for sid, date, sess in zip(q["haystack_session_ids"], q["haystack_dates"], q["haystack_sessions"]):
            ts = _lme_date(date)
            if gran == "session":
                text = "\n".join(f"{t['role']}: {t['content']}" for t in sess if t["role"] == "user")
                if text.strip():
                    group_of[s.add_item("note", text, title=f"session {sid}", created_at=ts)["id"]] = sid
                continue
            for i, t in enumerate(sess):  # one item per user turn (+ a short slice of the assistant reply)
                if t["role"] != "user":
                    continue
                reply = sess[i + 1]["content"][:300] if i + 1 < len(sess) and sess[i + 1]["role"] == "assistant" else ""
                text = f"user: {t['content']}" + (f"\nassistant: {reply}" if reply else "")
                group_of[s.add_item("note", text, created_at=ts)["id"]] = sid
        s.reindex()
        top, _ = _rank_groups(s, q["question"], _lme_date(q["question_date"]), group_of, max(KS), rerank)
        gold = set(q["answer_session_ids"])
        res = {"id": q["question_id"], "type": q["question_type"], "n_gold": len(gold), "top": top[:10]}
        for k in KS:
            hit = gold & set(top[:k])
            res[f"any@{k}"] = int(bool(hit))
            res[f"all@{k}"] = int(hit == gold)
        return res
    finally:
        s.db.close() if hasattr(s, "db") else None
        _cleanup(d)


def longmemeval(path: str, out: str, embedder: str = "auto", granularity: str = "turn", limit: int | None = None,
                workers: int = 4, rerank: bool = False) -> dict:
    data = [q for q in json.load(open(path)) if q.get("answer_session_ids") and not q["question_id"].endswith("_abs")]
    if limit:
        data = data[:limit]
    return _run("longmemeval", _lme_one, [(q, embedder, granularity, rerank) for q in data], out, workers,
                {"dataset": os.path.basename(path), "granularity": granularity, "embedder": embedder, "rerank": rerank},
                group_key="type")


# ---------------------------------------------------------------------------------------------- LoCoMo
def _loc_one(args):
    conv, embedder, rerank = args
    os.environ["COMMONS_TRUST_WEIGHTING"] = "0"
    s, d = _store(embedder)
    try:
        c = conv["conversation"]
        turn_of, sess_of = {}, {}
        n = 1
        while f"session_{n}" in c:
            ts = _locomo_date(c.get(f"session_{n}_date_time", ""))
            for t in c[f"session_{n}"]:
                text = f"{t['speaker']}: {t['text']}" + (f" [shares image: {t['blip_caption']}]" if t.get("blip_caption") else "")
                iid = s.add_item("note", text, created_at=ts)["id"]
                turn_of[iid] = t["dia_id"]
                sess_of[iid] = f"S{n}"
            n += 1
        s.reindex()
        ref = max(_locomo_date(c.get(f"session_{i}_date_time", "")) for i in range(1, n)) + 86400
        rows = []
        for qi, qa in enumerate(conv["qa"]):
            gold = set(qa.get("evidence") or [])
            if not gold:
                continue
            top, ids = _rank_groups(s, qa["question"], ref, turn_of, max(KS), rerank)
            gold_s = {"S" + g.split(":")[0].lstrip("D") for g in gold}
            seen, tops = set(), []
            for i in ids:
                g = sess_of.get(i)
                if g and g not in seen:
                    seen.add(g); tops.append(g)
            r = {"id": f"{conv['sample_id']}#{qi}", "type": f"cat{qa['category']}", "n_gold": len(gold), "top": top[:10]}
            for k in KS:
                hit = gold & set(top[:k])
                r[f"any@{k}"] = int(bool(hit))
                r[f"all@{k}"] = int(hit == gold)
                r[f"sess_any@{k}"] = int(bool(gold_s & set(tops[:k])))
            rows.append(r)
        return rows
    finally:
        _cleanup(d)


def locomo(path: str, out: str, embedder: str = "auto", limit: int | None = None, workers: int = 4,
           rerank: bool = False) -> dict:
    data = json.load(open(path))[: limit or None]
    return _run("locomo", _loc_one, [(c, embedder, rerank) for c in data], out, workers,
                {"dataset": os.path.basename(path), "embedder": embedder, "rerank": rerank}, group_key="type", flat=True)


# ---------------------------------------------------------------------------------------------- common
def _run(name, fn, jobs, out, workers, cfg, group_key, flat=False):
    os.makedirs(out, exist_ok=True)
    # each worker's ONNX session defaults to every core; N workers x all cores thrashes (load ~40 on 8 cores)
    os.environ.setdefault("COMMONS_EMBED_THREADS", str(max(1, (os.cpu_count() or 2) // max(1, workers))))
    t0 = time.time()
    rows = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for r in ex.map(fn, jobs):
            rows.extend(r if flat else [r])
    from .embed import get_embedder
    e = get_embedder(None if cfg["embedder"] == "auto" else cfg["embedder"])
    summary = {"benchmark": name, **cfg, "embedder_resolved": e.name if e else "none", "n": len(rows),
               "seconds": round(time.time() - t0, 1), "metrics": _agg(rows), "by_type": {}}
    for t in sorted({r[group_key] for r in rows}):
        sub = [r for r in rows if r[group_key] == t]
        summary["by_type"][t] = {"n": len(sub), **_agg(sub)}
    import commons_memory
    summary["commons_version"] = commons_memory.__version__
    tag = f"{name}-{summary['embedder_resolved'].replace(':', '_').replace('/', '_')}" + \
          (f"-{cfg['granularity']}" if "granularity" in cfg else "") + ("-rerank" if cfg.get("rerank") else "")
    with open(os.path.join(out, f"{tag}.jsonl"), "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    with open(os.path.join(out, f"{tag}.summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    return summary


def _agg(rows):
    keys = [k for k in (rows[0] if rows else {}) if "@" in k]
    return {k: round(statistics.mean(r[k] for r in rows), 4) for k in keys}
