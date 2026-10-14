"""The job workspace: one shared place where everything the agents did for a job lands.

Each job gets a Commons Drive folder, /jobs/<job>/. Every part writes its result there (<key>/result.md), and what it
changed in code (<key>/changes.diff plus changes.json: repo, branch, commit, whether GitHub confirms the commit, the
diffstat). Agents can add files of their own; reviewers get the diff of the work they check, and later steps get the
folder and their dependencies' changes with their claim. Work outside a job lands in /work/<claim>/ (start_work) or
/sessions/<id>/ (anything else the hooks saw). Drive keeps every version, redacts secrets, and skips unchanged writes,
so the hooks can resend the same diff at every stop for free.
"""
from __future__ import annotations

import json
import re

from .ids import iso, now
from .store import CommonsError, Store

MAX_DIFF = 200_000


def folder(job_id: str) -> str:
    return f"/jobs/{job_id}"


def _drive(s: Store):
    from .drive import Drive
    return Drive(s)


def _job_project(s: Store, job_id: str) -> str | None:
    r = s.q("SELECT project_id FROM jobs WHERE id=?", (job_id,))
    return s.project_name(r[0]["project_id"]) if r and r[0]["project_id"] else None


def _safe(name: str) -> str:
    """One path segment: part keys come from plans that agents and models write, so `../x` must stay inside."""
    return re.sub(r"[^A-Za-z0-9._:-]", "_", str(name)).lstrip(".")[:80] or "_"


def _where(s: Store, subtask: dict | None, claim: str | None, session: str | None) -> tuple[str, str | None, str]:
    """(folder, project, ref the files are linked to)."""
    if subtask:
        return f"{folder(subtask['job_id'])}/{_safe(subtask['key'])}", _job_project(s, subtask["job_id"]), subtask["id"]
    if claim:
        r = s.q("SELECT project_id FROM work_claims WHERE id=?", (claim,))
        return f"/work/{_safe(claim)}", (s.project_name(r[0]["project_id"]) if r and r[0]["project_id"] else None), claim
    if session:
        return f"/sessions/{_safe(session)}", None, session
    raise CommonsError("changes need a subtask, a work claim or a session")


def save_result(s: Store, st: dict, agent_id: str, result: str) -> None:
    where, project, ref = _where(s, st, None, None)
    _drive(s).write(f"{where}/result.md", f"# {st['title']}\n\n{result}", project=project, agent_id=agent_id,
                    message=f"{s.label(agent_id)[1]} finished {st['key']}", link=ref)


def files_changed(stat: str, diff: str) -> list[str]:
    found = re.findall(r"^diff --git a/(\S+) b/", diff or "", re.M)
    if not found:
        found = [ln.split("|")[0].strip() for ln in (stat or "").splitlines() if "|" in ln]
    return sorted(set(found))[:200]


def record_changes(s: Store, agent_id: str, *, subtask: dict | None = None, claim: str | None = None,
                   session: str | None = None, repo: str = "", branch: str = "", commit: str = "", diff: str = "",
                   stat: str = "", untracked: list[str] | None = None, source: str = "agent") -> dict:
    """Store what an agent changed: the diff (secrets redacted by the Drive, capped) and changes.json with the repo,
    branch, commit (checked on GitHub when given) and the files touched."""
    if not (diff.strip() or commit.strip() or untracked):
        return {}
    where, project, ref = _where(s, subtask, claim, session)
    d, who = _drive(s), s.label(agent_id)[1]
    out = {"folder": where, "files": files_changed(stat, diff)}
    if diff.strip():
        cut = len(diff) > MAX_DIFF
        d.write(f"{where}/changes.diff", diff[:MAX_DIFF] + ("\n# (truncated)\n" if cut else ""), project=project,
                agent_id=agent_id, message=f"{who}'s changes ({source})", link=ref)
        out["diff"] = f"{where}/changes.diff"
    verified = ""
    if commit.strip():
        from .status import git_verify
        try:
            verified = git_verify(s, repo, branch, commit.strip())
        except Exception as e:  # noqa: BLE001 - a GitHub hiccup must not lose the record
            verified = f"unverified: {str(e)[:80]}"
    meta = {"by": who, "at": iso(now()), "ts": now(), "source": source, "repo": repo, "branch": branch, "commit": commit.strip(),
            "verified": verified, "stat": (stat or "")[-4000:], "files": out["files"], "untracked": (untracked or [])[:100],
            "ref": ref}
    d.write(f"{where}/changes.json", json.dumps(meta, indent=1), project=project, agent_id=agent_id,
            message=f"{who}'s changes ({source})", link=ref)
    s.audit(agent_id, "work.changes", ref, {"files": len(out["files"]), "commit": commit[:12], "source": source})
    return out | {"verified": verified}


