"""Dashboard memory edits: update, delete, bulk, and the search index."""
import json
import threading
import urllib.error
import urllib.request

from commons_memory.dashboard import PAGE, make_server
from conftest import make_store


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=8) as r:
        return json.loads(r.read())


def _post(base, path, body):
    req = urllib.request.Request(
        base + path, data=json.dumps(body).encode(),
        headers={"X-Commons": "1", "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read())


def _ids(payload):
    return [x["id"] for x in payload["results"]]


def test_page_guards_are_present():
    assert "window.oc_sel=window.oc_sel||\"\"" in PAGE
    assert "commons:open" in PAGE
    assert "never seen" in PAGE
    assert "dataset.oc" in PAGE
    assert "stopPoll" in PAGE
    assert "No subtasks" in PAGE
    assert "vis.has(k)" in PAGE
    assert "${e.type}" not in PAGE
    assert "name:'${E(window.sh)}'" not in PAGE


def test_item_update_delete_and_bulk(tmp_path):
    store = make_store(tmp_path, "dash09.db")
    a = store.add_item("memory", "alpha cats prefer fish", project="shop", tags=["pets"], agent_id=store.ensure_agent("ada"))
    b = store.add_item("fact", "beta dogs bark", project="shop", tags=["pets"], agent_id=store.ensure_agent("ada"))
    c = store.add_item("decision", "gamma use sqlite", project="shop", tags=["pets"], agent_id=store.ensure_agent("ada"))
    srv = make_server(store, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        updated = _post(base, "/api/item/update", {
            "id": a["id"], "content": 'alpha cats prefer "tuna"', "tags": ["pets", "food"]})
        assert updated["id"] == a["id"] and updated["changed"] is True
        hit = _get(base, "/api/search?q=tuna")
        assert a["id"] in _ids(hit)
        assert a["id"] not in _ids(_get(base, "/api/search?q=fish"))
        item = _get(base, "/api/item?id=" + a["id"])
        assert "tuna" in item["content"] and "food" in item["tags"] and "pets" in item["tags"]

        tagged = _post(base, "/api/items/bulk", {"ids": [b["id"], c["id"]], "action": "tag", "tag": "review"})
        assert {x["id"] for x in tagged["results"]} == {b["id"], c["id"]}
        assert all("error" not in x for x in tagged["results"])
        assert "review" in _get(base, "/api/item?id=" + b["id"])["tags"]
        assert "review" in _get(base, "/api/item?id=" + c["id"])["tags"]

        _post(base, "/api/items/bulk", {"ids": [b["id"]], "action": "untag", "tag": "pets"})
        tags = _get(base, "/api/item?id=" + b["id"])["tags"]
        assert "pets" not in tags and "review" in tags

        _post(base, "/api/items/bulk", {"ids": [c["id"]], "action": "move", "project": "other"})
        assert _get(base, "/api/item?id=" + c["id"])["project"] == "other"
        assert c["id"] in _ids(_get(base, "/api/search?q=sqlite&project=other"))

        facts = _get(base, "/api/search?q=&kind=fact")
        assert b["id"] in _ids(facts) and a["id"] not in _ids(facts)

        gone = _post(base, "/api/item/delete", {"id": a["id"]})
        assert gone["deleted"] is True
        assert a["id"] not in _ids(_get(base, "/api/search?q=tuna"))

        mixed = _post(base, "/api/items/bulk", {"ids": [b["id"], "nope"], "action": "delete"})
        by = {x["id"]: x for x in mixed["results"]}
        assert by[b["id"]].get("deleted") is True
        assert "error" in by["nope"]
        assert b["id"] not in _ids(_get(base, "/api/search?q=bark"))
    finally:
        srv.shutdown()


def test_item_routes_reject_bad_bulk(tmp_path):
    store = make_store(tmp_path, "dash09b.db")
    srv = make_server(store, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    try:
        req = urllib.request.Request(
            base + "/api/items/bulk", data=b'{"ids":[],"action":"nope"}',
            headers={"X-Commons": "1", "Content-Type": "application/json"}, method="POST")
        try:
            urllib.request.urlopen(req, timeout=8)
            raise AssertionError("expected 400")
        except urllib.error.HTTPError as e:
            assert e.code == 400
            assert "bulk" in json.loads(e.read())["error"]
    finally:
        srv.shutdown()
