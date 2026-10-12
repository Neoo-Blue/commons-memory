"""Optional second-stage reranking over the top-N hybrid results (v0.8).

``COMMONS_RERANK``:
- ``off`` (default)
- ``cross-encoder``: local ONNX cross-encoder via fastembed (``COMMONS_RERANK_MODEL``, default
  Xenova/ms-marco-MiniLM-L-6-v2; e.g. BAAI/bge-reranker-base for higher quality)
- ``llm``: the provider-layer role ``reranker`` orders the candidates (budget-capped by ``COMMONS_RERANK_MAX_TOKENS``)
Callers may also force it per query (``search(rerank=True)``); failures fall back to the original order.
"""
from __future__ import annotations

import json
import os
import re

_CE: dict = {}


def mode() -> str:
    return os.environ.get("COMMONS_RERANK", "off").lower()


def _docs(store, ids):
    out = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in store.q(f"SELECT id, title, summary, content FROM items WHERE id IN ({','.join('?' * len(chunk))})", chunk):
            out[r["id"]] = (r["content"] or r["summary"] or r["title"] or "")[:2000]
    return out


def cross_encoder():
    name = os.environ.get("COMMONS_RERANK_MODEL", "Xenova/ms-marco-MiniLM-L-6-v2")
    if name not in _CE:
        from fastembed.rerank.cross_encoder import TextCrossEncoder
        _CE[name] = TextCrossEncoder(model_name=name)
    return _CE[name]


def rerank_ids(store, query: str, ids: list[str], top: int = 30, how: str | None = None) -> list[str]:
    how = (how or mode())
    if how in ("off", "", "none") or not ids or not query.strip():
        return ids
    head, tail = ids[:top], ids[top:]
    docs = _docs(store, head)
    head = [i for i in head if i in docs]
    try:
        if how in ("cross-encoder", "ce", "local", "on", "true", "1"):
            scores = list(cross_encoder().rerank(query, [docs[i] for i in head]))
            order = [i for _, i in sorted(zip(scores, head), key=lambda t: -t[0])]
        elif how == "llm":
            from .providers import ProviderLayer
            budget = int(os.environ.get("COMMONS_RERANK_MAX_TOKENS", "3000"))
            per = max(40, budget * 4 // max(1, len(head)))
            listing = "\n".join(f"[{n}] {docs[i][:per]}" for n, i in enumerate(head))
            out = ProviderLayer(store).call_role(
                "reranker", f"Query: {query}\n\nRank the passages by how well they help answer the query. "
                            f"Reply with JSON list of passage numbers, best first.\n\n{listing}", max_tokens=200)
            if not out:
                return ids
            nums = json.loads(re.search(r"\[[\d,\s]*\]", out).group(0))
            order = [head[n] for n in nums if 0 <= n < len(head)]
            order += [i for i in head if i not in order]
        else:
            return ids
    except Exception:  # noqa: BLE001 - reranking is best-effort
        return ids
    return order + tail
