"""Answer + judge with Commons' bench prompts over memories retrieved by ANOTHER system (one JSONL row per question:
question, gold, memories, cat, conv). Same reader, judge, prompts, "Today is" date, k and judge fallback as
`commons bench qa-locomo`. Rows are graded independently and appended as they finish, so a crash keeps paid work;
a re-run skips rows already graded."""
import json, re, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from commons_memory import bench as B
from commons_memory.providers import ProviderLayer
from commons_memory.store import Store

src, model = sys.argv[1], sys.argv[3]
out = Path(src.replace(".jsonl", ".graded.jsonl"))
rows = [json.loads(l) for l in open(src)]
done = {(r["conv"], r["question"]) for r in map(json.loads, out.read_text().splitlines())} if out.exists() else set()
convs = json.load(open(Path.home() / "genesis/commons-bench-data/locomo10.json"))


def label(ci):
    c, n = convs[ci]["conversation"], 1
    while f"session_{n}" in c:
        n += 1
    ref = max(B._locomo_date(c.get(f"session_{i}_date_time", "")) for i in range(1, n)) + 86400
    return time.strftime("%d %B %Y", time.gmtime(ref))


pl = ProviderLayer(Store(sys.argv[2]))


def one(r):
    try:
        prompt = (f"Today is {label(r['conv'])}.\nMemories:\n" + "\n".join(f"- {m[:700]}" for m in r["memories"][:20])
                  + f"\n\nQuestion: {r['question']}\nAnswer:")
        a = pl.chat(model, prompt, system="ROLE: reader.", cache_prefix=B.QA_READER, max_tokens=200, role="grader")
        ans = (a["text"] or "").strip()
        j = pl.chat(model, f"Question: {r['question']}\nGold answer: {r['gold']}\nGenerated answer: {ans}",
                    system="ROLE: grader.", cache_prefix=B.QA_JUDGE, max_tokens=60, role="grader", schema=B.QA_SCHEMA)
        try:
            ok = json.loads(re.search(r"\{.*\}", j["text"], re.S).group(0))["verdict"] == "CORRECT"
        except (AttributeError, ValueError, KeyError):
            ok = "CORRECT" in (j["text"] or "").upper() and "WRONG" not in (j["text"] or "").upper()
        return {**{k: r[k] for k in ("conv", "cat", "question", "gold")}, "answer": ans, "correct": ok,
                "cost": a.get("cost", 0) + j.get("cost", 0)}
    except Exception as e:  # noqa: BLE001 - one bad call must not lose the batch; a re-run retries it
        print("row failed:", str(e)[:200], file=sys.stderr)
        return None


todo = [r for r in rows if (r["conv"], r["question"]) not in done]
with ThreadPoolExecutor(8) as ex, open(out, "a") as f:
    for g in ex.map(one, todo):
        if g:
            f.write(json.dumps(g) + "\n")
            f.flush()
res = [json.loads(l) for l in out.read_text().splitlines()]
by = {}
for g in res:
    by.setdefault(f"cat{g['cat']}", []).append(g["correct"])
print(json.dumps({"n": len(res), "of": len(rows), "accuracy": round(sum(g["correct"] for g in res) / len(res), 4),
                  "cost": round(sum(g.get("cost", 0) for g in res), 3),
                  "by_type": {k: round(sum(v) / len(v), 4) for k, v in sorted(by.items())}}))
