"""claude-mem (isolated worker, real search API) on LoCoMo: same turns, same questions, same metrics as Commons."""
import json, re, sys, time, urllib.parse, urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
B = "http://127.0.0.1:37990"
DATA = Path.home() / "genesis/commons-bench-data/locomo10.json"
OUT = Path(sys.argv[1])
def post(path, body):
    req = urllib.request.Request(B + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=120).read())
def search(q, project, limit=60):
    u = B + "/api/search/observations?" + urllib.parse.urlencode({"query": q, "project": project, "limit": limit,
                                                                 "orderBy": "relevance", "format": "json"})
    d = json.loads(urllib.request.urlopen(u, timeout=120).read())
    # 13.x answers with the MCP text payload (a markdown table, relevance order) even for format=json
    text = "".join(c.get("text", "") for c in d.get("content", [])) if isinstance(d, dict) else ""
    return re.findall(r"^\| #\d+ \| [^|]* \| [^|]* \| (D\d+:\d+) \|", text, re.M)
rows, t0 = [], time.time()
convs = json.load(open(DATA))
for ci, conv in enumerate(convs):
    proj, c, n = f"locomo-c{ci}", conv["conversation"], 1
    turns = []
    while f"session_{n}" in c:
        date = c.get(f"session_{n}_date_time", "")
        for t in c[f"session_{n}"]:
            turns.append({"title": t["dia_id"], "project": proj,
                          "text": f"[{date}] {t['speaker']}: {t['text']}" + (f" [shares image: {t['blip_caption']}]" if t.get("blip_caption") else "")})
        n += 1
    if "--search-only" not in sys.argv:
        with ThreadPoolExecutor(8) as ex:
            list(ex.map(lambda b: post("/api/memory/save", b), turns))
        time.sleep(2)
    def one(qa):
        lat = time.perf_counter()
        got = search(qa["question"], proj)
        return qa, got, (time.perf_counter() - lat) * 1000
    with ThreadPoolExecutor(4) as ex:
        for qa, got, ms in ex.map(one, conv["qa"]):
            rows.append({"conv": ci, "cat": qa["category"], "evidence": qa.get("evidence") or [], "top": got[:60], "ms": round(ms, 1)})
    print(f"conv {ci}: {len(turns)} turns saved, {len(rows)} questions so far, {time.time()-t0:.0f}s", flush=True)
OUT.write_text("\n".join(json.dumps(r) for r in rows))
def sess_of(d): m = re.match(r"D(\d+):", d or ""); return f"S{m.group(1)}" if m else None
for k in (1, 5, 10):
    ev = [r for r in rows if r["evidence"]]
    any_k = sum(bool(set(r["evidence"]) & set(r["top"][:k])) for r in ev) / len(ev)
    print(f"claude-mem turn any@{k}: {100*any_k:.1f}")
ev = [r for r in rows if r["evidence"]]
def top_sess(r):
    out = []
    for t in r["top"]:
        s_ = sess_of(t)
        if s_ and s_ not in out: out.append(s_)
    return out
print(f"claude-mem session any@10: {100*sum(bool({sess_of(e) for e in r['evidence']} & set(top_sess(r)[:10])) for r in ev)/len(ev):.1f}")
mp_d = [1.0 if not r["evidence"] else sum(e in r["top"][:10] for e in set(r["evidence"])) / len(set(r["evidence"])) for r in rows]
mp_s = [1.0 if not r["evidence"] else sum(s_ in top_sess(r)[:10] for s_ in {sess_of(e) for e in r["evidence"]}) / len({sess_of(e) for e in r["evidence"]}) for r in rows]
print(f"claude-mem MemPalace-metric dialog top-10: {sum(mp_d)/len(mp_d):.3f}  session top-10: {sum(mp_s)/len(mp_s):.3f}")
ms = sorted(r["ms"] for r in rows)
print(f"claude-mem search latency p50 {ms[len(ms)//2]:.0f} ms p95 {ms[int(len(ms)*.95)]:.0f} ms (per conversation store, ~600 items)")
