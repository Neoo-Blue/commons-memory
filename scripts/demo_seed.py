"""Seed a demo hub for screenshots and UI work: made-up agents, jobs, a workflow, board items and memories.
Never point it at a real database.   python scripts/demo_seed.py /tmp/demo.db && commons dashboard --db /tmp/demo.db"""
import random
import sys
import time

sys.path.insert(0, "tests")
from commons_memory import board as B  # noqa: E402
from commons_memory.orchestrator import Orchestrator  # noqa: E402
from commons_memory.store import Store  # noqa: E402
from commons_memory.usage import Usage  # noqa: E402


def seed(path: str) -> None:
    s = Store(path)
    o = Orchestrator(s)
    rnd = random.Random(7)
    team = {"claude-code": {"code": 0.9, "plan": 0.8, "review": 0.7}, "gemini": {"research": 0.9, "review": 0.8, "test": 0.7},
            "grok": {"debate": 0.9, "red_team": 0.9, "review": 0.8}, "codex": {"code": 0.85, "test": 0.8, "spec": 0.6},
            "cursor": {"code": 0.7, "review": 0.9, "judge": 0.8}}
    ids = {}
    for name, exp in team.items():
        ids[name] = s.ensure_agent(name)
        o.profiles.set(ids[name], expertise=exp, cost=3, speed=3, max_concurrent=3)
    for n, text in enumerate(["Staging runs on k3s in eu-west, three nodes", "Release notes go out every Friday at 15:00 UTC",
                              "The billing service owns invoice numbering; never generate ids client-side",
                              "Customer exports are capped at 50,000 rows per file",
                              "Design tokens live in packages/ui/tokens.json", "On-call rotates Monday 09:00 local"]):
        s.add_item("memory", text, agent_id=list(ids.values())[n % len(ids)], project=rnd.choice(["web", "billing", "infra"]))
    s.record_decision("Use Postgres row-level security for tenant isolation", "one policy per table beats app checks",
                      agent_id=ids["claude-code"], project="billing")
    for t in ["Add CSV export to the reports page", "Move invoices to the new numbering scheme",
              "Weekly dependency upgrade", "Investigate p95 latency on /search"]:
        B.add(s, "ask", t, project=rnd.choice(["web", "billing", "infra"]), source="user")
    B.add(s, "question", "Which date format should exports use: ISO 8601 or the account locale?", agent_id=ids["codex"])
    jid = o.submit("Add CSV export to the reports page", pattern="sdlc", project="web", agent_id=ids["claude-code"])["job_id"]
    spec = "Spec.\nAC1: an Export button downloads a CSV\nAC2: columns match the table\nAC3: unicode headers survive\n"
    answers = {"spec": spec, "plan": "PRD and build graph: backend (AC2, AC3), UI (AC1)",
               "code": "to_csv and the button\nTESTS: pytest -q -> 41 passed", "review": "Solid.\nVERDICT: approve",
               "test": "AC1: PASS test_button\nAC2: PASS test_columns\nAC3: FAIL headers mangled\nTESTS: pytest -> 1 failed\nVERDICT: reject"}
    for _ in range(5):
        for name, aid in ids.items():
            p = o.claim(aid)
            if p:
                o.complete(p["subtask_id"], aid, answers.get(p["type"], "done"))
        for q in s.q("SELECT id, text FROM board WHERE kind='question' AND status<>'done' AND text LIKE '[gate %'"):
            B.answer(s, q["id"], "yes", by="owner")
    o.submit("Review the auth middleware for session fixation", pattern="cross_review", project="web", agent_id=ids["cursor"])
    o.submit("Should we move search to a hosted vector database?", pattern="debate", project="infra", agent_id=ids["grok"])
    for name, aid in ids.items():
        for d in range(14):
            Usage(s).report(aid, tokens_in=rnd.randint(20_000, 90_000), tokens_out=rnd.randint(3_000, 20_000),
                            model={"claude-code": "anthropic/claude-sonnet-5-5", "gemini": "google/gemini-3-flash"}.get(name, ""),
                            cost=round(rnd.uniform(0.05, 0.9), 3), seconds=rnd.uniform(30, 600), note="demo")
            s.x("UPDATE usage SET ts=? WHERE id=(SELECT MAX(id) FROM usage)", (time.time() - d * 86400 - rnd.randint(0, 80000),))
    from commons_memory.messaging import heartbeat
    for name in ("claude-code", "gemini", "grok"):
        heartbeat(s, ids[name], status="online", client="demo")
    print(f"seeded {path}: {len(ids)} agents, job {jid}")


if __name__ == "__main__":
    seed(sys.argv[1])
