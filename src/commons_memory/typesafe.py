"""TypeSafe (https://docs.typesafe.ai): typed yes/no judgments with a probability, ~150 ms, for checks that don't need a
text-generating model. Off unless TYPESAFE_API_KEY is set. Used by the librarian to flag free-text contradictions
(measured on 96 labeled note pairs: precision 1.00, recall 0.97 at 0.5, same as Claude Haiku 5.5 at ~1/7 the latency)."""
from __future__ import annotations

import json
import os
import urllib.request

URL = os.environ.get("COMMONS_TYPESAFE_URL", "https://api.typesafe.ai/v1/systemone")


def enabled() -> bool:
    return bool(os.environ.get("TYPESAFE_API_KEY"))


def noul(state: str, questions: dict[str, str], timeout: float = 20) -> dict[str, float]:
    """Ask yes/no questions about `state`; returns {key: probability of yes}."""
    body = {"state": state[:12000], "model": os.environ.get("COMMONS_TYPESAFE_MODEL", "jev-latest"),
            "questions": {k: {"type": "noul", "instructions": v} for k, v in questions.items()}}
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={
        "Authorization": "Bearer " + os.environ["TYPESAFE_API_KEY"], "Content-Type": "application/json",
        "User-Agent": "commons-memory"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    return {k: float(v.get("noul") or 0) for k, v in (d.get("answers") or {}).items()}
