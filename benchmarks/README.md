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

For orientation only: mem0's paper (gpt-4o-mini as reader and judge) reports mem0 66.9%, mem0 with graph 68.4%,
Zep 66.0%, LangMem 58.1% and full-context 72.9%. Our reader and judge are Claude Haiku 4.5 (the OpenRouter key we
planned to use for gpt-4o-mini had hit its monthly cap), so this is close to, not exactly, their protocol. mem0's
managed platform and Zep publish 92-95% under their own setups. One retrieval pass at k=20 is weakest on multi-hop
and open-domain questions.

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
