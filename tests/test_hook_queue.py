"""The offline hook queue must stay cheap: a backlog or a hub outage can never turn hooks into a memory bomb."""
import json
import multiprocessing as mp
import os
import resource
import urllib.error

import pytest

from commons_memory import capture


def line(i, pad=0):
    return json.dumps({"harness": "claude-code", "event": "tool", "payload": {"n": i, "pad": "x" * pad}}) + "\n"


@pytest.fixture
def q(tmp_path, monkeypatch):
    monkeypatch.setenv("COMMONS_HOME", str(tmp_path))
    return capture._queue_path()


def test_drain_is_bounded_and_keeps_the_rest(q, monkeypatch):
    q.write_text("".join(line(i) for i in range(60)))
    got = []
    monkeypatch.setattr(capture, "_send", lambda u, b, t: got.append(b["payload"]["n"]))
    capture._drain("http://hub", q, 5)
    assert got == list(range(capture.QUEUE_DRAIN_EVENTS))
    capture._drain("http://hub", q, 5)
    capture._drain("http://hub", q, 5)
    assert got == list(range(60))  # the next hooks carry on where the last one stopped
    assert not list(q.parent.glob("hook-queue.jsonl*"))


def test_corrupt_lines_and_refused_events_do_not_stop_the_drain(q, monkeypatch):
    q.write_text(line(0) + '{"cut off\n7\n\n' + line(1) + line(2))
    got = []

    def send(u, b, t):
        if b["payload"]["n"] == 1:
            raise urllib.error.HTTPError(u, 413, "too big", {}, None)
        got.append(b["payload"]["n"])
    monkeypatch.setattr(capture, "_send", send)
    capture._drain("http://hub", q, 5)
    assert got == [0, 2]
    assert not q.exists() or q.stat().st_size == 0


@pytest.mark.parametrize("error", [urllib.error.URLError("refused"), urllib.error.HTTPError("u", 500, "boom", {}, None)])
def test_a_failed_send_keeps_that_event_exactly_once(q, monkeypatch, error):
    q.write_text(line(0) + line(1) + line(2))
    calls = []

    def send(u, b, t):
        calls.append(b["payload"]["n"])
        if b["payload"]["n"] == 1:
            raise error
    monkeypatch.setattr(capture, "_send", send)
    capture._drain("http://hub", q, 5)
    assert calls == [0, 1]
    calls.clear()
    monkeypatch.setattr(capture, "_send", lambda u, b, t: calls.append(b["payload"]["n"]))
    capture._drain("http://hub", q, 5)
    assert calls == [1, 2]


def test_a_slow_hub_never_blocks_or_grows_the_queue(q, monkeypatch):
    """A stop with a diff takes the hub 10+ s against a 2 s hook limit: the hub has it, so it must not be queued or kept."""
    def slow(u, b, t):
        raise TimeoutError("read timed out")
    monkeypatch.setattr(capture, "_send", slow)
    capture._post("http://hub", "claude-code", "stop", {"n": 1}, 2)
    assert not q.exists()
    q.write_text(line(0) + line(1))
    capture._drain("http://hub", q, 5)
    assert not list(q.parent.glob("hook-queue.jsonl*"))


def test_an_unreachable_hub_queues_the_event(q, monkeypatch):
    def down(u, b, t):
        raise urllib.error.URLError("refused")
    monkeypatch.setattr(capture, "_send", down)
    capture._post("http://hub", "claude-code", "stop", {"n": 1}, 2)
    assert json.loads(q.read_text())["event"] == "stop"


def test_urllib_raises_bare_timeout_only_after_the_request_was_sent(tmp_path):
    """_post relies on this: a hub that accepts and answers late gives TimeoutError, a closed port gives URLError."""
    import socket
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    with pytest.raises(TimeoutError):
        capture._send(f"http://127.0.0.1:{port}", {"a": 1}, 0.2)
    srv.close()
    with pytest.raises(urllib.error.URLError):
        capture._send(f"http://127.0.0.1:{port}", {"a": 1}, 0.2)


def test_drain_only_uses_what_the_hook_has_left(q, monkeypatch):
    import time
    q.write_text(line(0))
    monkeypatch.setattr(capture, "_send", lambda u, b, t: {})
    budgets = []
    monkeypatch.setattr(capture, "_drain", lambda u, qq, b: budgets.append(b))
    capture._post("http://hub", "claude-code", "tool", {}, 2, time.monotonic() - 3.5)
    assert len(budgets) == 1 and 0 < budgets[0] <= 0.5
    capture._post("http://hub", "claude-code", "tool", {}, 2, time.monotonic() - 4.5)
    assert len(budgets) == 1  # nothing left: leave it for the next hook


def test_queue_is_capped(q, monkeypatch):
    monkeypatch.setenv("COMMONS_HOOK_QUEUE_MAX_MB", "0.001")
    for i in range(100):
        capture._enqueue(q, line(i, 100).encode())
    assert q.stat().st_size <= 1100


def test_claim_left_by_a_dead_hook_is_recovered(q, monkeypatch):
    dead = q.with_name(q.name + f".{len(line(9))}.1.1.draining")
    dead.write_text(line(9) + line(0))  # it had already sent the first event
    got = []
    monkeypatch.setattr(capture, "_send", lambda u, b, t: got.append(b["payload"]["n"]))
    capture._drain("http://hub", q, 5)
    assert got == [] and dead.exists()  # could still be alive
    os.utime(dead, (1, 1))
    capture._drain("http://hub", q, 5)
    assert got == [0] and not dead.exists()


def test_an_old_queue_is_not_taken_for_an_abandoned_claim(q, monkeypatch):
    q.write_text(line(0))
    os.utime(q, (1, 1))  # a rename keeps this time
    claim, _ = capture._claim_queue(q)
    assert capture._claim_queue(q) is None and claim.exists()


def _hook_worker(args):
    home, out = args
    os.environ["COMMONS_HOME"] = home
    sent = []
    capture._send = lambda u, b, t: sent.append(b["payload"]["n"])
    base = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # imports alone differ by platform and extras
    capture._drain("http://hub", capture._queue_path(), 5)
    with open(out, "a") as f:
        f.write("".join(f"{n}\n" for n in sent))
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - base


def test_concurrent_hooks_on_a_big_queue_stay_small_and_never_duplicate(q, tmp_path):
    """The crash: 11 hooks, a big queue, each loading all of it. 40 MB here grows each worker by 80+ MB in the old code."""
    n = 2000
    with open(q, "w") as f:
        for i in range(n):
            f.write(line(i, 20_000))
    size = q.stat().st_size
    out = tmp_path / "sent"
    ctx = mp.get_context("spawn")
    with ctx.Pool(11) as pool:
        for _ in range(100):  # hooks keep arriving, as they do in real sessions
            rss = pool.map(_hook_worker, [(str(tmp_path), str(out))] * 11)
            if not list(tmp_path.glob("hook-queue.jsonl*")):
                break
    ids = [int(x) for x in out.read_text().split()]
    assert sorted(ids) == list(range(n)), "every event sent exactly once"
    unit = 1 if os.uname().sysname == "Darwin" else 1024
    assert max(rss) * unit < 16 * 2 ** 20 and size > 35 * 2 ** 20
