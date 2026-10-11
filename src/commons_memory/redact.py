"""Secret-redaction scanner applied to every write. Patterns inspired by gitleaks."""
import os
import re

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_\-]{20,}\b")),
    ("huggingface_token", re.compile(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("gitlab_token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b")),
    ("npm_token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("sendgrid_key", re.compile(r"\bSG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\b")),
    ("digitalocean_token", re.compile(r"\bdo[opr]_v1_[a-f0-9]{64}\b")),
    ("xai_key", re.compile(r"\bxai-[A-Za-z0-9]{30,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9a-zA-Z]{16,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("url_credentials", re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+:)([^\s@/]{3,})(@)")),
    ("assignment", re.compile(
        r"(?i)\b((?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|token)"
        r"\s*[:=]\s*)([\"']?)(?!vault://|env://|op://|\[REDACTED)([^\s\"',;]{6,})\2")),
]


_CAND = re.compile(r"(?<![A-Za-z0-9_/+=.-])[A-Za-z0-9+/_=-]{32,}(?![A-Za-z0-9_/+=-])")
_SAFE = re.compile(r"^(?:[0-9a-f]+|[0-9A-F]+|[a-z]{2,6}_[0-9a-f]{16,}|[A-Za-z]+(?:[-_][A-Za-z]+)*|\d+)$")


def _entropy(s: str) -> float:
    import math
    from collections import Counter
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values())


def _high_entropy(text: str) -> list[str]:
    """Novel-format secrets: long mixed-case+digit tokens with high Shannon entropy. Hex digests, our ids,
    words/slugs and numbers are skipped (they are not secrets and are everywhere)."""
    if os.environ.get("COMMONS_ENTROPY_REDACT", "1") != "1":
        return []
    out = []
    for m in _CAND.finditer(text):
        t = m.group(0).strip("=")
        if "/" in t and not re.search(r"[+=]", m.group(0)):  # file paths / URLs: judge the longest segment
            t = max(t.split("/"), key=len)
        if len(t) < 32 or _SAFE.match(t) or "REDACTED" in t:
            continue
        if not (re.search(r"[a-z]", t) and re.search(r"[A-Z]", t) and re.search(r"\d", t)):
            continue
        if _entropy(t) >= float(os.environ.get("COMMONS_ENTROPY_MIN", "4.3")):
            out.append(m.group(0))
    return out


def scan(text: str) -> list[str]:
    """Return the names of secret patterns found in text."""
    if not text:
        return []
    return [name for name, p in _PATTERNS if p.search(text)] + (["high_entropy"] if _high_entropy(text) else [])


def redact(text: str) -> tuple[str, list[str]]:
    """Replace secrets with [REDACTED:<kind>]. Returns (clean_text, kinds_found)."""
    if not text:
        return text, []
    found: list[str] = []
    for name, p in _PATTERNS:
        if not p.search(text):
            continue
        found.append(name)
        if name == "url_credentials":
            text = p.sub(lambda m: f"{m.group(1)}[REDACTED:{name}]{m.group(3)}", text)
        elif name == "assignment":
            text = p.sub(lambda m: f"{m.group(1)}[REDACTED:{name}]", text)
        else:
            text = p.sub(f"[REDACTED:{name}]", text)
    hits = _high_entropy(text)
    if hits:
        found.append("high_entropy")
        for h in set(hits):
            text = text.replace(h, "[REDACTED:high_entropy]")
    return text, found
