"""Per-agent trust scores, used to weight search ranking, KB digests and librarian decisions.

score in [0.05, 1.0], default 0.5 for unknown/anonymous writers. Components (recomputed lazily, cached 10 min):
  auth      share of the agent's writes made with an authenticated key            (+0.2 * auth)
  outcomes  Beta(1,1) success rate of its orchestrated work, weighted by evidence  (+0.3 * (rate-0.5)*2 * conf)
  conflicts share of its facts that lost a conflict resolution                    (-0.4 * loss_rate)
  flags     librarian stale/contradiction flags and rejected skill versions      (-0.05 each, max -0.2)
An admin can pin a manual score (`trust set`), which overrides the computed value.
"""
from __future__ import annotations

import json

from .ids import now
from .store import Store

TTL = 600
DEFAULT = 0.5


class Trust:
    def __init__(self, store: Store):
        self.s = store
        self._mem: dict[str, float] = {}

    def compute(self, agent_id: str) -> dict:
        it = self.s.q("SELECT COUNT(*) n, COALESCE(SUM(authenticated),0) a FROM items WHERE agent_id=?", (agent_id,))[0]
        auth = (it["a"] / it["n"]) if it["n"] else 0.0
        o = self.s.q("SELECT COUNT(*) n, COALESCE(SUM(success),0) s FROM outcomes WHERE agent_id=?", (agent_id,))[0]
        rate = (o["s"] + 1) / (o["n"] + 2)
        conf = min(1.0, o["n"] / 10)
        lost = self.s.q("SELECT COUNT(*) n FROM conflicts c JOIN items i ON i.id IN (c.a, c.b) WHERE c.status='resolved' "
                        "AND i.agent_id=? AND i.superseded_by IS NOT NULL", (agent_id,))[0]["n"]
        facts = self.s.q("SELECT COUNT(*) n FROM items WHERE agent_id=? AND kind='fact'", (agent_id,))[0]["n"]
        loss = lost / facts if facts else 0.0
        flags = self.s.q("SELECT COUNT(*) n FROM librarian_proposals p JOIN items i ON i.id=p.target "
                         "WHERE p.kind IN ('stale','contradiction') AND i.agent_id=?", (agent_id,))[0]["n"]
        score = 0.4 + 0.2 * auth + 0.3 * (rate - 0.5) * 2 * conf - 0.4 * loss - min(0.2, 0.05 * flags)
        comp = {"auth": round(auth, 3), "success_rate": round(rate, 3), "evidence": o["n"], "conflict_loss": round(loss, 3),
                "flags": flags}
        score = max(0.05, min(1.0, score))
        man = self.s.q("SELECT manual FROM agent_trust WHERE agent_id=?", (agent_id,))
        manual = man[0]["manual"] if man and man[0]["manual"] is not None else None
        final = manual if manual is not None else score
        self.s.x("INSERT OR REPLACE INTO agent_trust(agent_id,score,components,manual,updated_at) VALUES(?,?,?,?,?)",
                 (agent_id, final, json.dumps(comp), manual, now()))
        return {"agent_id": agent_id, "score": round(final, 3), "computed": round(score, 3), "manual": manual} | comp

    def score(self, agent_id: str | None) -> float:
        if not agent_id:
            return DEFAULT
        if agent_id in self._mem:
            return self._mem[agent_id]
        r = self.s.q("SELECT score, updated_at FROM agent_trust WHERE agent_id=?", (agent_id,))
        v = r[0]["score"] if r and r[0]["updated_at"] and r[0]["updated_at"] > now() - TTL else self.compute(agent_id)["score"]
        self._mem[agent_id] = v
        return v

    def set_manual(self, agent: str, score: float | None) -> dict:
        a = self.s.get_agent(agent)
        aid = a["id"] if a else agent
        if score is not None and not 0 <= score <= 1:
            raise ValueError("score must be 0..1")
        self.s.x("INSERT OR IGNORE INTO agent_trust(agent_id,score,components,manual,updated_at) VALUES(?,?,?,?,?)",
                 (aid, DEFAULT, "{}", None, 0))
        self.s.x("UPDATE agent_trust SET manual=?, updated_at=0 WHERE agent_id=?", (score, aid))
        self.s.audit(None, "trust.set", aid, {"manual": score})
        return self.compute(aid)

    def table(self) -> list[dict]:
        out = []
        for a in self.s.q("SELECT id, name FROM agents ORDER BY name"):
            out.append({"agent": a["name"]} | self.compute(a["id"]))
        return sorted(out, key=lambda x: -x["score"])
