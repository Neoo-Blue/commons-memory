"""Online research: pluggable web search + fetch/extract, cached, stored as cited memories that feed the KB.

Providers: brave, tavily, exa, searxng, fetch (URLs only, no search), mock. API keys are secret refs
(`provider_key_ref`) resolved server-side; values never reach models or memory.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser

from .ids import now
from .redact import redact
from .store import CommonsError, Store

CACHE_TTL = 24 * 3600


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title, self.parts, self._skip, self._in_title = "", [], 0, False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg", "nav", "footer", "header"):
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in ("p", "br", "li", "h1", "h2", "h3", "div", "tr"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg", "nav", "footer", "header") and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def extract(raw: str) -> tuple[str, str]:
    p = _Text()
    try:
        p.feed(raw)
    except Exception:  # noqa: BLE001
        pass
    text = re.sub(r"[ \t]+", " ", html.unescape("".join(p.parts)))
    text = "\n".join(ln.strip() for ln in text.splitlines() if len(ln.strip()) > 2)
    return html.unescape(p.title.strip()), text


def fetch(url: str, timeout: float = 15, max_bytes: int = 2_000_000) -> dict:
    if not re.match(r"^https?://", url):
        raise CommonsError("only http(s) URLs")
    req = urllib.request.Request(url, headers={"User-Agent": "commons-memory/0.4 (+research)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        raw = r.read(max_bytes).decode(r.headers.get_content_charset() or "utf-8", "replace")
        ctype = r.headers.get("Content-Type", "")
    title, text = extract(raw) if "html" in ctype or raw.lstrip().startswith("<") else ("", raw)
    return {"url": url, "title": title or url, "text": text}


class Research:
    def __init__(self, store: Store):
        self.s = store

    def _key(self, provider: str) -> str | None:
        r = self.s.q("SELECT value FROM meta WHERE key=?", (f"research_key:{provider}",))
        if not r:
            return None
        from .secrets import Broker
        return Broker(self.s).resolve_internal(r[0]["value"], f"research:{provider}")

    def configure(self, provider: str, key_ref: str | None = None, url: str | None = None, default: bool = False) -> dict:
        if key_ref:
            from .secrets import Broker
            b = Broker(self.s)
            if not self.s.q("SELECT 1 FROM secret_refs WHERE ref=?", (key_ref,)):
                b.register(key_ref, f"research provider {provider}")
            self.s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"research_key:{provider}", key_ref))
        if url:
            self.s.x("INSERT OR REPLACE INTO meta VALUES(?,?)", (f"research_url:{provider}", url))
        if default:
            self.s.x("INSERT OR REPLACE INTO meta VALUES('research_provider', ?)", (provider,))
        return {"provider": provider, "key_ref": key_ref, "url": url, "default": default}

    def default_provider(self) -> str:
        r = self.s.q("SELECT value FROM meta WHERE key='research_provider'")
        return r[0]["value"] if r else "fetch"

    def _url(self, provider: str) -> str | None:
        r = self.s.q("SELECT value FROM meta WHERE key=?", (f"research_url:{provider}",))
        return r[0]["value"] if r else None

    def search(self, query: str, provider: str, n: int) -> list[dict]:
        if provider == "brave":
            req = urllib.request.Request("https://api.search.brave.com/res/v1/web/search?" +
                                         urllib.parse.urlencode({"q": query, "count": n}),
                                         headers={"X-Subscription-Token": self._key("brave") or "", "Accept": "application/json"})
            data = json.loads(urllib.request.urlopen(req, timeout=20).read())  # noqa: S310
            return [{"url": r["url"], "title": r.get("title", ""), "snippet": r.get("description", "")}
                    for r in data.get("web", {}).get("results", [])[:n]]
        if provider == "tavily":
            req = urllib.request.Request("https://api.tavily.com/search", method="POST",
                                         data=json.dumps({"api_key": self._key("tavily"), "query": query,
                                                          "max_results": n}).encode(),
                                         headers={"Content-Type": "application/json"})
            data = json.loads(urllib.request.urlopen(req, timeout=30).read())  # noqa: S310
            return [{"url": r["url"], "title": r.get("title", ""), "snippet": r.get("content", "")} for r in data["results"]]
        if provider == "exa":
            req = urllib.request.Request("https://api.exa.ai/search", method="POST",
                                         data=json.dumps({"query": query, "numResults": n}).encode(),
                                         headers={"Content-Type": "application/json", "x-api-key": self._key("exa") or ""})
            data = json.loads(urllib.request.urlopen(req, timeout=30).read())  # noqa: S310
            return [{"url": r["url"], "title": r.get("title", ""), "snippet": r.get("text", "")[:300]} for r in data["results"]]
        if provider == "searxng":
            base = (self._url("searxng") or "http://127.0.0.1:8888").rstrip("/")
            data = json.loads(urllib.request.urlopen(f"{base}/search?" + urllib.parse.urlencode(
                {"q": query, "format": "json"}), timeout=20).read())  # noqa: S310
            return [{"url": r["url"], "title": r.get("title", ""), "snippet": r.get("content", "")} for r in data["results"][:n]]
        if provider == "mock":
            base = self._url("mock") or "http://127.0.0.1"
            return [{"url": f"{base}/page{i}", "title": f"Result {i} for {query}", "snippet": f"snippet {i}"}
                    for i in range(1, n + 1)]
        if provider == "fetch":
            urls = re.findall(r"https?://\S+", query)
            if not urls:
                raise CommonsError("the 'fetch' provider needs URLs in the query; configure a search provider "
                                   "(brave|tavily|exa|searxng) for web search")
            return [{"url": u, "title": "", "snippet": ""} for u in urls[:n]]
        raise CommonsError(f"unknown research provider {provider}")

    def research(self, query: str, *, provider: str | None = None, max_results: int = 5, fetch_pages: bool = True,
                 project: str | None = None, agent_id: str | None = None, refresh: bool = False) -> dict:
        provider = provider or self.default_provider()
        key = hashlib.sha256(f"{provider}|{query}|{max_results}|{fetch_pages}".encode()).hexdigest()
        cached = self.s.q("SELECT * FROM research_cache WHERE key=?", (key,))
        if cached and not refresh and now() - cached[0]["ts"] < CACHE_TTL:
            data = json.loads(cached[0]["results"])
            data["cached"] = True
            return data
        results = self.search(query, provider, max_results)
        sources = []
        for r in results:
            page = {"url": r["url"], "title": r["title"], "text": r.get("snippet", "")}
            if fetch_pages:
                try:
                    page = fetch(r["url"])
                except Exception as e:  # noqa: BLE001
                    page["error"] = str(e)[:120]
            text, _ = redact(page["text"])
            excerpt = " ".join(text.split())[:1200]
            item = self.s.add_item("source", f"{page.get('title') or r['title']}\nURL: {r['url']}\n\n{excerpt}",
                                   title=(page.get("title") or r["title"] or r["url"])[:120],
                                   summary=f"{(page.get('title') or r['title'])[:80]} — {excerpt[:140]} ({r['url']})",
                                   project=project, agent_id=agent_id, tags=["research", "source"],
                                   meta={"url": r["url"], "provider": provider, "query": query})
            sources.append({"id": item["id"], "url": r["url"], "title": page.get("title") or r["title"],
                            "excerpt": excerpt[:600], **({"error": page["error"]} if page.get("error") else {})})
        for s_ in sources:  # per-source corroboration across independent domains
            first = re.split(r"(?<=[.!?])\s+", s_["excerpt"])[0]
            s_["corroboration"] = corroboration(first, s_["url"], sources)
            if s_["corroboration"] <= 1:
                self.s.x("UPDATE items SET tags=? WHERE id=?", (json.dumps(["research", "source", "single-source"]), s_["id"]))
                self.s.sync_fts(s_["id"])
        summary = self._summarize(query, sources)
        note = self.s.add_item("memory", summary, title=f"Research: {query[:100]}", project=project, agent_id=agent_id,
                               tags=["research"], related_ids=[s["id"] for s in sources],
                               meta={"query": query, "provider": provider, "sources": [s["id"] for s in sources]})
        for s_ in sources:
            self.s.relate(note["id"], s_["id"], "cites")
        data = {"query": query, "provider": provider, "note_id": note["id"], "sources": sources, "summary": summary,
                "cached": False}
        self.s.x("INSERT OR REPLACE INTO research_cache VALUES(?,?,?,?,?)", (key, provider, query, json.dumps(data), now()))
        return data

    def _summarize(self, query: str, sources: list[dict]) -> str:
        from .providers import ProviderLayer
        body = "\n".join(f"[{s['id']}] {s['title']} ({s['url']}): {s['excerpt']}" for s in sources)
        llm = ProviderLayer(self.s).call_role("kb_summarizer", f"Answer the research question using ONLY these sources; "
                                              f"cite them as [id].\nQUESTION: {query}\nSOURCES:\n{body}")
        if llm:
            return llm + "\n\nSources:\n" + "\n".join(f"- [{s['id']}] {s['url']}" for s in sources)
        lines = [f"Research question: {query}", "", "Findings (extractive; corroboration = independent domains agreeing):"]
        terms = set(re.findall(r"[a-z]{4,}", query.lower()))
        for s in sources:
            sents = re.split(r"(?<=[.!?])\s+", s["excerpt"])
            best = max(sents, key=lambda x: len(terms & set(re.findall(r"[a-z]{4,}", x.lower()))), default="")
            c = corroboration(best, s["url"], sources)
            mark = "single-source, unverified" if c <= 1 else f"corroborated by {c} domains"
            lines.append(f"- {best[:240]} [{s['id']}] ({s['url']}) — {mark}")
        return "\n".join(lines)


def _domain(url: str) -> str:
    from urllib.parse import urlparse
    h = (urlparse(url).hostname or url).lower()
    parts = h.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def corroboration(claim: str, url: str, sources: list[dict], min_overlap: float = 0.5) -> int:
    """How many independent domains (including the claim's own) contain most of the claim's salient terms.
    A cheap cross-source check: it flags single-source claims; it does not establish truth."""
    stop = {"that", "this", "with", "from", "have", "were", "which", "their", "about", "there", "would", "these", "also"}
    ct = {t for t in re.findall(r"[a-z0-9]{4,}", claim.lower()) if t not in stop}
    if not ct:
        return 1
    doms = {_domain(url)}
    for s in sources:
        st = set(re.findall(r"[a-z0-9]{4,}", (s.get("excerpt") or "").lower()))
        if len(ct & st) / len(ct) >= min_overlap:
            doms.add(_domain(s["url"]))
    return len(doms)
