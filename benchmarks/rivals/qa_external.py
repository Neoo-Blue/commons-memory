"""Answer + judge with Commons' bench prompts over memories retrieved by ANOTHER system (one JSONL row per question:
question, gold, memories, cat, conv). Same reader, judge, prompts and k as `commons bench qa-locomo`."""
import json, sys
from concurrent.futures import ThreadPoolExecutor
from commons_memory import bench as B
from commons_memory.providers import ProviderLayer
from commons_memory.store import Store
rows = [json.loads(l) for l in open(sys.argv[1])]
pl, model = ProviderLayer(Store(sys.argv[2])), sys.argv[3]
def one(r):
    prompt = "Memories:\n" + "\n".join(f"- {m[:700]}" for m in r["memories"][:20]) + f"\n\nQuestion: {r['question']}\nAnswer:"
    a = pl.chat(model, prompt, system="ROLE: reader.", cache_prefix=B.QA_READER, max_tokens=200, role="grader")
    j = pl.chat(model, f"Question: {r['question']}\nGold answer: {r['gold']}\nGenerated answer: {a['text'].strip()}",
                system="ROLE: grader.", cache_prefix=B.QA_JUDGE, max_tokens=60, role="grader", schema=B.QA_SCHEMA)
    return r, a["text"].strip(), json.loads(j["text"])["verdict"] == "CORRECT", a.get("cost", 0) + j.get("cost", 0)
with ThreadPoolExecutor(8) as ex:
    res = list(ex.map(one, rows))
by, cost = {}, 0
for r, ans, ok, c in res:
    by.setdefault(f"cat{r['cat']}", []).append(ok)
    cost += c
n = len(res)
print(json.dumps({"n": n, "accuracy": round(sum(ok for *_, ok, _c in res) / n, 4), "cost": round(cost, 3),
                  "by_type": {k: round(sum(v) / len(v), 4) for k, v in sorted(by.items())}}))
with open(sys.argv[1].replace(".jsonl", ".graded.jsonl"), "w") as f:
    for r, ans, ok, c in res:
        f.write(json.dumps({**{k: r[k] for k in ("conv", "cat", "question", "gold")}, "answer": ans, "correct": ok}) + "\n")
