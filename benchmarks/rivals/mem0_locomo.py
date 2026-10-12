"""mem0 OSS on LoCoMo: ingest each conversation (LLM fact extraction), retrieve top-20 memories per question.
Writes one JSONL row per question; answering and judging happen in Commons' harness with identical prompts."""
import json, os, sys, tempfile, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from mem0 import Memory
DATA = Path.home() / "genesis/commons-bench-data/locomo10.json"
convs = [int(x) for x in sys.argv[1].split(",")]
out = open(sys.argv[2], "a")
for ci in convs:
    conv = json.load(open(DATA))[ci]
    d = tempfile.mkdtemp(prefix="mem0-")
    m = Memory.from_config({
        "llm": {"provider": "anthropic", "config": {"model": "claude-haiku-4-5", "api_key": os.environ["ANTHROPIC_API_KEY"],
                                                    "max_tokens": 2000, "temperature": 0.0}},
        "embedder": {"provider": "fastembed", "config": {"model": "BAAI/bge-small-en-v1.5"}},
        "vector_store": {"provider": "qdrant", "config": {"collection_name": f"locomo{ci}", "embedding_model_dims": 384,
                                                          "path": d, "on_disk": True}},
        "history_db_path": os.path.join(d, "history.db")})
    c, n, t0, chunks = conv["conversation"], 1, time.time(), []
    while f"session_{n}" in c:
        date = c.get(f"session_{n}_date_time", "")
        msgs = [{"role": "user", "content": f"[{date}] {t['speaker']}: {t['text']}"} for t in c[f"session_{n}"]]
        chunks += [msgs[k:k + 6] for k in range(0, len(msgs), 6)]
        n += 1
    uid = f"conv{ci}"
    for ch in chunks:  # in order: mem0 updates memories against what it already knows
        try:
            m.add(ch, user_id=uid)
        except Exception as e:  # noqa: BLE001
            print("add failed:", str(e)[:200], flush=True)
    print(f"conv {ci}: ingested {len(chunks)} chunks in {time.time()-t0:.0f}s, memories={len(m.get_all(filters={'user_id': uid}).get('results', []))}", flush=True)
    def one(qa):
        t = time.perf_counter()
        r = m.search(qa["question"], top_k=20, filters={"user_id": uid})
        ms = (time.perf_counter() - t) * 1000
        return qa, [x.get("memory", "") for x in r.get("results", [])], ms
    with ThreadPoolExecutor(4) as ex:
        for qa, mems, ms in ex.map(one, [q for q in conv["qa"] if q.get("category") in (1, 2, 3, 4) and "answer" in q]):
            out.write(json.dumps({"conv": ci, "cat": qa["category"], "question": qa["question"], "gold": str(qa["answer"]),
                                  "memories": mems, "ms": round(ms, 1)}) + "\n")
    out.flush()
