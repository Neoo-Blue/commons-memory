"""Phone approvals: every "Needs you" question (including vault approvals) is pushed to the user's phone through ntfy
with Yes / No buttons. A tap answers it through a one-time signed link to the dashboard (/api/board/quick), so no
dashboard login is needed on the phone; the signature covers exactly that question and that answer."""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import urllib.parse
import urllib.request

from . import settings
from .store import CommonsError, Store


def _secret(s: Store) -> bytes:
    r = s.q("SELECT value FROM meta WHERE key='quick_answer_secret'")
    if r:
        return r[0]["value"].encode()
    v = secrets.token_hex(32)
    s.x("INSERT OR IGNORE INTO meta VALUES('quick_answer_secret', ?)", (v,))
    return s.q("SELECT value FROM meta WHERE key='quick_answer_secret'")[0]["value"].encode()


def sign(s: Store, item_id: str, answer: str) -> str:
    return hmac.new(_secret(s), f"{item_id}:{answer}".encode(), hashlib.sha256).hexdigest()


def quick_answer(s: Store, item_id: str, answer: str, sig: str) -> dict:
    if answer not in ("yes", "no") or not hmac.compare_digest(sign(s, item_id, answer), sig or ""):
        raise CommonsError("invalid or expired link")
    from . import board as B
    item = B.get(s, item_id)
    if item["status"] not in ("open", "doing", "blocked"):
        return {"id": item_id, "already": item["status"]}
    return B.answer(s, item_id, answer, by="user (phone)")


def push(s: Store, title: str, message: str, actions: list | None = None) -> bool:
    url = settings.get(s, "phone_ntfy")
    if not url:
        return False
    p = urllib.parse.urlparse(url)
    server, topic = f"{p.scheme}://{p.netloc}", p.path.strip("/")
    body = {"topic": topic, "title": title[:200], "message": message[:1500], "priority": 4, "tags": ["robot"]}
    if actions:
        body["actions"] = actions
    try:
        urllib.request.urlopen(urllib.request.Request(server, data=json.dumps(body).encode(),
                                                      headers={"Content-Type": "application/json"}), timeout=8)
        return True
    except Exception as e:  # noqa: BLE001 - a failed push must never break the question itself
        s.audit(None, "phone.push.error", topic, str(e)[:200])
        return False


def push_question(s: Store, item: dict) -> bool:
    dash = (settings.get(s, "dashboard_url") or "").rstrip("/")
    actions = []
    if dash:
        for label, ans in (("Yes", "yes"), ("No", "no")):
            actions.append({"action": "http", "label": label, "url": f"{dash}/api/board/quick", "method": "POST",
                            "headers": {"X-Commons": "1", "Content-Type": "application/json"},
                            "body": json.dumps({"id": item["id"], "answer": ans, "sig": sign(s, item["id"], ans)}),
                            "clear": True})
        actions.append({"action": "view", "label": "Open", "url": f"{dash}/#Board"})
    who = item.get("source") or "an agent"
    return push(s, f"Commons: {who} needs you", item["text"], actions)
