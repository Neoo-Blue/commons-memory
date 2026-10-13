import json
import subprocess

import pytest

from commons_memory import diagrams as D
from commons_memory.store import CommonsError


def _repo(tmp_path, body_api):
    repo = tmp_path / "shop"
    repo.mkdir(exist_ok=True)
    (repo / "api.py").write_text(body_api)
    (repo / "pay.py").write_text("from tax import add_tax\n\ndef charge(amount):\n    return add_tax(amount)\n")
    (repo / "tax.py").write_text("def add_tax(amount):\n    return amount * 1.1\n")
    return repo


def test_call_sequence_and_commit_pinned_source_links(store, tmp_path):
    from commons_memory.codeindex import index_repo
    repo = _repo(tmp_path, "from pay import charge\n\ndef checkout(cart):\n    return charge(cart)\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/shop.git"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"], cwd=repo, check=True)
    index_repo(store, str(repo), "shop")
    seq = D.code_sequence(store, "checkout")
    assert seq["participants"][:3] == ["api.py", "pay.py", "tax.py"]
    assert [m["label"] for m in seq["messages"]][:2] == ["charge()", "add_tax()"]
    assert "<svg" in D.render_sequence(seq)
    g = D.code_graph(store, "shop")
    pay = next(n for n in g["nodes"] if n["title"].endswith("pay.py"))
    assert pay["detail"]["source"].startswith("https://github.com/acme/shop/blob/") and pay["detail"]["source"].endswith("/pay.py")
    assert any(l["url"].endswith("pay.py#L3") for l in pay["detail"]["links"])


def test_delta_between_chosen_snapshots(store, tmp_path):
    from commons_memory.codeindex import index_repo
    repo = _repo(tmp_path, "def checkout(cart):\n    return cart\n")
    index_repo(store, str(repo), "shop2")
    (repo / "ship.py").write_text("def ship(order):\n    return order\n")
    index_repo(store, str(repo), "shop2")
    (repo / "refund.py").write_text("def refund(order):\n    return order\n")
    index_repo(store, str(repo), "shop2")
    g = D.delta_graph(store, "shop2", "0", "-1")                              # oldest kept vs latest
    assert g["summary"]["files_added"] == 2
    assert D.delta_graph(store, "shop2")["summary"]["files_added"] == 1        # default: last two
    assert "Architecture delta" in D.make(store, "delta", "shop2:0:-1")


def test_job_sequence_and_lifecycles(store):
    from commons_memory.orchestrator import Orchestrator
    from commons_memory import board as B
    o = Orchestrator(store)
    a, w = store.ensure_agent("asker"), store.ensure_agent("worker")
    o.profiles.set(w, expertise={"general": 0.9}, max_concurrent=3)
    d = o.delegate(a, "worker", "Add the movie to Radarr", "Radarr add")
    o.claim(w, d["subtask_id"])
    o.complete(d["subtask_id"], w, "added, searching")
    seq = D.job_sequence(store, d["job_id"])
    labels = [m["label"] for m in seq["messages"]]
    assert seq["participants"][:3] == ["asker", "Commons", "worker"]
    assert any(l.startswith("claim") for l in labels) and any(l.startswith("done") for l in labels)
    lc = D.lifecycle(store, "subtask")
    edges = {(e["from"], e["to"]): e["weight"] for e in lc["edges"]}
    assert edges[("pending", "claimed")] >= 1 and edges[("claimed", "done")] >= 1
    it = B.add(store, "action", "Ship it", project="p")
    B.update(store, it["id"], status="doing")
    B.update(store, it["id"], status="done", evidence="v1 live")
    bl = {(e["from"], e["to"]) for e in D.lifecycle(store, "board")["edges"]}
    assert ("open", "doing") in bl and ("doing", "done") in bl
    assert "Lifecycle" in D.make(store, "lifecycle", "board")
    with pytest.raises(CommonsError):
        D.lifecycle(store, "nope")
