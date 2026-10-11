"""Automatic knowledge base: one living wiki per project plus a `misc` bucket (auto-clustered into topics).

Every statement cites the record ids it came from, e.g. `… [mem_01a…]`. Pages are rebuilt incrementally:
writes mark a bucket dirty, and a dirty bucket is rebuilt on read / by `commons scheduler` / `commons kb build`,
at most once per COMMONS_KB_DEBOUNCE seconds (default 10). Summaries are extractive by default; set
COMMONS_KB_SUMMARIZER="<cmd>" to post-process each section with an LLM (stdin markdown -> stdout markdown;
citations are preserved in a Sources line).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

from .ids import estimate_tokens, iso, now
from .store import Store

SECTIONS = ["overview", "decisions", "architecture", "glossary", "references", "howtos", "people",
            "open_questions", "timeline", "preferences"]
TITLES = {"overview": "Overview", "decisions": "Key decisions & rationale", "architecture": "Architecture & components",
          "glossary": "Glossary & entities", "references": "References & links", "howtos": "How-tos & runbooks",
          "people": "People & agents", "open_questions": "Open questions & leftovers", "timeline": "Timeline / changelog",
          "preferences": "Preferences that apply"}
STOP = set("""a about above after again all also an and any are as at be because been before being below between both
but by can could did do does doing done for from further had has have having here how i if in into is it its itself
just more most must my no nor not now of off on once only or other our out over own same should so some such than
that the their them then there these they this those through to too under until up use used uses using very was we
were what when where which while who why will with would you your into via per new add adds added make made one two
memory task subtask job result results agent agents run runs need needs should could""".split())
URL = re.compile(r"https?://[^\s)\]>'\"`]+")
REPO = re.compile(r"\bgithub\.com/([\w.-]+/[\w.-]+)")
FILE = re.compile(r"(?<![\w/])((?:[\w.-]+/)*[\w-]+\.(?:py|ts|tsx|js|jsx|md|json|ya?ml|toml|sql|go|rs|java|sh|css|html))\b")
CMD = re.compile(r"`([^`\n]{3,120})`")
HOWTO = re.compile(r"(?i)^\s*(?:to [\w -]{3,40}, |how to |run |install |deploy |start |set |configure |use |restart |"
                   r"migrate |rotate |build |release )")


def debounce() -> float:
    return float(os.environ.get("COMMONS_KB_DEBOUNCE", "10"))


PROTOCOL = re.compile(r"(?im)^\s*(?:VERDICT|LEFTOVER|PREFERENCE|TOKENS|Applied preference):.*$")


def _clean(text: str) -> str:
    """Drop worker protocol lines and '[agent] type:' prefixes so KB prose stays readable."""
    text = PROTOCOL.sub("", text or "")
    text = re.sub(r"(?m)^(?:DECISION|WHY): ", "", text)
    return re.sub(r"(?m)^\[[\w.-]+\] [\w_]+: ", "", text).strip()


def _sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", _clean(text)).strip()
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9`\[])", text) if len(s.strip()) > 12]


def _terms(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z][a-z0-9_-]{2,}", (text or "").lower()) if w not in STOP]


def _cite(*ids) -> str:
    ids = [i for i in ids if i]
    return (" " + " ".join(f"[{i}]" for i in dict.fromkeys(ids))) if ids else ""


class KB:
    def __init__(self, store: Store):
        self.s = store

    # ---------------------------------------------------------------- buckets
    def buckets(self) -> list[str]:
        names = [r["name"] for r in self.s.q("SELECT name FROM projects ORDER BY name")]
        return names + ["misc"] + [r["bucket"] for r in self.s.q(
            "SELECT DISTINCT bucket FROM kb_pages WHERE bucket LIKE 'misc/%' ORDER BY bucket")]

    def _items(self, bucket: str) -> list[dict]:
        if bucket == "misc" or bucket.startswith("misc/"):
            rows = self.s.q("SELECT * FROM items WHERE project_id IS NULL AND superseded_by IS NULL ORDER BY created_at")
            items = [dict(r) for r in rows]
            if bucket.startswith("misc/"):
                topics = self.misc_topics(items)
                keep = set(topics.get(bucket.split("/", 1)[1], []))
                items = [i for i in items if i["id"] in keep]
            return items
        pid = self.s.project_id(bucket)
        return [dict(r) for r in self.s.q("SELECT * FROM items WHERE project_id=? AND superseded_by IS NULL "
                                          "ORDER BY created_at", (pid,))]

    def misc_topics(self, items: list[dict] | None = None, min_size: int = 2) -> dict[str, list[str]]:
        """Cluster unassigned memories by their most distinctive shared keyword."""
        if items is None:
            items = [dict(r) for r in self.s.q("SELECT * FROM items WHERE project_id IS NULL AND superseded_by IS NULL")]
        docs = {i["id"]: set(_terms(f"{i['title']} {i['content']} {' '.join(json.loads(i['tags'] or '[]'))}"))
                for i in items if i["kind"] not in ("checkpoint",)}
        df = Counter(t for terms in docs.values() for t in terms)
        tot = Counter(t for i in items for t in _terms(f"{i['title']} {i['content']}"))
        n = max(1, len(docs))
        topics: dict[str, list[str]] = defaultdict(list)
        for iid, terms in docs.items():
            cands = [t for t in terms if min_size <= df[t] <= max(min_size, int(n * 0.6))]
            best = max(cands, key=lambda t: (df[t], tot[t], len(t), t), default=None)
            topics[best or "general"].append(iid)
        # fold tiny topics into general
        out: dict[str, list[str]] = defaultdict(list)
        for t, ids in topics.items():
            out[t if len(ids) >= min_size and t != "general" else "general"].extend(ids)
        return dict(out)

    # ---------------------------------------------------------------- build
    def build(self, bucket: str) -> dict:
        items = self._items(bucket)
        pid = None if bucket.startswith("misc") else self.s.project_id(bucket)
        pages = {}
        for sec in SECTIONS:
            lines, cites = getattr(self, f"_sec_{sec}")(bucket, pid, items)
            content = "\n".join(lines) if lines else "_Nothing recorded yet._"
            content = self._llm(bucket, sec, content, cites)
            pages[sec] = (content, sorted(set(cites)))
        with self.s.lock:
            for sec, (content, cites) in pages.items():
                self.s.db.execute("INSERT OR REPLACE INTO kb_pages VALUES(?,?,?,?,?,?)",
                                  (bucket, sec, TITLES[sec], content, json.dumps(cites), now()))
            self.s.db.execute("INSERT INTO kb_state(bucket,dirty,dirtied_at,built_at) VALUES(?,0,?,?) ON CONFLICT(bucket) "
                              "DO UPDATE SET dirty=0, built_at=excluded.built_at", (bucket, now(), now()))
        built = [bucket]
        if bucket == "misc":
            for topic in self.misc_topics(items):
                if topic != "general":
                    built += self.build(f"misc/{topic}")["buckets"]
        return {"buckets": built, "items": len(items)}

    def _llm(self, bucket, sec, content, cites):
        if not cites:
            return content
        if sec in ("overview", "decisions"):
            from .providers import ProviderLayer
            llm = ProviderLayer(self.s).call_role("kb_summarizer", f"Rewrite this knowledge-base section as concise "
                                                  f"prose. Keep every [id] citation.\n# {TITLES[sec]} ({bucket})\n\n{content}")
            if llm:
                return llm.strip() + "\n\nSources: " + " ".join(f"[{c}]" for c in cites)
        hook = os.environ.get("COMMONS_KB_SUMMARIZER")
        if not hook:
            return content
        try:
            out = subprocess.run(hook, shell=True, input=f"# {TITLES[sec]} ({bucket})\n\n{content}", capture_output=True,
                                 text=True, timeout=120).stdout.strip()
            return (out + "\n\nSources: " + " ".join(f"[{c}]" for c in cites)) if out else content
        except Exception:  # noqa: BLE001
            return content

    def build_all(self) -> dict:
        built = []
        for b in [r["name"] for r in self.s.q("SELECT name FROM projects")] + ["misc"]:
            built += self.build(b)["buckets"]
        return {"built": built}

    def refresh_dirty(self, force: bool = False) -> list[str]:
        out = []
        for r in self.s.q("SELECT * FROM kb_state WHERE dirty=1"):
            if force or now() - (r["built_at"] or 0) >= debounce():
                b = r["bucket"]
                if b.startswith("misc/"):
                    b = "misc"
                if b == "misc" or self.s.q("SELECT 1 FROM projects WHERE name=?", (b,)):
                    out += self.build(b)["buckets"]
        return out

    def _ensure(self, bucket: str) -> bool:
        st = self.s.q("SELECT * FROM kb_state WHERE bucket=?", (bucket.split("/")[0] if bucket.startswith("misc/") else bucket,))
        never = not self.s.q("SELECT 1 FROM kb_pages WHERE bucket=?", (bucket,))
        if never or (st and st[0]["dirty"] and now() - (st[0]["built_at"] or 0) >= debounce()):
            self.build("misc" if bucket.startswith("misc/") else bucket)
            return False
        return bool(st and st[0]["dirty"])

    # ---------------------------------------------------------------- read API
    def get(self, bucket: str = "misc", section: str = "", max_tokens: int = 800) -> dict:
        bucket = bucket or "misc"
        stale = self._ensure(bucket)
        secs = [section] if section else SECTIONS
        parts, cites, used = [], [], 0
        for sec in secs:
            r = self.s.q("SELECT * FROM kb_pages WHERE bucket=? AND section=?", (bucket, sec))
            if not r:
                continue
            block = f"## {r[0]['title']}\n{r[0]['content']}"
            t = estimate_tokens(block)
            if used + t > max_tokens:
                room = (max_tokens - used) * 4
                if room > 200:
                    parts.append(block[:room].rsplit("\n", 1)[0] + "\n…")
                parts.append(f"(truncated at max_tokens={max_tokens}; ask for a single section)")
                break
            parts.append(block)
            cites += json.loads(r[0]["citations"] or "[]")
            used += t
        return {"bucket": bucket, "section": section or "all", "stale": stale,
                "text": f"# Knowledge base: {bucket}\n\n" + "\n\n".join(parts), "citations": sorted(set(cites)),
                "tokens": used}

    def list(self) -> list[dict]:
        out = []
        for b in self.buckets():
            st = self.s.q("SELECT * FROM kb_state WHERE bucket=?", (b,))
            pages = self.s.q("SELECT section, length(content) n, citations FROM kb_pages WHERE bucket=?", (b,))
            out.append({"bucket": b, "built_at": iso(st[0]["built_at"]) if st and st[0]["built_at"] else None,
                        "dirty": bool(st and st[0]["dirty"]), "sections": [p["section"] for p in pages],
                        "citations": sum(len(json.loads(p["citations"] or "[]")) for p in pages)})
        return out

    def search(self, query: str, bucket: str = "", limit: int = 15, max_tokens: int = 600) -> dict:
        terms = set(_terms(query)) or set(query.lower().split())
        hits = []
        rows = self.s.q("SELECT * FROM kb_pages" + (" WHERE bucket=?" if bucket else ""), ([bucket] if bucket else []))
        for r in rows:
            for line in r["content"].splitlines():
                lt = set(_terms(line))
                score = len(terms & lt) + (0.5 if query.lower() in line.lower() else 0)
                if score:
                    hits.append((score, r["bucket"], r["section"], line.strip()))
        hits.sort(key=lambda h: -h[0])
        lines, used = [], 0
        for _, b, sec, line in hits[:limit]:
            ln = f"{b}/{sec}: {line}"
            if used + estimate_tokens(ln) > max_tokens and lines:
                break
            lines.append(ln)
            used += estimate_tokens(ln)
        return {"text": "\n".join(lines) or "(no matches)", "hits": len(hits)}

    def digest(self, bucket: str, max_tokens: int = 160) -> str:
        """Compact digest injected into pickups: key points, decisions, open questions."""
        if not bucket:
            return ""
        self._ensure(bucket)
        out = []
        for sec, n in (("overview", 3), ("decisions", 3), ("open_questions", 3)):
            r = self.s.q("SELECT content FROM kb_pages WHERE bucket=? AND section=?", (bucket, sec))
            if r:
                bullets = [ln for ln in r[0]["content"].splitlines() if ln.startswith("- ")][:n]
                if bullets:
                    out.append(f"{TITLES[sec]}:\n" + "\n".join(bullets))
        text = f"Project KB digest ({bucket}; kb_get for more):\n" + "\n".join(out) if out else ""
        while estimate_tokens(text) > max_tokens and "\n" in text:
            text = text.rsplit("\n", 1)[0]
        return text

    # ---------------------------------------------------------------- export
    def export_markdown(self, out_dir: str) -> dict:
        """Obsidian-friendly vault: <bucket>/index.md, <bucket>/<section>.md, records/<id>.md with backlinks."""
        root = Path(out_dir)
        root.mkdir(parents=True, exist_ok=True)
        self.refresh_dirty(force=True)
        for b in [r["name"] for r in self.s.q("SELECT name FROM projects")] + ["misc"]:
            if not self.s.q("SELECT 1 FROM kb_pages WHERE bucket=?", (b,)):
                self.build(b)
        cited_by: dict[str, list[str]] = defaultdict(list)
        files = 0
        buckets = sorted({r["bucket"] for r in self.s.q("SELECT bucket FROM kb_pages")})
        for b in buckets:
            d = root / b
            d.mkdir(parents=True, exist_ok=True)
            idx = [f"# {b}", "", f"_Generated by Commons {iso(now())}_", ""]
            for sec in SECTIONS:
                r = self.s.q("SELECT * FROM kb_pages WHERE bucket=? AND section=?", (b, sec))
                if not r:
                    continue
                body = re.sub(r"\[((?:mem|dec|fct|hof|chk|ent|prj|ses|agt|tsk|job|sub|lft|prf|rec|lea|usg)_[0-9a-f]+)\]",
                              r"[[records/\1|\1]]", r[0]["content"])
                (d / f"{sec}.md").write_text(f"# {r[0]['title']} — {b}\n\n{body}\n\n[[{b}/index|← {b}]]\n")
                files += 1
                idx.append(f"- [[{b}/{sec}|{r[0]['title']}]]")
                for c in json.loads(r[0]["citations"] or "[]"):
                    cited_by[c].append(f"{b}/{sec}")
            (d / "index.md").write_text("\n".join(idx) + "\n")
            files += 1
        rec = root / "records"
        rec.mkdir(exist_ok=True)
        for rid, pages in cited_by.items():
            try:
                d = self.s.expand(rid, max_tokens=1500)
            except Exception:  # noqa: BLE001
                continue
            typ, name = self.s.label(rid)
            body = d.get("content") or d.get("statement") or d.get("text") or json.dumps(
                {k: v for k, v in d.items() if k not in ("neighbors",)}, default=str, indent=1)
            (rec / f"{rid}.md").write_text(f"# {name}\n\n`{rid}` · {typ}\n\n{body}\n\n## Cited in\n" +
                                           "\n".join(f"- [[{p}]]" for p in sorted(set(pages))) + "\n")
            files += 1
        (root / "index.md").write_text("# Commons knowledge base\n\n" + "\n".join(f"- [[{b}/index|{b}]]" for b in buckets) + "\n")
        return {"dir": str(root), "files": files + 1, "buckets": buckets}

    # ================================================================= sections
    def _sec_overview(self, bucket, pid, items):
        lines, cites = [], []
        kinds = Counter(i["kind"] for i in items)
        agents = Counter(self.s.label(i["agent_id"])[1] for i in items if i["agent_id"])
        files = self.s.q("SELECT COUNT(*) c FROM entities WHERE project_id=? AND type='file'", (pid,))[0]["c"] if pid else 0
        span = f"{iso(items[0]['created_at'])[:10]} → {iso(items[-1]['created_at'])[:10]}" if items else "—"
        lines.append(f"**{bucket}**: {len(items)} records ({', '.join(f'{v} {k}' for k, v in kinds.most_common())}), "
                     f"{files} indexed code files, {len(agents)} contributing agents; active {span}.")
        if pid:
            root = self.s.q("SELECT root FROM projects WHERE id=?", (pid,))
            if root and root[0]["root"]:
                lines.append(f"Code root: `{root[0]['root']}`" + _cite(pid))
        if bucket == "misc":
            topics = self.misc_topics(items)
            if topics:
                lines.append("Topics: " + ", ".join(f"[[misc/{t}]] ({len(v)})" if t != "general" else f"general ({len(v)})"
                                                    for t, v in sorted(topics.items(), key=lambda x: -len(x[1]))))
        lines.append("")
        lines.append("Key points:")
        # extractive: score sentences by bucket term frequency, prefer durable kinds
        tf = Counter(t for i in items for t in _terms(f"{i['title']} {i['content']}"))
        weight = {"fact": 1.3, "memory": 1.2, "decision": 1.1, "handoff": 1.0, "checkpoint": 0.6}
        cands = []
        for i in items:
            tags = json.loads(i["tags"] or "[]")
            w = weight.get(i["kind"], 0.8) * (0.5 if "subtask" in tags else 1.0) * (1.3 if "job-result" in tags else 1.0)
            for s_ in _sentences(i["content"])[:6]:
                if s_.startswith(("DECISION:", "WHY:", "GOAL:", "STATE:", "##", "[")) or len(s_) > 300:
                    continue
                ts = _terms(s_)
                if len(ts) < 3:
                    continue
                score = w * sum(tf[t] for t in set(ts)) / (len(ts) ** 0.5)
                cands.append((score, s_, i["id"]))
        seen, picked = set(), 0
        for score, s_, iid in sorted(cands, key=lambda x: -x[0]):
            key = " ".join(sorted(set(_terms(s_))))[:80]
            if key in seen or iid in cites:
                continue
            seen.add(key)
            lines.append(f"- {s_}" + _cite(iid))
            cites.append(iid)
            picked += 1
            if picked >= 7:
                break
        return lines, cites + ([pid] if pid else [])

    def _sec_decisions(self, bucket, pid, items):
        lines, cites = [], []
        for i in items:
            if i["kind"] != "decision":
                continue
            m = json.loads(i["meta"] or "{}")
            who = self.s.label(i["agent_id"])[1] if i["agent_id"] else "unknown"
            why = (_sentences(m.get("why", "")) or [_clean(m.get("why", ""))])[0][:220]
            lines.append(f"- **{m.get('what', i['title'])}** — {why} "
                         f"_(by {who}, {iso(i['created_at'])[:10]})_" + _cite(i["id"]))
            if m.get("alternatives"):
                lines.append(f"  - alternatives considered: {'; '.join(m['alternatives'])}")
            cites.append(i["id"])
        return lines, cites

    def _sec_architecture(self, bucket, pid, items):
        if not pid:
            return [], []
        lines, cites = [], []
        files = self.s.q("SELECT id, path, meta FROM entities WHERE project_id=? AND type='file'", (pid,))
        if not files:
            return ["_No code indexed; run `commons index <path> --project " + bucket + "`._"], []
        dirs = Counter((f["path"].split("/")[0] if "/" in f["path"] else ".") for f in files)
        langs = Counter(json.loads(f["meta"] or "{}").get("lang", "?") for f in files)
        lines.append(f"{len(files)} files ({', '.join(f'{v} {k}' for k, v in langs.items())}). Components:")
        for d, n in dirs.most_common(8):
            ex = next(f for f in files if (f["path"].split("/")[0] if "/" in f["path"] else ".") == d)
            lines.append(f"- `{d}/` — {n} files" + _cite(ex["id"]))
            cites.append(ex["id"])
        hubs = self.s.q("SELECT e.id, e.name, e.type, e.path, e.line, COUNT(r.src) n FROM entities e JOIN relations r ON "
                        "r.dst=e.id AND r.type='calls' WHERE e.project_id=? GROUP BY e.id ORDER BY n DESC LIMIT 8", (pid,))
        if hubs:
            lines.append("")
            lines.append("Most depended-on symbols:")
            for h in hubs:
                lines.append(f"- `{h['name']}` ({h['type']}, {h['path']}:{h['line']}) — {h['n']} callers" + _cite(h["id"]))
                cites.append(h["id"])
        classes = self.s.q("SELECT id, name, path, meta FROM entities WHERE project_id=? AND type='class' LIMIT 12", (pid,))
        if classes:
            lines.append("")
            lines.append("Classes:")
            for c in classes:
                doc = json.loads(c["meta"] or "{}").get("doc", "")
                lines.append(f"- `{c['name']}` in {c['path']}" + (f" — {doc}" if doc else "") + _cite(c["id"]))
                cites.append(c["id"])
        ext = self.s.q("SELECT DISTINCT e.name FROM entities e JOIN relations r ON r.dst=e.id AND r.type='imports' "
                       "WHERE e.project_id=? AND e.type='module' LIMIT 15", (pid,))
        if ext:
            lines.append("")
            lines.append("External dependencies: " + ", ".join(f"`{e['name']}`" for e in ext))
        return lines, cites

    def _sec_glossary(self, bucket, pid, items):
        ids = [i["id"] for i in items]
        if not ids:
            return [], []
        ph = ",".join("?" * len(ids))
        ents = self.s.q(f"SELECT e.id, e.name, e.type, COUNT(*) n FROM relations r JOIN entities e ON e.id=r.dst "
                        f"WHERE r.src IN ({ph}) AND r.type IN ('mentions','about','related_to') AND e.type NOT IN "
                        f"('project','file','task_type','workflow','scope','model') GROUP BY e.id ORDER BY n DESC LIMIT 25",
                        ids)
        lines, cites = [], []
        for e in ents:
            definition, src = "", None
            for i in sorted(items, key=lambda i: "subtask" in (i["tags"] or "")):
                for s_ in _sentences(i["content"]):
                    if e["name"].lower() in s_.lower():
                        definition, src = s_[:200], i["id"]
                        break
                if src:
                    break
            lines.append(f"- **{e['name']}** ({e['type']}, {e['n']} mentions)" + (f": {definition}" if definition else "")
                         + _cite(e["id"], src))
            cites += [e["id"]] + ([src] if src else [])
        return lines, cites

    def _sec_references(self, bucket, pid, items):
        refs: dict[tuple[str, str], list[str]] = defaultdict(list)
        for i in items:
            text = f"{i['title']}\n{i['content']}"
            for u in URL.findall(text):
                refs[("link", u.rstrip(".,;"))].append(i["id"])
            for r in REPO.findall(text):
                refs[("repo", r.rstrip(".,;"))].append(i["id"])
            for f in FILE.findall(text):
                if "://" not in f:
                    refs[("file", f)].append(i["id"])
        lines, cites = [], []
        for kind in ("repo", "link", "file"):
            group = [(k[1], v) for k, v in refs.items() if k[0] == kind]
            if group:
                lines.append(f"{kind.title()}s:")
                for val, ids in sorted(group, key=lambda x: -len(x[1]))[:20]:
                    fmt = f"<{val}>" if kind == "link" else f"`{val}`"
                    lines.append(f"- {fmt}" + _cite(*ids[:3]))
                    cites += ids[:3]
        return lines, cites

    def _sec_howtos(self, bucket, pid, items):
        lines, cites = [], []
        for i in items:
            tags = json.loads(i["tags"] or "[]")
            if set(tags) & {"runbook", "howto", "how-to", "procedure"} or i["title"].lower().startswith("how to"):
                body = _clean(i["content"])[:220]
                lines.append(f"- **{i['title'][:80]}**" + ("" if body.startswith(i["title"][:40]) else f": {body}")
                             + _cite(i["id"]))
                cites.append(i["id"])
                continue
            for s_ in _sentences(i["content"]):
                if HOWTO.match(s_) or (CMD.search(s_) and re.search(r"(?i)\b(run|install|deploy|start|use)\b", s_)):
                    lines.append(f"- {s_[:220]}" + _cite(i["id"]))
                    cites.append(i["id"])
                    break
        return lines[:25], cites

    def _sec_people(self, bucket, pid, items):
        lines, cites = [], []
        by = defaultdict(Counter)
        for i in items:
            if i["agent_id"]:
                by[i["agent_id"]][i["kind"]] += 1
        jobs = [r["id"] for r in self.s.q("SELECT id FROM jobs WHERE project_id IS ?", (pid,))]
        if jobs:
            ph = ",".join("?" * len(jobs))
            for r in self.s.q(f"SELECT claimed_by a, type, COUNT(*) n FROM subtasks WHERE job_id IN ({ph}) AND claimed_by "
                              f"IS NOT NULL GROUP BY a, type", jobs):
                by[r["a"]][f"{r['type']} subtasks"] += r["n"]
        for aid, c in sorted(by.items(), key=lambda x: -sum(x[1].values())):
            a = self.s.get_agent(aid) or {}
            cost = self.s.q("SELECT COALESCE(SUM(cost),0) c FROM usage WHERE agent_id=? AND project_id IS ?", (aid, pid))[0]["c"]
            lines.append(f"- **{a.get('name', aid)}** ({a.get('platform') or a.get('vendor') or 'agent'}): "
                         + ", ".join(f"{v} {k}" for k, v in c.most_common()) + (f"; spent ${cost:.4f}" if cost else "")
                         + _cite(aid))
            cites.append(aid)
        return lines, cites

    def _sec_open_questions(self, bucket, pid, items):
        lines, cites = [], []
        for lo in self.s.q("SELECT * FROM leftovers WHERE status='open' AND project_id IS ? ORDER BY priority DESC", (pid,)):
            lines.append(f"- [{lo['task_type']}, p{lo['priority']}] {lo['text']}" + _cite(lo["id"], lo["source_id"]))
            cites.append(lo["id"])
        ids = {i["id"] for i in items}
        for c in self.s.list_conflicts():
            if c["a"] in ids or c["b"] in ids:
                lines.append(f"- CONFLICT: {c['subject']}.{c['predicate']} has contradicting values" + _cite(c["a"], c["b"]))
                cites += [c["a"], c["b"]]
        for j in self.s.q("SELECT id, title, status FROM jobs WHERE project_id IS ? AND status IN "
                          "('running','failed','budget_exceeded')", (pid,)):
            lines.append(f"- Job {j['status']}: {j['title']}" + _cite(j["id"]))
            cites.append(j["id"])
        return lines, cites

    def _sec_timeline(self, bucket, pid, items):
        days = defaultdict(list)
        for i in items:
            days[iso(i["created_at"])[:10]].append(i)
        lines, cites = [], []
        for d in sorted(days, reverse=True)[:30]:
            its = days[d]
            notable = [i for i in its if i["kind"] in ("decision", "handoff") or "job-result" in (i["tags"] or "")][:4] or its[:2]
            lines.append(f"- **{d}** — {len(its)} records: " + "; ".join(f"{i['kind']}: {i['title'][:70]}" + _cite(i["id"])
                                                                         for i in notable))
            cites += [i["id"] for i in notable]
        return lines, cites

    def _sec_preferences(self, bucket, pid, items):
        from .prefs import Preferences
        prefs = Preferences(self.s).get(project=None if bucket.startswith("misc") else bucket, max_tokens=2000)["preferences"]
        lines = [f"- {p['statement']} _(confidence {p['confidence']:.2f}, {p['scope']}, {p['source']})_" + _cite(p["id"])
                 for p in prefs]
        return lines, [p["id"] for p in prefs]
