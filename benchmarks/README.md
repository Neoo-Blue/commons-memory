# Commons retrieval benchmarks

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
