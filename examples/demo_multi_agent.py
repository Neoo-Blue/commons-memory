"""End-to-end demo: 3 simulated agents (claude-code, grok, cursor) run real `commons worker` processes with a mock
CLI agent and cooperate on decomposed work with debate, review gates, a leftover carried into a recurring run,
and a learned user preference applied.

    python examples/demo_multi_agent.py
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from commons_memory.orchestrator import Orchestrator
from commons_memory.store import Store

HERE = Path(__file__).resolve().parent
tmp = tempfile.mkdtemp()
db = os.path.join(tmp, "demo.db")
env = {**os.environ, "COMMONS_DB": db, "HOME": tmp}  # keep demo agent keys out of your real ~/.commons
commons = [sys.executable, "-m", "commons_memory"]


def sh(*args):
    return subprocess.run(commons + list(args), env=env, capture_output=True, text=True, check=True).stdout


def workers(round_name):
    procs = []
    for name in ("claude-code", "grok", "cursor"):
        e = {**env, "MOCK_NAME": name, "MOCK_REJECT_FIRST": "1"}
        procs.append(subprocess.Popen(commons + ["worker", "--agent", name, "--model",
                                                 {"claude-code": "claude-sonnet", "grok": "grok-4", "cursor": "gpt-4o"}[name],
                                                 "--cmd", f"{sys.executable} {HERE / 'mock_agent.py'}", "--interval", "0.2",
                                                 "--idle-exit", "2.5"], env=e, stderr=subprocess.PIPE, text=True))
    for p in procs:
        p.wait()
        for line in p.stderr.read().splitlines():
            print("   ", line)


print("== 1. connect three agents from different platforms")
print(sh("connect", "claude-code", "--platform", "claude-code", "--expertise", "code:0.9,plan:0.8,summarize:0.7",
         "--cost", "4", "--no-install"), end="")
print(sh("connect", "grok", "--platform", "grok", "--expertise", "debate:0.9,red_team:0.9,research:0.8,judge:0.6",
         "--cost", "3", "--no-install"), end="")
print(sh("connect", "cursor", "--platform", "cursor", "--expertise", "review:0.9,test:0.8,code:0.7,judge:0.8",
         "--cost", "2", "--no-install"), end="")

store = Store(db)
o = Orchestrator(store)
print("\n== 2. user corrects an agent once -> preference learned")
print(o.prefs.extract("No, use tabs instead. Always add type hints.", project="shop", source="user-correction"))

print("\n== 3. submit one big task (parallel parts, each gated by review) and a debate")
big = o.submit("Build the checkout service:\n- cart API endpoints\n- research payment providers\n- order confirmation email",
               project="shop", agent_id=store.ensure_agent("user"), budget={"tokens": 200000, "cost": 5})
print(big["text"])
deb = o.submit("Should checkout use Stripe vs Adyen?", project="shop", n=3)
print(deb["text"])

print("\n== 4. recurring nightly task (run 1)")
o.schedule("nightly-qa", "0 2 * * *", "Implement fixes for failing checkout tests", pattern="cross_review", project="shop")
r1 = o.run_due(force="nightly-qa")[0]
print(r1)

print("\n== 5. workers run (claim -> mock CLI agent -> complete; first review of each part rejects)")
workers("round 1")
for j in (big["job_id"], deb["job_id"], r1["job_id"]):
    print(o.status(j)["text"], "\n")

print("== 6. leftovers recorded:")
for lo in o.leftovers(project="shop"):
    print("   ", lo["id"], lo["task_type"], lo["text"], "(recurring)" if lo["recurring_id"] else "")

print("\n== 7. recurring run 2 carries leftovers forward")
r2 = o.run_due(force="nightly-qa")[0]
print(r2)
workers("round 2")
st2 = o.status(r2["job_id"])
print(st2["text"])

print("\n== 8. checks")
debate_res = o.status(deb["job_id"])["result"]
big_st = o.status(big["job_id"])
reworked = store.q("SELECT COUNT(*) n FROM subtasks WHERE job_id=? AND attempts>1", (big["job_id"],))[0]["n"]
pref_applied = "Applied preference" in (big_st["result"] or "")
carried = any(s["key"].startswith("leftover") for s in st2["subtasks"])
checks = {
    "big task done": big_st["status"] == "done",
    "debate (3 positions -> judge) done": o.status(deb["job_id"])["status"] == "done",
    "review gates forced rework": reworked > 0,
    "leftover carried into recurring run 2": carried and r2["carried_leftovers"] >= 1,
    "recurring run 2 done": st2["status"] == "done",
    "learned preference applied by agents": pref_applied,
    "decision recorded from debate": bool(store.why("Stripe Adyen")["decisions"]),
}
for k, v in checks.items():
    print(f"   {'PASS' if v else 'FAIL'}  {k}")
print("\nusage by agent:")
print(sh("usage", "--by", "agent"))
print("debate synthesis excerpt:", (debate_res or "")[:200].replace("\n", " | "))
print(f"\ndemo database: {db}  (inspect with: COMMONS_DB={db} commons dashboard)")
sys.exit(0 if all(checks.values()) else 1)