def changes(s: Store, ref: str, diff_chars: int = 0) -> dict | None:
    """The changes recorded for a subtask, work claim or session (diff_chars > 0 adds the start of the diff)."""
    d = _drive(s)
    meta, diff = None, ""
    for a in d.artifacts(ref):
        try:
            if a["path"].endswith("/changes.json"):
                meta = json.loads(d.read(a["path"], a["project"], max_tokens=4000)["text"] or "{}")
            elif a["path"].endswith("/changes.diff") and diff_chars:
                diff = d.read(a["path"], a["project"], max_tokens=diff_chars // 3 + 50)["text"] or ""
        except Exception:  # noqa: BLE001 - a missing or unreadable file just isn't shown
            continue
    if meta and diff_chars:
        meta["diff"] = diff[:diff_chars] + ("…" if len(diff) > diff_chars else "")
    return meta


def files(s: Store, job_id: str) -> list[dict]:
    try:
        ls = _drive(s).list(folder(job_id), _job_project(s, job_id), recursive=True)
    except CommonsError:
        return []
    return [{k: f.get(k) for k in ("path", "version", "size", "updated_by", "updated_at")}
            for f in (ls.get("files") if isinstance(ls, dict) else ls) or [] if not f.get("dir")]


def packet_view(s: Store, st: dict, budget_chars: int = 6000) -> dict:
    """What a claim packet carries from the workspace: the folder, its files, and the code changes this step must see
    (the gated work's diff for a review; the dependencies' changes for later steps)."""
    look = [st["gate_of"]] if st.get("gate_of") else json.loads(st.get("deps") or "[]")
    seen = []
    for ref in look:
        c = changes(s, ref, diff_chars=max(800, budget_chars // max(1, len(look))))
        if c:
            seen.append(c)
    return {"folder": folder(st["job_id"]), "files": files(s, st["job_id"])[:40], "changes": seen,
            "hint": f"fs_read(path) reads a file; fs_write('{folder(st['job_id'])}/{_safe(st['key'])}/<name>', ...) adds one. "
                    "Report code you changed with complete_subtask(repo, branch, commit, diff)."}


def recent(s: Store, agent: str | None = None, limit: int = 30, diff_chars: int = 8000,
           where: tuple[str, list] | None = None) -> list[dict]:
    """Recorded changes across all jobs, work and sessions, newest first; agent filters to one agent's, where adds an
    SQL condition on drive_files (a project page passes its names and jobs)."""
    sql = "SELECT * FROM drive_files WHERE path LIKE '%/changes.json' AND deleted_at IS NULL"
    args: list = []
    if where:
        sql += f" AND {where[0]}"
        args += list(where[1])
    if agent:
        a = s.get_agent(agent)
        sql += " AND updated_by=?"
        args.append(a["id"] if a else "__none__")
    out = []
    d = _drive(s)
    for r in s.q(sql + " ORDER BY updated_at DESC LIMIT ?", args + [limit]):
        try:
            meta = json.loads(d.read(r["path"], r["project"] or None, max_tokens=4000)["text"] or "{}")
        except Exception:  # noqa: BLE001
            continue
        try:
            if not diff_chars:
                raise LookupError("diff not wanted")
            diff = d.read(r["path"].replace("/changes.json", "/changes.diff"), r["project"] or None,
                          max_tokens=diff_chars // 3 + 50)["text"] or ""
            meta["diff"] = diff[:diff_chars] + ("…" if len(diff) > diff_chars else "")
        except Exception:  # noqa: BLE001 - a commit-only record has no diff file
            pass
        out.append(meta | {"path": r["path"].rsplit("/", 1)[0], "project": r["project"] or None,
                           "updated_at": r["updated_at"], "version": r["version"]})
    return out
