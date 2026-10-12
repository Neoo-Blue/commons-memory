"""Pluggable embedding providers.

- ``auto`` (default): ``fastembed`` when installed (``pip install commons-memory[embed]``), else ``openai`` when
  ``COMMONS_EMBED_URL`` is set, else ``hash``.
- ``fastembed``: local ONNX model, ``COMMONS_EMBED_MODEL`` (default BAAI/bge-small-en-v1.5; e.g.
  google/embeddinggemma-300m or intfloat/multilingual-e5-large for multilingual).
- ``openai``: any OpenAI-compatible ``/v1/embeddings`` endpoint (OpenAI, Ollama, LM Studio, vLLM, llama.cpp):
  ``COMMONS_EMBED_URL`` (base URL), ``COMMONS_EMBED_MODEL``, key via ``COMMONS_EMBED_KEY_REF`` (secret reference).
- ``hash``: dependency-free feature-hashing embedder (words + char trigrams); lexical-ish fallback.
- ``none``: disable vectors, keyword (FTS5) only.
Vectors are stored per embedder name (which includes the model), so switching models never mixes vector spaces;
``commons reindex`` (or the background job) fills vectors for the active embedder.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from array import array

_WORD = re.compile(r"[A-Za-z0-9_]+")


class Embedder:
    name = "none"
    dim = 0

    def embed(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover - interface
        raise NotImplementedError

    def embed_query(self, text: str) -> list[float]:
        return self.embed([text])[0]


class HashEmbedder(Embedder):
    name = "hash"

    def __init__(self, dim: int = 384):
        self.dim = dim

    def _features(self, text: str):
        words = [w.lower() for w in _WORD.findall(text)]
        for w in words:
            # split snake/camel case so code identifiers match natural words
            yield w, 1.0
            for part in re.findall(r"[a-z]+|[0-9]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", w)):
                if part != w:
                    yield part, 0.5
            padded = f"#{w}#"
            for i in range(len(padded) - 2):
                yield "3:" + padded[i:i + 3], 0.3
        for a, b in zip(words, words[1:]):
            yield f"2:{a} {b}", 0.5

    def embed(self, texts):
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for feat, w in self._features(t):
                h = int.from_bytes(hashlib.blake2b(feat.encode(), digest_size=8).digest(), "little")
                v[h % self.dim] += w if (h >> 63) & 1 else -w
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


DEFAULT_FASTEMBED = "BAAI/bge-small-en-v1.5"
_CACHE: dict[str, Embedder] = {}


class FastEmbedEmbedder(Embedder):
    def __init__(self, model: str | None = None):
        from fastembed import TextEmbedding  # optional dependency

        self.model = model or os.environ.get("COMMONS_EMBED_MODEL") or DEFAULT_FASTEMBED
        th = os.environ.get("COMMONS_EMBED_THREADS")
        self._m = TextEmbedding(self.model, **({"threads": int(th)} if th else {}))
        self._qcache: dict[str, list[float]] = {}
        self.dim = len(next(iter(self._m.embed(["probe"]))))
        # legacy name for the original default so existing vectors stay valid
        self.name = "fastembed" if self.model == DEFAULT_FASTEMBED else f"fastembed:{self.model}"

    def embed(self, texts):
        return [list(map(float, v)) for v in self._m.embed(list(texts), batch_size=16)]

    def embed_query(self, text):
        # agents repeat queries (pickup, resume, hooks): a small cache skips the ~10-20 ms model call
        hit = self._qcache.get(text)
        if hit is None:
            qe = getattr(self._m, "query_embed", None)  # model-specific query instruction (bge, e5, ...)
            hit = list(map(float, next(iter(qe([text]))) if qe else next(iter(self._m.embed([text])))))
            if len(self._qcache) >= 512:
                self._qcache.pop(next(iter(self._qcache)))
            self._qcache[text] = hit
        return list(hit)


class OpenAICompatEmbedder(Embedder):
    def __init__(self, url: str | None = None, model: str | None = None, key: str | None = None):
        self.url = (url or os.environ.get("COMMONS_EMBED_URL", "")).rstrip("/")
        if not self.url:
            raise ValueError("COMMONS_EMBED_URL is required for the openai embedder")
        if not self.url.endswith("/embeddings"):
            self.url += "/embeddings" if self.url.endswith("/v1") else "/v1/embeddings"
        self.model = model or os.environ.get("COMMONS_EMBED_MODEL", "text-embedding-3-small")
        self.key = key if key is not None else _resolve_ref(os.environ.get("COMMONS_EMBED_KEY_REF", ""))
        self.name = f"openai:{self.model}"
        self.dim = len(self.embed(["probe"])[0])

    def embed(self, texts):
        import json
        import urllib.request
        out: list[list[float]] = []
        texts = list(texts)
        for i in range(0, len(texts), 128):
            body = json.dumps({"model": self.model, "input": texts[i:i + 128]}).encode()
            h = {"Content-Type": "application/json"} | ({"Authorization": f"Bearer {self.key}"} if self.key else {})
            req = urllib.request.Request(self.url, data=body, headers=h, method="POST")
            with urllib.request.urlopen(req, timeout=120) as r:
                data = sorted(json.loads(r.read())["data"], key=lambda d: d["index"])
            for d in data:
                v = d["embedding"]
                n = math.sqrt(sum(x * x for x in v)) or 1.0
                out.append([x / n for x in v])
        return out


def _resolve_ref(ref: str) -> str:
    if not ref:
        return ""
    if ref.startswith("env://"):
        return os.environ.get(ref[6:], "")
    try:
        from .secrets import _VAULT, EnvBackend, InfisicalBackend, OnePasswordBackend
        b = {x.scheme: x for x in (EnvBackend(), _VAULT, OnePasswordBackend(), InfisicalBackend())}[ref.split("://", 1)[0]]
        return b.resolve(ref)
    except Exception:  # noqa: BLE001
        return ""


def fastembed_available() -> bool:
    import importlib.util
    return importlib.util.find_spec("fastembed") is not None


def get_embedder(name: str | None = None) -> Embedder | None:
    name = (name or os.environ.get("COMMONS_EMBEDDER", "auto")).lower()
    if name in ("none", "off", ""):
        return None
    if name == "auto":
        if fastembed_available():
            name = "fastembed"
        elif os.environ.get("COMMONS_EMBED_URL"):
            name = "openai"
        else:
            name = "hash"
    key = f"{name}|{os.environ.get('COMMONS_EMBED_MODEL', '')}|{os.environ.get('COMMONS_EMBED_URL', '')}"
    if key in _CACHE:
        return _CACHE[key]
    try:
        if name.startswith("fastembed"):
            e: Embedder = FastEmbedEmbedder(name.split(":", 1)[1] if ":" in name else None)
        elif name.startswith("openai"):
            e = OpenAICompatEmbedder(model=name.split(":", 1)[1] if ":" in name else None)
        else:
            e = HashEmbedder()
    except Exception as ex:  # noqa: BLE001 - model download/endpoint failure: degrade, don't crash
        import sys
        print(f"commons: embedder {name} unavailable ({ex}); falling back to hash", file=sys.stderr)
        e = HashEmbedder()
    _CACHE[key] = e
    return e


def pack(v: list[float]) -> bytes:
    return array("f", v).tobytes()


def unpack(b: bytes) -> list[float]:
    a = array("f")
    a.frombytes(b)
    return a.tolist()


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))
