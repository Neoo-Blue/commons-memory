"""Secret-redaction scanner applied to every write. Patterns inspired by gitleaks."""
import re

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("openai_key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_\-]{20,}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("stripe_key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9a-zA-Z]{16,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("url_credentials", re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+:)([^\s@/]{3,})(@)")),
    ("assignment", re.compile(
        r"(?i)\b((?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|token)"
        r"\s*[:=]\s*)([\"']?)(?!vault://|env://|op://|\[REDACTED)([^\s\"',;]{6,})\2")),
]


def scan(text: str) -> list[str]:
    """Return the names of secret patterns found in text."""
    if not text:
        return []
    return [name for name, p in _PATTERNS if p.search(text)]


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
    return text, found
