"""Live multi-agent benchmark against a running Commons hub: real agents, verifiable answers.

Every scenario plants a random nonce through one tool and asks each agent (via `delegate`) to recover it through
another, so a correct answer proves the agent used the shared hub rather than its own knowledge or a guess.

  ping     reply with a token                         -> delivery + wake latency
  recall   remember() a codeword, agent must search()  -> shared memory works across agents
  why      record_decision() a reason, agent uses why() -> decision log works across agents
  resume   handoff() a next step, agent uses resume()   -> continuity across agents
  code     submit_task(cross_review): one agent writes is_leap(), another reviews; the code is run on 8 cases here

Usage (stdlib only):
  COMMONS_KEY=... python benchmarks/live_agents.py --url http://hub:8770/mcp --agents grok-build,antigravity,muse,grok
Writes one JSON line per (scenario, agent) to --out and prints a table. Items go to project `bench-live`
(resume uses its own `bench-live-<nonce>` project: a handoff without session_id updates your latest session there).
"""
import argparse
import json
import os
import random
import re
import string
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

WORDS = ("amber basalt cobalt dahlia ember fjord garnet harbor indigo juniper kestrel lantern meadow nimbus orchid "
         "pewter quartz russet saffron tundra umber velvet willow xenon yarrow zephyr").split()


class Hub:
    def __init__(self, url, key):
        self.url, self.key, self.local = url, key, threading.local()

    def _post(self, body, sid=None):
        h = {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"} | ({"Mcp-Session-Id": sid} if sid else {})
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            raw, sid = r.read().decode(), r.headers.get("Mcp-Session-Id") or sid
        for line in raw.splitlines():
            if line.startswith("data:"):
                raw = line[5:]
        return (json.loads(raw) if raw.strip() else None), sid

    def call(self, tool, **args):
        if not getattr(self.local, "sid", None):  # one MCP session per thread
            _, sid = self._post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "live-bench", "version": "1"}}})
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
            self.local.sid = sid
        res, _ = self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": "call", "arguments": {"tool": tool, "args": args}}}, self.local.sid)
        if "error" in res or res["result"].get("isError"):
            raise RuntimeError(f"{tool}: {res.get('error') or res['result']['content'][0]['text'][:300]}")
        text = "".join(c.get("text", "") for c in res["result"].get("content", []))
        try:
            return json.loads(text)
        except ValueError:
            return text


