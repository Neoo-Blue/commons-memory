# Retrieval: embedders, ranking signals, rerank, benchmarks (v0.8)

## Embedders
`COMMONS_EMBEDDER=auto` is the default. It resolves to:

1. **fastembed** when installed (`pip install "commons-memory[embed]"`; the Docker image bakes in the model).
   The default model is `BAAI/bge-small-en-v1.5`, with query instructions applied. Use `COMMONS_EMBED_MODEL` to
   pick another fastembed model, e.g. `intfloat/multilingual-e5-large` or `google/embeddinggemma-300m` for
   multilingual memory.
2. **openai** when `COMMONS_EMBED_URL` is set. This works with any OpenAI-compatible `/v1/embeddings` endpoint
   (OpenAI, Ollama, LM Studio, vLLM, llama.cpp). Set `COMMONS_EMBED_MODEL`, and put the key in
   `COMMONS_EMBED_KEY_REF` as a secret reference such as `env://OPENAI_API_KEY` or `vault://…`.
3. **hash** otherwise: dependency-free lexical hashing.

Vectors are stored per embedder (the model name is part of the key), so changing models never mixes vector spaces.
Two things fill in vectors for the new embedder: `commons reindex`, and the background `reindex` job, which handles
`COMMONS_REINDEX_BATCH` items per minute. Use `commons reindex --force` to recompute everything.

## Ranking
`search` and `search_index` fuse BM25 and vector ranks with weighted reciprocal-rank fusion, then apply these
multiplicative signals. All weights are env-tunable; pass `explain=true` to see the per-hit breakdown.

| Signal | Effect | Knob (default) |
|---|---|---|
| keyword / semantic weight | RRF weights | `COMMONS_RANK_KEYWORD` (1.0), `COMMONS_RANK_SEMANTIC` (1.0; 0.7 with hash) |
| entity boost | items linked to a graph entity named in the query | `COMMONS_RANK_ENTITY` (0.3) |
| time proximity | query says "last week", "3 months ago", "in May", "2024-05-07", "yesterday", "recently"… → items inside/near that window | `COMMONS_RANK_TIME` (0.6) |
| recency | gentle decay by age relative to `as_of` | `COMMONS_RANK_RECENCY` (0.1), `COMMONS_RANK_HALF_LIFE_DAYS` (30) |
| trust | author trust | `COMMONS_TRUST_WEIGHTING` (1) |

`as_of` sets the reference time for relative dates. For example, asking "what did I do last week?" about a past
conversation needs the date of that conversation as `as_of`.

## Rerank (optional)
- `COMMONS_RERANK=cross-encoder` uses a local ONNX cross-encoder via fastembed. The default model is
  `Xenova/ms-marco-MiniLM-L-6-v2`; `BAAI/bge-reranker-base` is better but slower.
- `COMMONS_RERANK=llm` uses the provider role `reranker` and is budget-capped by `COMMONS_RERANK_MAX_TOKENS`.
- You can also turn it on per query with `search(rerank=true)`. On any failure it falls back to the hybrid order.

## Benchmarks
`commons bench longmemeval|locomo --data FILE` measures **retrieval only**. Each question gets a fresh store
holding its haystack, and `Store.rank` (the ranking behind `search`) is checked for the evidence in the top k.
The command writes per-question JSONL plus a summary. See [benchmarks/README.md](../benchmarks/README.md) for
the published numbers and how to reproduce them.
