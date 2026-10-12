# Commons benchmarks

Every number below is reproducible with `commons bench ...`; per-question results and summaries are committed under
[`results/`](results/). Measurements were taken on an 8-core Apple M1 laptop that was running other jobs at the same
time, so latencies are conservative.

## v0.9.0

### Retrieval: LoCoMo (10 conversations, 1,982 questions with evidence)
Same protocol as v0.8 (one store per conversation, `Store.rank`, fastembed bge-small, no rerank, no LLM). The only
change is the fusion method: normalised-score fusion instead of reciprocal-rank fusion. No weights were tuned.

| Config | turn any@1 | turn any@5 | **turn any@10** | **session any@10** |
|---|---|---|---|---|
| v0.9 default (score fusion) | **39.4** | **66.8** | **76.5** | **97.4** |
| v0.8 default (RRF k=60 + signals) | 31.2 | 60.4 | 71.1 | 95.8 |
| v0.8 with cross-encoder rerank | 49.4 | 69.7 | 75.1 | 95.8 |

### End-to-end QA: LoCoMo categories 1-4 (1,540 questions)
`commons bench qa-locomo --model anthropic/claude-haiku-4-5 --k 20`: the top 20 memories (10 per speaker, as in
mem0's evaluation) go to a reader model, and a judge model grades the answer against the gold answer, generously
with paraphrase and date formats. Total cost $2.56.

| Category | n | Accuracy |
|---|---|---|
| single-hop (cat 4) | 841 | 78.6 |
| temporal (cat 2) | 321 | 68.9 |
| multi-hop (cat 1) | 282 | 53.2 |
| open-domain (cat 3) | 96 | 39.6 |
| **overall** | 1,540 | **69.5** |

Published figures use other readers and judges (mem0's paper: gpt-4o-mini, mem0 66.9%, Zep 66.0%, full-context
72.9%), so they are not comparable; the head-to-head below runs mem0 here instead. LongMemEval-S QA on a seeded
100-question sample, same reader/judge: **62.0%** ($0.35; strongest on single-session-user 94%, weakest on
single-session-assistant 45% and multi-session 50%).

### Head-to-head: the rivals run here, same data, same metric (2026-10-12)
Every system ingested the same LoCoMo turns on the same machine. Harnesses are in [`rivals/`](rivals/), per-question
results in [`results/v0.9.0/rivals/`](results/v0.9.0/rivals/).

**End-to-end QA vs mem0** (LoCoMo cats 1-4, 1,540 questions). mem0 OSS ingested each conversation in 6-turn chunks
with its LLM fact extraction (Claude Haiku 4.5), fastembed bge-small and local Qdrant, and returned its top 20
memories per question. Its memories and Commons' top 20 went through the *same* reader, judge, prompts and "Today
is" date line (Claude Haiku 4.5, [`rivals/qa_external.py`](rivals/qa_external.py)).

| System | overall | multi-hop | temporal | open-domain | single-hop | LLM calls at ingest | search p50 |
|---|---|---|---|---|---|---|---|
| mem0 OSS | **70.3** | **61.0** | **73.8** | 37.5 | 75.9 | ~1 per 6 turns (~10 min per conversation) | 377 ms |
| Commons 0.9 | 69.5 | 53.2 | 68.9 | **39.6** | **78.6** | none | 24-68 ms |

Paired on the same questions: Commons right and mem0 wrong 188 times, the reverse 203 times (sign test p = 0.45),
so overall accuracy is a **statistical tie**. mem0's LLM-distilled facts win multi-hop and temporal questions;
Commons' raw turns win single-hop and open-domain, with no ingest model, ~6-15x faster search and nothing to pay
per write.

**Retrieval vs MemPalace and claude-mem** (LoCoMo, 1,986 questions, MemPalace's own metric: per question, the
fraction of evidence turns or sessions in the top 10, averaged; MemPalace measured by its own
`benchmarks/locomo_bench.py`, claude-mem 13.36 through its real worker HTTP API on an isolated port).

| System | dialog top-10 | session top-10 | turn any@10 | session any@10 | search time |
|---|---|---|---|---|---|
| **Commons 0.9** | **0.708** | **0.945** | **76.5** | **97.4** | 24.5 ms p50 (1k items) |
| MemPalace (hybrid) | 0.545 | 0.889 | - | - | 0.71-0.91 s per question (harness) |
| claude-mem 13.36 | 0.496 | 0.811 | 54.3 | 85.3 | 2,726 ms p50 (HTTP API) |

### Storage engine: SQLite vs Postgres + pgvector (`commons bench scale --target postgresql://...`)
Same synthetic store, same machine (an oversubscribed 8-core Mac, load ~33, so compare rows, not absolutes).
`conc_qps` = searches per second with 8 searching threads while 2 threads keep writing.

| Backend | items | search p50 | inserts/s | conc_qps |
|---|---|---|---|---|
| SQLite (WAL) | 10,000 | 84-230 ms | 671-995 | 10-17 |
| Postgres 17 + pgvector HNSW | 10,000 | 633 ms | 159 | 1.5 |
| SQLite (WAL) | 50,000 | 154 ms | 925 | 5.9 |
| Postgres 17 + pgvector HNSW | 50,000 | 628 ms | 175 | 1.9 |

Postgres as wired (one connection, SQLite dialect translated per query, many small queries per search) is 4-10x
slower; it is there for sharing one database between hosts, not for speed. A cProfile of one search on an idle
10k-item SQLite store: 23 ms total, of which SQL ~13 ms, exact vector matmul ~4 ms, query embedding ~4 ms. Exact
numpy search stays cheap to a few hundred thousand memories; an ANN index only pays beyond that.

### TypeSafe (jev) for cheap judgments
- **QA judge** (300 Commons QA rows): 94.7% agreement with the Haiku judge; on the 16 disagreements Sonnet 5.5 sided
  with Haiku 11 times, so benchmarks keep the Haiku judge. TypeSafe answered in 154 ms p50.
- **Free-text contradictions** (96 labeled note pairs written by Sonnet 5.5: contradiction / consistent /
  related-but-compatible): TypeSafe precision 1.00, recall 0.97 at 139 ms p50; Claude Haiku 5.5 precision 1.00,
  recall 0.97 at 927 ms. The librarian now uses it when `TYPESAFE_API_KEY` is set
  ([`results/v0.9.0/typesafe/`](results/v0.9.0/typesafe/), [`rivals/ts_contra.py`](rivals/ts_contra.py)).

### Live multi-agent run ([`live_agents.py`](live_agents.py))
Real agents connected to the hub, each asked through `delegate` to recover a random nonce that was planted with a
different tool, so a correct answer proves it used the shared hub. grok-build and antigravity run
`commons agent daemon` (instant wake); muse is a chat AI that polls on its own schedule.

| Scenario | grok-build (Grok 4.6) | antigravity (Gemini 3) | muse (Muse Spark) |
|---|---|---|---|
| ping (delivery + wake) | ok, 36-128 s | ok, 15-53 s | ok, 183 s |
| recall (`remember` -> `search`) | ok, 220 s | ok, 155 s | ok, 187 s |
| why (`record_decision` -> `why`) | ok, 220 s | ok, 158 s | ok, 192 s |
| resume (`handoff` -> `resume`) | ok, 79 s | ok, 18 s | ok, 309 s |
| code + review job (`cross_review`) | wrote `is_leap`, 2 approvals, passes 8/8 cases, 201 s | (reviewer) | - |

Times are wall clock including each agent's own model run. The first resume round exposed two real problems, both
fixed (the row above is the re-run): `handoff` did not return its `session_id`, and four concurrent handoffs without one shared a session. The
user's `grok` chat bot was offline for the whole run and answered nothing.

### Latency and footprint (`commons bench scale`)
Synthetic memories with real keyword structure; each query is embedded for real, so latency includes the embedding.

| Store size | search p50 | search p95 | inserts/s | DB size | process RSS |
|---|---|---|---|---|---|
| 1,000 | 24.5 ms | 45.4 ms | 2,186 | 4.4 MB | 222 MB |
| 10,000 | 48.7 ms | 102.8 ms | 2,583 | 37.2 MB | 222 MB |
| 30,000 | 67.6 ms | 118.7 ms | 2,371 | 111 MB | 306 MB |
| 60,000 | 114.0 ms | 253.0 ms | 2,117 | 220 MB | 415 MB |

v0.8 at 30,000 items: 1,383 ms p50 (pure-python cosine over every vector).

### Context cost
Tool schemas every connected client pays for, per session (cl100k tokens): v0.9 core profile **2,970** (21 tools);
v0.9 all tools 21,526; v0.8 33,057 (148 tools).

### Self-check on your own data (`commons bench self`)
Samples your stored memories, has a model write a paraphrased question each one answers, and checks the memory
comes back. On a 166-memory curated sample: hit@1 93%, hit@5 100%, MRR 0.96 ($0.04).

---

# v0.8.0 results (kept for comparison)


Commons v0.8.0, measured 2026-10-11 on an 8-core CPU box. There is **no GPU and no LLM** anywhere in the pipeline.

These numbers measure **retrieval only**. Each question gets a fresh Commons store holding its haystack, and we check
whether the evidence is in the top k returned by `Store.rank`, the ranking behind `search`. End-to-end QA accuracy,
where a reader LLM answers from the retrieved context, is **not** measured here.

Per-question results (`*.jsonl`) and the summaries are in [`results/v0.8.0/`](results/v0.8.0/).

## Reproduce
```bash
pip install "commons-memory[embed]"
curl -LO https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json
curl -LO https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json
commons bench longmemeval --data longmemeval_s_cleaned.json --embedder fastembed --granularity turn
commons bench locomo --data locomo10.json --embedder fastembed [--rerank]
```

## LongMemEval-S (cleaned): 470 questions with evidence
The 30 abstention (`_abs`) questions are excluded because they have no evidence session. Recall is measured at the
session level:
- `any@k`: at least one evidence session is in the top k.
- `all@k`: every evidence session is in the top k.

| Config | any@1 | any@3 | **any@5** | all@5 | any@10 | all@10 |
|---|---|---|---|---|---|---|
| fastembed bge-small, one item per user turn (default) | 90.4 | 96.8 | **97.7** | 91.3 | 99.6 | 95.5 |
| hash embedder (no ML deps), per turn | 76.6 | 87.7 | 93.6 | 77.7 | 97.9 | 89.2 |
| hash embedder, one item per session | 73.4 | 88.3 | 92.8 | 76.0 | 97.4 | 89.8 |

any@5 by question type for the default config:

| Question type | any@5 |
|---|---|
| knowledge-update | 100 |
| multi-session | 99.2 |
| single-session-assistant | 100 |
| single-session-user | 96.9 |
| temporal-reasoning | 96.1 |
| single-session-preference | 90.0 |

## LoCoMo (10 conversations, 1,982 questions with evidence, all categories)
- **Turn-level** recall: the exact evidence dialog turn (`D3:7`) is in the top k. This is strict.
- **Session-level** recall: the session containing the evidence is in the top k.

| Config | turn R@1 | turn R@5 | **turn R@10** | **session R@10** |
|---|---|---|---|---|
| fastembed + cross-encoder rerank (`--rerank`, ms-marco-MiniLM-L-6) | 49.4 | 69.7 | **75.1** | 95.8 |
| fastembed (default) | 31.2 | 60.4 | 71.1 | 95.8 |
| fastembed, v0.8 signals off (entity/time/recency = 0) | 34.8 | 60.0 | 70.0 | 95.4 |
| hash embedder | 26.9 | 50.4 | 61.3 | 94.2 |

## How this compares with published claims (read the caveats)
| System | Claim | Commons v0.8.0 |
|---|---|---|
| MemPalace | LongMemEval R@5 96.6% (raw), 98.4% (hybrid, held-out), ≥99% (with rerank) | **97.7%** any@5, default config, no rerank |
| MemPalace | LoCoMo R@10 88.9% | 75.1% turn-level with rerank / **95.8% session-level** |
| mem0 (managed platform) | LoCoMo 92.5, LongMemEval 94.4 | not comparable (see below) |

Caveats:
- **The metrics aren't identical.** We use LongMemEval session-level `recall_any@5` on the 470 non-abstention
  questions. MemPalace's exact protocol (question subset, granularity, chunking) may differ. Our LoCoMo turn-level
  number is stricter than session-level, and we don't know which granularity MemPalace's 88.9% uses. That's why
  both are reported.
- **mem0's figures are end-to-end QA accuracy** (LLM-judged answers) from a managed platform. Retrieval recall is
  a different quantity, so there's no direct comparison.
- **The v0.8 ranking signals are not a uniform win on LoCoMo.** They add about +1 R@10 but lose about 3.6 R@1,
  because recency favours newer turns while many LoCoMo questions ask about older events. Weights are untuned
  defaults; we did not tune on these test sets. Recency can be disabled with `COMMONS_RANK_RECENCY=0`.
- Each question runs against an isolated store with only its own haystack, as in the benchmarks' protocol.
  Real deployments hold far more data.
- The session-granularity fastembed run was stopped for time, so only the hash session-level run is reported.
