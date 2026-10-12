"""TypeSafe (jev) as the QA judge vs the Haiku judge on Commons' graded LoCoMo rows; Sonnet 5.5 arbitrates disagreements.
Run: sesame run typesafe -- sesame run anthropic -- python3 ts_judge.py <graded.jsonl> [n]"""
import json, os, random, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

rows = [json.loads(l) for l in open(sys.argv[1])]
random.seed(7)
rows = random.sample(rows, int(sys.argv[2]) if len(sys.argv) > 2 else 300)
INSTR = ("Does the generated answer state the same fact as the gold answer (same entity, number, or the same date or "
         "time period, even in another format or with extra words)? Answer no if it contradicts the gold answer, misses "
         "the key fact, or says it doesn't know.")


def post(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers | {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def typesafe(r):
    t = time.perf_counter()
    d = post("https://api.typesafe.ai/v1/systemone",
             {"state": f"Question: {r['q']}\nGold answer: {r['gold']}\nGenerated answer: {r['answer']}", "model": "jev-latest",
              "questions": {"same": {"type": "noul", "instructions": INSTR}}},
             {"Authorization": "Bearer " + os.environ["TYPESAFE_API_KEY"]})
    return d["answers"]["same"]["noul"], (time.perf_counter() - t) * 1000, d.get("usage", {})


def sonnet(r):
    d = post("https://api.anthropic.com/v1/messages", {
        "model": "claude-sonnet-5-5", "max_tokens": 600,
        "system": "Grade a long-term-memory benchmark answer. Be generous about format: same entity, number or date/period "
                  "counts as CORRECT. WRONG if it contradicts the gold answer, misses the key fact, or says it doesn't know. "
                  "Reply with exactly CORRECT or WRONG.",
        "messages": [{"role": "user", "content": f"Question: {r['q']}\nGold answer: {r['gold']}\nGenerated answer: {r['answer']}"}]},
        {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"})
    return "CORRECT" in "".join(c.get("text", "") for c in d["content"] if c.get("type") == "text").upper()


with ThreadPoolExecutor(8) as ex:
    ts = list(ex.map(typesafe, rows))
agree, lat, tok, dis = 0, [], 0, []
for r, (p, ms, u) in zip(rows, ts):
    ok = p >= 0.5
    lat.append(ms)
    tok += u.get("input_tokens", 0) + u.get("output_tokens", 0)
    if ok == bool(r["correct"]):
        agree += 1
    else:
        dis.append((r, p))
with ThreadPoolExecutor(8) as ex:
    arb = list(ex.map(lambda x: sonnet(x[0]), dis))
ts_right = sum(a == (p >= 0.5) for (r, p), a in zip(dis, arb))
lat.sort()
print(json.dumps({"n": len(rows), "agreement_with_haiku": round(agree / len(rows), 3), "disagreements": len(dis),
                  "sonnet_sides_with_typesafe": ts_right, "sonnet_sides_with_haiku": len(dis) - ts_right,
                  "typesafe_p50_ms": round(lat[len(lat) // 2]), "typesafe_p95_ms": round(lat[int(len(lat) * .95)]),
                  "typesafe_tokens": tok}, indent=1))
for (r, p), a in list(zip(dis, arb))[:6]:
    print(f"- ts={p:.2f} haiku={'C' if r['correct'] else 'W'} sonnet={'C' if a else 'W'} | gold={r['gold'][:50]!r} ans={r['answer'][:70]!r}")
