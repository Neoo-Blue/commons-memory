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
import sys
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


# ---------------------------------------------------------------------------------------------- self-check
SELF_SYSTEM = """You write evaluation questions for a memory system. For each memory, write ONE question that a person
would ask later, when they half-remember the topic, and that this memory answers. Paraphrase: don't copy distinctive
phrases, names of files, or the title verbatim; ask the way someone who forgot the details would.
Reply with JSON only: {"q":[{"i":0,"question":"..."}]} with one entry per input index."""
SELF_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["q"], "properties": {"q": {
    "type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["i", "question"],
                               "properties": {"i": {"type": "integer"}, "question": {"type": "string"}}}}}}


def selfcheck(s, sample: int = 100, model: str | None = None, seed: int = 7, kinds: tuple = ("memory", "decision", "fact"),
              log=lambda m: None) -> dict:
    """Is this store doing its job? Sample real memories, let a model write a paraphrased question each one answers,
    and measure whether search ranks that memory first / in the top 5 / top 10 (known-item retrieval on your own data).
    The summary is kept in meta 'selfcheck:last' for the dashboard."""
    import random
    from .providers import ProviderLayer
    from .store import CommonsError
    pl = ProviderLayer(s)
    model = model or pl.role_model("curator") or pl.role_model("grader")
    if not model:
        raise CommonsError("self-check needs a model: --model provider/model or set the curator role")
    ids = [r["id"] for r in s.q(f"SELECT id FROM items WHERE kind IN ({','.join('?' * len(kinds))}) AND superseded_by IS NULL "
                                f"AND length(content) > 40 ORDER BY id", list(kinds))]
    pick = random.Random(seed).sample(ids, min(sample, len(ids)))
    items = [dict(r) for i in pick for r in s.q("SELECT id, kind, project_id, title, content FROM items WHERE id=?", (i,))]
    rows, cost = [], 0.0
    for b in range(0, len(items), 20):
        chunk = items[b:b + 20]
        payload = [{"i": n, "title": it["title"], "memory": it["content"][:900]} for n, it in enumerate(chunk)]
        try:
            r = pl.chat(model, json.dumps(payload, ensure_ascii=False), system="ROLE: grader.", cache_prefix=SELF_SYSTEM,
                        max_tokens=120 * len(chunk) + 600, role="grader", effort="minimal", schema=SELF_SCHEMA)
            qs = {q["i"]: q["question"] for q in json.loads(r["text"])["q"]}
            cost += r.get("cost", 0)
        except Exception as e:  # noqa: BLE001
            log(f"question batch failed: {str(e)[:160]}")
            continue
        for n, it in enumerate(chunk):
            q = qs.get(n)
            if not q:
                continue
            ranked, _, _ = s.rank(q, limit=10)
            pos = (ranked or []).index(it["id"]) + 1 if it["id"] in (ranked or []) else None
            rows.append({"id": it["id"], "kind": it["kind"], "question": q, "rank": pos})
        log(f"{len(rows)}/{len(items)} questions scored")

    def agg(rs):
        m = len(rs) or 1
        return {"n": len(rs), **{f"hit@{k}": round(sum(1 for r in rs if r["rank"] and r["rank"] <= k) / m, 3) for k in (1, 5, 10)},
                "mrr": round(sum(1 / r["rank"] for r in rs if r["rank"]) / m, 3)}
    out = {"model": model, "sample": len(rows), "items_in_store": len(ids), "cost": round(cost, 4), **agg(rows),
           "by_kind": {k: agg([r for r in rows if r["kind"] == k]) for k in sorted({r["kind"] for r in rows})},
           "misses": [r for r in rows if not r["rank"]][:15], "ts": time.time()}
    s.x("INSERT OR REPLACE INTO meta VALUES('selfcheck:last', ?)", (json.dumps({k: v for k, v in out.items() if k != "misses"}),))
    return out


# ---------------------------------------------------------------------------------------------- scale / latency
def scale(sizes=(1000, 10000, 50000), queries: int = 40, embedder: str = "auto", log=lambda m: None) -> dict:
    """Search latency, write throughput, DB size and process memory as the store grows. Items are synthetic
    sentences with real keyword structure; vectors are random unit vectors of the embedder's dimension (scan cost
    doesn't depend on content), while each query is embedded for real, so latency includes the query embedding."""
    import random
    import resource
    import statistics as st
    from .embed import get_embedder
    from .store import Store
    from .embed import pack
    rng = random.Random(1)
    words = ("deploy payment webhook retry stripe postgres index cache latency cluster backup restore token auth key "
             "graph memory agent review queue schedule budget invoice tenant region gateway proxy tunnel vault secret "
             "dashboard metric alert incident rollback migration schema replica shard lease worker").split()
    e = get_embedder(None if embedder == "auto" else embedder)
    out = {"embedder": e.name if e else "none", "dim": e.dim if e else 0, "sizes": []}
    d, path = tempfile.mkdtemp(prefix="cscale-"), None
    try:
        path = os.path.join(d, "s.db")
        s = Store(path, embedder=e)
        s.defer_embed = True
        n = 0
        for target in sizes:
            t0 = time.time()
            with s.lock:
                s.db.execute("BEGIN")
                while n < target:
                    txt = " ".join(rng.choice(words) for _ in range(rng.randint(8, 30)))
                    iid = s.add_item("memory", f"note {n}: {txt}", project=f"p{n % 5}")["id"]
                    if e:
                        v = [rng.gauss(0, 1) for _ in range(e.dim)]
                        nv = sum(x * x for x in v) ** 0.5
                        s.db.execute("INSERT OR REPLACE INTO embeddings(item_id,model,vec) VALUES(?,?,?)",
                                     (iid, e.name, pack([x / nv for x in v])))
                    n += 1
                s.db.execute("COMMIT")
            write_s = time.time() - t0
            lat = []
            for q in range(queries):
                qtext = " ".join(rng.choice(words) for _ in range(rng.randint(2, 6)))
                t1 = time.perf_counter()
                s.search(qtext, limit=10, max_tokens=600)
                lat.append((time.perf_counter() - t1) * 1000)
            lat.sort()
            row = {"items": n, "search_p50_ms": round(st.median(lat), 1), "search_p95_ms": round(lat[int(len(lat) * 0.95) - 1], 1),
                   "insert_per_s": round((target - (out["sizes"][-1]["items"] if out["sizes"] else 0)) / max(write_s, 1e-6)),
                   "db_mb": round(os.path.getsize(path) / 1e6, 1),
                   "rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1e6 if sys.platform == "darwin" else 1e3), 1)}
            out["sizes"].append(row)
            log(json.dumps(row))
    finally:
        _cleanup(d)
    return out
