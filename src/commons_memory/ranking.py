"""Retrieval ranking signals (v0.8).

Hybrid search fuses BM25 (FTS) and vector ranks with weighted reciprocal-rank fusion, then applies
multiplicative signals:

- **entity boost**: items linked (graph relations) to an entity named in the query;
- **time proximity**: when the query contains a time expression ("last week", "3 months ago", "in May",
  "on 2023-05-07", "yesterday"), items whose timestamp falls inside / near that window are boosted;
- **recency**: a gentle exponential decay by age relative to ``as_of`` (newer facts win ties, which helps
  knowledge-update questions);
- **trust**: provenance weighting by author trust (existing).

Every weight is an env knob (``COMMONS_RANK_*``) and ``search(explain=True)`` returns the per-signal breakdown.
"""
from __future__ import annotations

import calendar
import datetime as dt
import math
import os
import re

STOPWORDS = set(["a", "about", "above", "after", "again", "against", "all", "am", "an", "and", "any", "are", "as", "at", "be", "because", "been", "before", "being", "below", "between", "both", "but", "by", "can", "could", "did", "do", "does", "doing", "down", "during", "each", "few", "for", "from", "further", "had", "has", "have", "having", "he", "her", "here", "hers", "herself", "him", "himself", "his", "how", "i", "if", "in", "into", "is", "it", "its", "itself", "just", "me", "more", "most", "my", "myself", "no", "nor", "not", "now", "of", "off", "on", "once", "only", "or", "other", "our", "ours", "ourselves", "out", "over", "own", "same", "she", "should", "so", "some", "such", "than", "that", "the", "their", "theirs", "them", "themselves", "then", "there", "these", "they", "this", "those", "through", "to", "too", "under", "until", "up", "very", "was", "we", "were", "what", "when", "where", "which", "while", "who", "whom", "why", "will", "with", "would", "you", "your", "yours", "yourself", "yourselves", "tell", "remember", "recall", "know", "also", "ever"])

_TOK = re.compile(r"[A-Za-z0-9_]+")


def w(name: str, default: float) -> float:
    try:
        return float(os.environ.get(f"COMMONS_RANK_{name.upper()}", default))
    except ValueError:
        return default


def terms(q: str, limit: int = 32) -> list[str]:
    toks = _TOK.findall(q or "")
    keep = [t for t in toks if t.lower() not in STOPWORDS]
    return (keep or toks)[:limit]


def fts_query(q: str) -> str:
    return " OR ".join(f'"{t}"' for t in terms(q))


MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_NUM = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
        "nine": 9, "ten": 10, "couple": 2, "few": 3, "several": 3}
_UNIT = {"day": 1, "week": 7, "month": 30, "year": 365, "weekend": 7}


def time_window(query: str, ref: float) -> tuple[float, float] | None:
    """Best-effort time window (start, end) referenced by the query, relative to ``ref`` (epoch seconds)."""
    q = (query or "").lower()
    day = 86400.0
    m = re.search(r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b", q)
    if m:
        try:
            t = dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=dt.timezone.utc).timestamp()
            return t - day, t + 2 * day
        except ValueError:
            pass
    m = re.search(r"\b(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|couple|few|several)\s+(?:of\s+)?"
                  r"(day|week|month|year)s?\s+ago\b", q)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else _NUM[m.group(1)]
        c = ref - n * _UNIT[m.group(2)] * day
        span = max(2, _UNIT[m.group(2)] * 0.5 * max(1, n ** 0.5)) * day
        return c - span, c + span
    for pat, back, span in ((r"\byesterday\b", 1, 1), (r"\btoday\b|\bthis morning\b|\btonight\b", 0, 1),
                            (r"\blast (week|weekend)\b", 7, 7), (r"\bthis week\b", 3.5, 4),
                            (r"\blast month\b", 30, 20), (r"\bthis month\b", 15, 16),
                            (r"\blast year\b", 365, 200), (r"\brecently\b|\blately\b", 14, 21)):
        if re.search(pat, q):
            c = ref - back * day
            return c - span * day, c + span * day
    m = re.search(r"\b(?:in|during|on|of|since)\s+(january|february|march|april|may|june|july|august|september|october|"
                  r"november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\b(?:\s+(\d{4}))?", q)
    if m:
        mon = MONTHS.get(m.group(1)[:3] if m.group(1) != "sept" else "sep") or MONTHS.get(m.group(1))
        r = dt.datetime.fromtimestamp(ref, dt.timezone.utc)
        year = int(m.group(2)) if m.group(2) else (r.year if mon <= r.month else r.year - 1)
        start = dt.datetime(year, mon, 1, tzinfo=dt.timezone.utc).timestamp()
        end = start + calendar.monthrange(year, mon)[1] * day
        return start, end
    return None


def time_score(ts: float, win: tuple[float, float]) -> float:
    a, b = win
    if a <= ts <= b:
        return 1.0
    dist = (a - ts) if ts < a else (ts - b)
    return math.exp(-dist / max(86400.0, (b - a)))


def recency(ts: float, ref: float, half_life_days: float) -> float:
    age = max(0.0, ref - ts) / 86400.0
    return 0.5 ** (age / max(0.1, half_life_days))


def fuse(lex: dict[str, float], sem: dict[str, float], wk: float, wv: float) -> dict[str, float]:
    """Fuse lexical and semantic candidates into one relevance score per id.

    ``COMMONS_RANK_FUSION=rrf`` is reciprocal-rank fusion (k = ``COMMONS_RANK_RRF_K``). ``score`` is a convex
    combination of min-max normalised scores, which keeps how *much* better a match is (RRF only keeps the order),
    so the multiplicative signals applied afterwards can't override a clearly stronger match.
    """
    how = os.environ.get("COMMONS_RANK_FUSION", "rrf")
    out: dict[str, float] = {}
    if how == "rrf":
        k = w("rrf_k", 60)
        for src, wt in ((lex, wk), (sem, wv)):
            for rank, iid in enumerate(sorted(src, key=lambda i: -src[i])):
                out[iid] = out.get(iid, 0.0) + wt / (k + rank)
        return out
    for src, wt in ((lex, wk), (sem, wv)):
        if not src:
            continue
        lo, hi = min(src.values()), max(src.values())
        span = (hi - lo) or 1.0
        for iid, v in src.items():
            out[iid] = out.get(iid, 0.0) + wt * ((v - lo) / span if hi > lo else 1.0)
    tot = (wk if lex else 0) + (wv if sem else 0)
    return {i: v / tot for i, v in out.items()} if tot else out
