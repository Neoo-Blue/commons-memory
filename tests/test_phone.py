import json

import pytest

from commons_memory import board as B
from commons_memory import phone as P
from commons_memory import settings
from commons_memory.store import CommonsError


def test_needs_you_questions_reach_the_phone_with_signed_one_tap_answers(store, monkeypatch):
    sent = []
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=8: sent.append((req.full_url, json.loads(req.data))))
    settings.put(store, "phone_ntfy", "https://ntfy.sh/commons-test-topic-x81")
    settings.put(store, "dashboard_url", "http://192.168.50.111:8771")
    a = store.ensure_agent("asker4")
    q = B.add(store, "question", "Allow muse to POST the Radarr search?", project="p", agent_id=a)
    url, body = sent[-1]
    assert url == "https://ntfy.sh" and body["topic"] == "commons-test-topic-x81" and "Radarr" in body["message"]
    yes = next(x for x in body["actions"] if x["label"] == "Yes")
    assert yes["url"] == "http://192.168.50.111:8771/api/board/quick" and yes["headers"]["X-Commons"] == "1"
    payload = json.loads(yes["body"])
    with pytest.raises(CommonsError):
        P.quick_answer(store, payload["id"], "yes", "0" * 64)                 # forged signature
    with pytest.raises(CommonsError):
        P.quick_answer(store, payload["id"], "no", payload["sig"])            # a Yes signature can't answer No
    P.quick_answer(store, payload["id"], payload["answer"], payload["sig"])
    assert B.get(store, q["id"])["status"] == "done"
    assert P.quick_answer(store, payload["id"], "yes", payload["sig"])["already"] == "done"   # a second tap is a no-op