def nonce():
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def wait_task(hub, tid, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        t = hub.call("get_task", task_id=tid)
        if t.get("status") in ("done", "completed", "failed", "rejected"):
            return t, time.time() - t0
        time.sleep(3)
    return None, timeout


def ask(hub, agent, task, timeout):
    tid = hub.call("delegate", to_agent=agent, task=task, project="bench-live")["task_id"]
    t, secs = wait_task(hub, tid, timeout)
    return (t or {}).get("result") or "", secs, bool(t)


def s_ping(hub, agent, timeout):
    n = nonce()
    ans, secs, done = ask(hub, agent, f"Live benchmark ping. Reply with exactly this token and nothing else: PONG-{n}", timeout)
    return {"ok": f"PONG-{n}" in ans, "secs": secs, "answered": done, "answer": ans[:200]}


def s_recall(hub, agent, timeout):
    n, word = nonce(), random.choice(WORDS)
    hub.call("remember", content=f"Bench canary {n}: the codeword for run {n} is {word}.", title=f"Bench canary {n}",
             project="bench-live", tags=["bench-live"])
    ans, secs, done = ask(hub, agent, f"Use the Commons `search` tool (project bench-live) to find 'Bench canary {n}'. "
                                      f"Answer with only the codeword stored for run {n}. Do not guess: if search finds "
                                      f"nothing, answer NOT FOUND.", timeout)
    return {"ok": word in ans.lower(), "secs": secs, "answered": done, "answer": ans[:200]}


def s_why(hub, agent, timeout):
    n, word = nonce(), random.choice(WORDS)
    hub.call("record_decision", what=f"Run {n} uses the {word} cache tier", project="bench-live",
             why=f"Run {n} chose it because the {word} tier had the lowest p95 in the {n} trial.")
    ans, secs, done = ask(hub, agent, f"Use the Commons `why` tool with the query 'run {n} cache tier' and answer with "
                                      f"only the name of the cache tier chosen for run {n} (one word).", timeout)
    return {"ok": word in ans.lower(), "secs": secs, "answered": done, "answer": ans[:200]}


def s_resume(hub, agent, timeout):
    n, word = nonce(), random.choice(WORDS)
    h = hub.call("handoff", summary=f"Bench handoff {n}: live benchmark continuity check.", project=f"bench-live-{n}",
                 goal=f"bench handoff {n}", next_steps=[f"Rotate the {word} widget for run {n}"])
    sid = h.get("session_id") or h.get("session") or ""
    ans, secs, done = ask(hub, agent, f"Use the Commons `resume` tool with session_id '{sid}' and answer with only the "
                                      f"first next step of that handoff, verbatim.", timeout)
    return {"ok": word in ans.lower(), "secs": secs, "answered": done, "answer": ans[:200], "session": sid}


LEAP_CASES = {1900: False, 2000: True, 2023: False, 2024: True, 2100: False, 2400: True, 1996: True, 1: False}


def run_leap(code):
    m = re.search(r"```(?:python)?\n(.*?)```", code, re.S)
    src = m.group(1) if m else code
    if "def is_leap" not in src:
        return False
    test = src + "\nimport json\nprint(json.dumps({str(y): bool(is_leap(y)) for y in %r}))" % list(LEAP_CASES)
    try:  # ponytail: the generated function runs in a separate isolated interpreter with a timeout, not a sandbox
        out = subprocess.run([sys.executable, "-I", "-c", test], capture_output=True, text=True, timeout=10).stdout
        got = json.loads(out.strip().splitlines()[-1])
    except Exception:  # noqa: BLE001
        return False
    return all(got.get(str(y)) == v for y, v in LEAP_CASES.items())


def s_code(hub, agents, timeout):
    t0 = time.time()
    job = hub.call("submit_task", pattern="cross_review", project="bench-live", task=(
        "Write a Python function is_leap(year: int) -> bool implementing the Gregorian leap-year rule. "
        "Output only one ```python fenced block with the function, no prose."))
    jid = job.get("job_id") or job.get("id")
    status = {}
    while time.time() - t0 < timeout:
        status = hub.call("job_status", job_id=jid, format="json")
        if status.get("status") in ("done", "failed", "cancelled"):
            break
        time.sleep(5)
    subs = status.get("subtasks") or []
    return {"ok": run_leap(status.get("result") or ""), "secs": time.time() - t0, "status": status.get("status"),
            "workers": sorted({f"{s['type']}:{s.get('agent')}" for s in subs}),
            "verdicts": [s["verdict"] for s in subs if s.get("verdict")]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8770/mcp")
    ap.add_argument("--agents", default="grok-build,antigravity")
    ap.add_argument("--scenarios", default="ping,recall,why,resume,code")
    ap.add_argument("--timeout", type=float, default=600, help="seconds to wait for each answer")
    ap.add_argument("--out", default="live_agents.jsonl")
    a = ap.parse_args()
    hub, agents = Hub(a.url, os.environ["COMMONS_KEY"]), a.agents.split(",")
    fns = {"ping": s_ping, "recall": s_recall, "why": s_why, "resume": s_resume}
    jobs = [(sc, ag) for sc in a.scenarios.split(",") if sc in fns for ag in agents]
    rows = []
    with ThreadPoolExecutor(len(jobs) or 1) as ex:
        futs = {ex.submit(fns[sc], hub, ag, a.timeout): (sc, ag) for sc, ag in jobs}
        if "code" in a.scenarios:
            futs[ex.submit(s_code, hub, agents, a.timeout * 2)] = ("code", "+".join(agents))
        for f, (sc, ag) in futs.items():
            try:
                r = f.result()
            except Exception as e:  # noqa: BLE001
                r = {"ok": False, "error": str(e)[:300]}
            rows.append({"scenario": sc, "agent": ag, **r})
    with open(a.out, "a") as f:
        for r in rows:
            f.write(json.dumps(r | {"ts": time.time()}) + "\n")
    print(f"{'scenario':8} {'agent':28} {'ok':5} {'secs':>7}  answer")
    for r in sorted(rows, key=lambda r: (r["scenario"], r["agent"])):
        print(f"{r['scenario']:8} {r['agent']:28} {str(r.get('ok')):5} {r.get('secs', 0):7.1f}  "
              f"{(r.get('answer') or r.get('error') or json.dumps(r.get('verdicts', '')))[:70]!r}")


if __name__ == "__main__":
    main()
