"""Pluggable embedding providers.

- ``hash`` (default): dependency-free feature-hashing embedder (words + char trigrams).
  Fast, deterministic, fully local; gives fuzzy lexical/semantic-ish matching.
- ``fastembed``: small local ONNX model (BAAI/bge-small-en-v1.5) if ``pip install commons-memory[embed]``.
- ``none``: disable vectors, keyword (FTS5) only.
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


class FastEmbedEmbedder(Embedder):
    name = "fastembed"

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5"):
        from fastembed import TextEmbedding  # optional dependency

        self._m = TextEmbedding(model)
        self.dim = len(next(iter(self._m.embed(["probe"]))))

    def embed(self, texts):
        return [list(map(float, v)) for v in self._m.embed(texts)]


def get_embedder(name: str | None = None) -> Embedder | None:
    name = (name or os.environ.get("COMMONS_EMBEDDER", "hash")).lower()
    if name in ("none", "off", ""):
        return None
    if name == "fastembed":
        return FastEmbedEmbedder()
    return HashEmbedder()


def pack(v: list[float]) -> bytes:
    return array("f", v).tobytes()


def unpack(b: bytes) -> list[float]:
    a = array("f")
    a.frombytes(b)
    return a.tolist()


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))
