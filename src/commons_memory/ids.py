import os
import time


def new_id(prefix: str) -> str:
    """Stable, sortable, globally unique id: <prefix>_<ms-time hex><random hex>."""
    return f"{prefix}_{int(time.time() * 1000):012x}{os.urandom(5).hex()}"


def now() -> float:
    return time.time()


def iso(ts: float | None) -> str:
    if ts is None:
        return ""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)
