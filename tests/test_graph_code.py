import textwrap
import xml.dom.minidom

from commons_memory.codeindex import code_impact, code_symbols, index_repo
from commons_memory.graphexport import export


def make_repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "db.py").write_text(textwrap.dedent('''
        def connect(url):
            """Open a DB connection."""
            return url

        class Repo:
            def save(self, x):
                return connect("db")
    '''))
    (tmp_path / "pkg" / "api.py").write_text(textwrap.dedent('''
        from pkg.db import Repo, connect

        def handler(req):
            return Repo().save(req)
    '''))
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "util.ts").write_text(textwrap.dedent('''
        export function formatPrice(n: number): string { return n.toFixed(2); }
        export const total = (xs: number[]) => xs.reduce((a, b) => a + b, 0);
    '''))
    (tmp_path / "web" / "cart.tsx").write_text(textwrap.dedent('''
        import { formatPrice, total } from "./util";
        export class Cart extends Base {
          render(): string {
            return formatPrice(total([1, 2]));
          }
        }
        function helper() { return new Cart(); }
    '''))
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("function junk(){}")
    return tmp_path


def test_index_symbols_impact(store, tmp_path):
    repo = make_repo(tmp_path)
    st = index_repo(store, str(repo), "demo")
    assert st["files"] == 5 and st["symbols"] >= 8 and st["imports"] >= 2 and st["calls"] >= 3
    names = {s["name"] for s in code_symbols(store, "", "demo", limit=100)}
    assert {"connect", "Repo", "Repo.save", "handler", "formatPrice", "total", "Cart", "Cart.render", "helper"} <= names
    assert "junk" not in names
    c = code_symbols(store, "connect", "demo")[0]
    assert c["path"] == "pkg/db.py" and c["line"] == 2 and c["doc"] == "Open a DB connection."
    imp = code_impact(store, "connect", "demo")
    assert "pkg/db.py" in imp["files"] and any(i["name"] == "Repo.save" for i in imp["impacted"])
    assert any(i["name"] == "handler" for i in imp["impacted"])  # transitive
    js = code_impact(store, "formatPrice", "demo")
    assert any(i["name"] == "Cart.render" for i in js["impacted"])
    # re-index is idempotent
    st2 = index_repo(store, str(repo), "demo")
    assert st2["symbols"] == st["symbols"]
    assert len(code_symbols(store, "connect", "demo")) == 1


def test_graph_query_neighbors_and_exports(store, tmp_path):
    index_repo(store, str(make_repo(tmp_path)), "demo")
    sym = code_symbols(store, "connect", "demo")[0]["id"]
    m = store.add_item("memory", "connect() must retry on timeout", project="demo", related_ids=[sym])
    nb = store.neighbors(sym)
    assert any(e["src"] == m["id"] for e in nb["edges"])
    gq = store.graph_query(name="Repo", type_="class", project="demo", rel_type="defines")
    assert gq["matches"] and any(e["type"] == "defines" for e in gq["edges"])
    g = store.export_graph("demo")
    assert m["id"] in g["nodes"]
    mer = export(g, "mermaid")
    assert mer.startswith("graph LR") and "-->|defines|" in mer
    xml.dom.minidom.parseString(export(g, "graphml"))
    html = export(g, "html")
    assert "<canvas" in html and sym in html
    code_only = store.export_graph("demo", include_items=False)
    assert m["id"] not in code_only["nodes"]
