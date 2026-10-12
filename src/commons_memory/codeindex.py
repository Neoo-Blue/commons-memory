"""Code indexer: files, symbols, imports and calls for Python (ast) and JS/TS (tree-sitter when installed —
`pip install commons-memory[code]` — else a lightweight regex parser).

Call targets are *resolved*: same-file definitions, imported bindings (`from x import f as g`, `import {f as g} from
'./x'`, `const m = require('./x')`, namespace imports), `self.`/`this.` methods of the enclosing class, then a globally
unique name. Each `calls` edge carries meta.resolved = local|import|self|unique|ambiguous.

Writes into the shared graph so code entities link with memories, decisions and sessions.
Entity types: repo(project), file, module, function, class, method.
Relations: contains, defines, imports, calls, inherits.
"""
from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

from .store import Store

PY_EXT = {".py"}
JS_EXT = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".tox", ".mypy_cache",
             ".pytest_cache", ".next", "coverage", "site-packages"}
MAX_FILE_BYTES = 512_000


def index_repo(store: Store, root: str, project: str | None = None, agent_id: str | None = None) -> dict:
    rootp = Path(root).resolve()
    project = project or rootp.name
    pid = store.project_id(project, str(rootp))
    stats = {"project": project, "project_id": pid, "files": 0, "symbols": 0, "imports": 0, "calls": 0, "skipped": 0}
    symtab: dict[str, list[str]] = {}       # short name -> entity ids
    pending_calls: list[tuple] = []  # (caller id, callee name, receiver|None, file id, enclosing class|None)
    ctx = {"defs": {}, "bindings": {}, "parser": None}  # per-file top-level defs and import bindings
    modules: dict[str, str] = {}            # module dotted name / rel path -> file id
    pending_imports: list[tuple[str, str]] = []
    with store.lock:
        store.db.execute("BEGIN")
        try:
            kept = _wipe_code_graph(store, pid)  # inside the transaction: a failed re-index keeps the old graph
            for dirpath, dirnames, filenames in os.walk(rootp):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
                for fn in sorted(filenames):
                    path = Path(dirpath) / fn
                    ext = path.suffix.lower()
                    if ext not in PY_EXT | JS_EXT:
                        continue
                    if path.stat().st_size > MAX_FILE_BYTES:
                        stats["skipped"] += 1
                        continue
                    rel = path.relative_to(rootp).as_posix()
                    try:
                        src = path.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        stats["skipped"] += 1
                        continue
                    fid = store.upsert_entity("file", rel, pid, rel, meta={"lang": "python" if ext in PY_EXT else "js",
                                                                            "lines": src.count("\n") + 1})
                    store.relate(pid, fid, "contains")
                    stats["files"] += 1
                    modules[rel] = fid
                    modules[re.sub(r"\.[^.]+$", "", rel)] = fid
                    if ext in PY_EXT:
                        mod = re.sub(r"(/__init__)?\.py$", "", rel).replace("/", ".")
                        modules[mod] = fid
                        for part in ("src.",):
                            if mod.startswith(part):
                                modules[mod[len(part):]] = fid
                        _index_python(store, pid, fid, rel, src, symtab, pending_calls, pending_imports, stats, ctx)
                    elif _ts_parser(ext) is not None:
                        _index_js_ts(store, pid, fid, rel, src, ext, symtab, pending_calls, pending_imports, stats, ctx)
                        stats["parser_js"] = "tree-sitter"
                    else:
                        _index_js(store, pid, fid, rel, src, symtab, pending_calls, pending_imports, stats, ctx)
                        stats["parser_js"] = "regex"
            # resolve imports to files where possible, else to module entities
            for fid, target in pending_imports:
                dst = modules.get(target) or modules.get(_resolve_rel(store, fid, target)) or \
                    store.upsert_entity("module", target, pid)
                store.relate(fid, dst, "imports")
                stats["imports"] += 1
            stats["calls_resolved"] = stats["calls_ambiguous"] = 0
            seen_edges = set()
            for caller, name, recv, cfid, cls in pending_calls:
                targets, how = _resolve_call(store, name, recv, cfid, cls, ctx, modules, symtab)
                for callee in targets:
                    if callee != caller and (caller, callee) not in seen_edges:
                        seen_edges.add((caller, callee))
                        store.relate(caller, callee, "calls", meta={"resolved": how})
                        stats["calls"] += 1
                        stats["calls_ambiguous" if how == "ambiguous" else "calls_resolved"] += 1
            stats["relinked"] = _relink(store, pid, kept)
            store.db.execute("COMMIT")
        except Exception:
            store.db.execute("ROLLBACK")
            raise
    store.audit(agent_id, "code.index", pid, stats)
    return stats


CODE_RELS = ("contains", "defines", "imports", "calls", "inherits")


def _wipe_code_graph(store: Store, pid: str) -> list[tuple]:
    """Delete the project's code entities and edges for a clean re-index, returning the other edges that touched
    them (a memory that `mentions` a function, a decision `about` a file) keyed by (type, name, path) so _relink
    can re-attach them to the re-created entities."""
    old = {r["id"]: (r["type"], r["name"], r["path"]) for r in store.q(
        "SELECT id, type, name, path FROM entities WHERE project_id=? AND type IN ('file','function','class','method','module')",
        (pid,))}
    ids, kept = list(old), []
    ph_types = ",".join("?" * len(CODE_RELS))
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        ph = ",".join("?" * len(chunk))
        for r in store.q(f"SELECT src, dst, type, meta FROM relations WHERE (src IN ({ph}) OR dst IN ({ph})) "
                         f"AND type NOT IN ({ph_types})", chunk + chunk + list(CODE_RELS)):
            kept.append((old.get(r["src"], r["src"]), old.get(r["dst"], r["dst"]), r["type"], r["meta"]))
        store.x(f"DELETE FROM relations WHERE src IN ({ph}) OR dst IN ({ph})", chunk + chunk)
        store.x(f"DELETE FROM entities WHERE id IN ({ph})", chunk)
    return kept


def _relink(store: Store, pid: str, kept: list[tuple]) -> int:
    n = 0
    for src, dst, typ, meta in kept:
        ends = []
        for e in (src, dst):
            if isinstance(e, tuple):
                r = store.q("SELECT id FROM entities WHERE project_id=? AND type=? AND name=? AND path IS ?", (pid, *e))
                e = r[0]["id"] if r else None
            ends.append(e)
        if all(ends):
            store.relate(ends[0], ends[1], typ, json.loads(meta) if meta else None)
            n += 1
    return n


def _target_file(store, fid, target, modules):
    t = modules.get(target) or modules.get(_resolve_rel(store, fid, target))
    if t:
        return t
    rel = _resolve_rel(store, fid, target)
    for cand in (rel + "/index", rel.rstrip("/") + "/__init__"):
        if cand in modules:
            return modules[cand]
    return None


def _resolve_call(store, name, recv, fid, cls, ctx, modules, symtab):
    defs = ctx["defs"]
    binds = ctx["bindings"].get(fid, {})
    if recv in ("self", "this", "cls") and cls:
        e = defs.get(fid, {}).get(f"{cls}.{name}")
        if e:
            return [e], "self"
    if recv and recv in binds:  # module alias / namespace import: alias.name(...)
        mod, imported = binds[recv]
        tf = _target_file(store, fid, mod, modules)
        if tf:
            e = defs.get(tf, {}).get(name if imported is None else f"{imported}.{name}") or defs.get(tf, {}).get(name)
            if e:
                return [e], "import"
    if recv is None:
        e = defs.get(fid, {}).get(name)
        if e:
            return [e], "local"
        if name in binds:
            mod, imported = binds[name]
            tf = _target_file(store, fid, mod, modules)
            if tf:
                e = defs.get(tf, {}).get(imported or name)
                if e:
                    return [e], "import"
    cands = symtab.get(name, [])
    if len(cands) == 1:
        return cands, "unique"
    if recv not in ("self", "this") and 1 < len(cands) <= 3:
        return cands, "ambiguous"
    return [], "unresolved"


def _resolve_rel(store: Store, fid: str, target: str) -> str:
    if not target.startswith("."):
        return target
    r = store.q("SELECT path FROM entities WHERE id=?", (fid,))
    base = Path(r[0]["path"]).parent if r else Path(".")
    p = os.path.normpath((base / target).as_posix())
    return p


def _index_python(store, pid, fid, rel, src, symtab, pending_calls, pending_imports, stats, ctx):
    defs = ctx["defs"].setdefault(fid, {})
    binds = ctx["bindings"].setdefault(fid, {})
    try:
        tree = ast.parse(src)
    except SyntaxError:
        stats["skipped"] += 1
        return

    def visit(node, parent_id, prefix, in_class):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                is_class = isinstance(child, ast.ClassDef)
                typ = "class" if is_class else ("method" if in_class else "function")
                qual = f"{prefix}{child.name}"
                doc = (ast.get_docstring(child) or "").split("\n")[0][:160]
                sig = ""
                if not is_class:
                    sig = f"({', '.join(a.arg for a in child.args.args)})"
                eid = store.upsert_entity(typ, qual, pid, rel, child.lineno,
                                          {"doc": doc, "signature": sig, "end_line": getattr(child, "end_lineno", None)})
                store.relate(parent_id, eid, "defines")
                symtab.setdefault(child.name, []).append(eid)
                defs.setdefault(qual, eid)
                if not prefix:
                    defs.setdefault(child.name, eid)
                stats["symbols"] += 1
                cls_name = qual if is_class else (prefix[:-1] if in_class else None)
                if is_class:
                    for b in child.bases:
                        bn, br = _py_call(b)
                        if bn:
                            pending_calls.append((eid, bn, br, fid, None))  # inherits ~ depends on
                for sub in _walk_own(child):
                    if isinstance(sub, ast.Call):
                        n, r = _py_call(sub.func)
                        if n:
                            pending_calls.append((eid, n, r, fid, cls_name))
                visit(child, eid, qual + ".", is_class)
            elif isinstance(child, ast.Import):
                for a in child.names:
                    pending_imports.append((fid, a.name))
                    binds[a.asname or a.name.split(".")[0]] = (a.name if a.asname else a.name.split(".")[0], None)
            elif isinstance(child, ast.ImportFrom):
                mod = ("." * child.level) + (child.module or "")
                if child.level:
                    base = rel.rsplit("/", 1)[0].replace("/", ".") if "/" in rel else ""
                    parts = base.split(".") if base else []
                    parts = parts[: len(parts) - (child.level - 1)] if child.level > 1 else parts
                    mod = ".".join(parts + ([child.module] if child.module else []))
                    if mod.startswith("src."):
                        mod = mod[4:]
                pending_imports.append((fid, mod))
                for a in child.names:
                    if a.name != "*":
                        binds[a.asname or a.name] = (mod, a.name)
                        binds.setdefault("__from__" + (a.asname or a.name), (f"{mod}.{a.name}", None))
            elif not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if parent_id == fid:
                    for sub in ast.walk(child):
                        if isinstance(sub, ast.Call):
                            n, r = _py_call(sub.func)
                            if n:
                                pending_calls.append((fid, n, r, fid, None))

    visit(tree, fid, "", False)


def _walk_own(node):
    """ast.walk that does not descend into nested function/class definitions (they record their own calls)."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        n = stack.pop()
        yield n
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(n))


def _py_call(node) -> tuple[str | None, str | None]:
    """(callee name, receiver) — receiver is the simple name before the dot (self, module alias), else None."""
    if isinstance(node, ast.Name):
        return node.id, None
    if isinstance(node, ast.Attribute):
        return node.attr, node.value.id if isinstance(node.value, ast.Name) else "?"
    return None, None


def _py_name(node) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


_JS_IMPORT = re.compile(r"""(?:import\s[^'"]*?from\s*|import\s*\(\s*|require\s*\(\s*|import\s+)['"]([^'"]+)['"]""")
_JS_FUNC = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(", re.M)
_JS_ARROW = re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=>", re.M)
_JS_CLASS = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)(?:\s+extends\s+([A-Za-z_$][\w$.]*))?", re.M)
_JS_METHOD = re.compile(r"^\s+(?:public\s+|private\s+|protected\s+|static\s+|async\s+|readonly\s+)*([A-Za-z_$][\w$]*)\s*\([^;{]*\)\s*(?::\s*[^{;]+)?\{", re.M)
_JS_CALL = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
_JS_KEYWORDS = {"if", "for", "while", "switch", "catch", "function", "return", "typeof", "new", "super", "constructor",
                "require", "import", "await", "async", "yield", "delete", "void", "with", "else"}


def _block_end(src: str, start: int) -> int:
    i = src.find("{", start)
    if i < 0:
        return len(src)
    depth = 0
    for j in range(i, len(src)):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j + 1
    return len(src)


def _index_js(store, pid, fid, rel, src, symtab, pending_calls, pending_imports, stats, ctx):
    defs = ctx["defs"].setdefault(fid, {})
    line_of = lambda pos: src.count("\n", 0, pos) + 1  # noqa: E731
    for m in _JS_IMPORT.finditer(src):
        pending_imports.append((fid, m.group(1)))
    spans = []
    for m in _JS_CLASS.finditer(src):
        name = m.group(1)
        end = _block_end(src, m.end())
        cid = store.upsert_entity("class", name, pid, rel, line_of(m.start()), {"extends": m.group(2)})
        store.relate(fid, cid, "defines")
        symtab.setdefault(name, []).append(cid)
        stats["symbols"] += 1
        defs.setdefault(name, cid)
        if m.group(2):
            pending_calls.append((cid, m.group(2).split(".")[-1], None, fid, None))
        body = src[m.end():end]
        for mm in _JS_METHOD.finditer(body):
            mname = mm.group(1)
            if mname in _JS_KEYWORDS:
                continue
            mstart = m.end() + mm.start()
            mend = _block_end(src, m.end() + mm.end() - 1)
            mid = store.upsert_entity("method", f"{name}.{mname}", pid, rel, line_of(mstart))
            store.relate(cid, mid, "defines")
            symtab.setdefault(mname, []).append(mid)
            defs.setdefault(f"{name}.{mname}", mid)
            stats["symbols"] += 1
            spans.append((mid, mstart, mend, name))
    for rx in (_JS_FUNC, _JS_ARROW):
        for m in rx.finditer(src):
            name = m.group(1)
            end = _block_end(src, m.end()) if "{" in src[m.end():m.end() + 200] else src.find("\n", m.end())
            eid = store.upsert_entity("function", name, pid, rel, line_of(m.start()))
            store.relate(fid, eid, "defines")
            symtab.setdefault(name, []).append(eid)
            defs.setdefault(name, eid)
            stats["symbols"] += 1
            spans.append((eid, m.end(), end if end > 0 else len(src), None))
    for eid, s, e, cls in spans:
        for c in _JS_CALL.finditer(src[s:e]):
            if c.group(1) not in _JS_KEYWORDS:
                before = src[s:e][max(0, c.start() - 6):c.start()]
                recv = "this" if before.endswith("this.") else ("?" if before.endswith(".") else None)
                pending_calls.append((eid, c.group(1), recv, fid, cls))


# ---------------------------------------------------------------------------- tree-sitter JS/TS
_TS_CACHE: dict[str, object] = {}


def _ts_parser(ext: str):
    if os.environ.get("COMMONS_TREE_SITTER", "1") != "1" or ext not in JS_EXT:
        return None
    key = {".ts": "ts", ".tsx": "tsx"}.get(ext, "js")
    if key in _TS_CACHE:
        return _TS_CACHE[key]
    try:
        from tree_sitter import Language, Parser
        if key == "js":
            import tree_sitter_javascript as m
            lang = Language(m.language())
        else:
            import tree_sitter_typescript as m
            lang = Language(m.language_typescript() if key == "ts" else m.language_tsx())
        _TS_CACHE[key] = Parser(lang)
    except Exception:  # noqa: BLE001 - optional dependency
        _TS_CACHE[key] = None
    return _TS_CACHE[key]


def _t(n) -> str:
    return n.text.decode("utf-8", "replace") if n is not None else ""


def _index_js_ts(store, pid, fid, rel, src, ext, symtab, pending_calls, pending_imports, stats, ctx):
    tree = _ts_parser(ext).parse(src.encode())
    defs = ctx["defs"].setdefault(fid, {})
    binds = ctx["bindings"].setdefault(fid, {})

    def define(typ, name, node, parent, meta=None):
        eid = store.upsert_entity(typ, name, pid, rel, node.start_point[0] + 1,
                                  (meta or {}) | {"end_line": node.end_point[0] + 1})
        store.relate(parent, eid, "defines")
        symtab.setdefault(name.split(".")[-1], []).append(eid)
        defs.setdefault(name, eid)
        stats["symbols"] += 1
        return eid

    def calls_in(node, owner, cls):
        stack = list(node.children)
        while stack:
            n = stack.pop()
            if n.type in ("function_declaration", "class_declaration", "method_definition", "arrow_function",
                          "function_expression", "generator_function_declaration") and n is not node:
                if n.type in ("arrow_function", "function_expression") and n.parent is not None \
                        and n.parent.type != "variable_declarator":
                    pass  # inline callbacks: their calls belong to the enclosing function
                else:
                    continue
            if n.type == "call_expression":
                f = n.child_by_field_name("function")
                if f is not None and f.type == "identifier" and _t(f) not in _JS_KEYWORDS:
                    pending_calls.append((owner, _t(f), None, fid, cls))
                elif f is not None and f.type == "member_expression":
                    obj, prop = f.child_by_field_name("object"), f.child_by_field_name("property")
                    recv = "this" if obj is not None and obj.type == "this" else (_t(obj) if obj is not None and obj.type == "identifier" else "?")
                    pending_calls.append((owner, _t(prop), recv, fid, cls))
            elif n.type == "new_expression":
                c = n.child_by_field_name("constructor")
                if c is not None and c.type == "identifier":
                    pending_calls.append((owner, _t(c), None, fid, cls))
            stack.extend(n.children)

    def handle_import(n):
        srcn = n.child_by_field_name("source")
        mod = _t(srcn).strip("'\"`") if srcn is not None else ""
        if not mod:
            return
        pending_imports.append((fid, mod))
        for c in n.children:
            if c.type != "import_clause":
                continue
            for cc in c.children:
                if cc.type == "identifier":
                    binds[_t(cc)] = (mod, "default")
                elif cc.type == "namespace_import":
                    ids = [x for x in cc.children if x.type == "identifier"]
                    if ids:
                        binds[_t(ids[-1])] = (mod, None)
                elif cc.type == "named_imports":
                    for sp in cc.children:
                        if sp.type == "import_specifier":
                            nm, al = sp.child_by_field_name("name"), sp.child_by_field_name("alias")
                            binds[_t(al or nm)] = (mod, _t(nm))

    def walk(node, parent, cls=None):
        for n in node.children:
            t = n.type
            if t == "export_statement":
                walk(n, parent, cls)
                if n.child_by_field_name("source") is not None:
                    handle_import(n)
            elif t == "import_statement":
                handle_import(n)
            elif t in ("function_declaration", "generator_function_declaration"):
                name = _t(n.child_by_field_name("name"))
                params = _t(n.child_by_field_name("parameters"))
                eid = define("function", name, n, parent, {"signature": params[:120]})
                calls_in(n, eid, None)
            elif t in ("class_declaration", "abstract_class_declaration", "class"):
                name = _t(n.child_by_field_name("name")) or "anonymous"
                ext_name = None
                for c in n.children:
                    if c.type == "class_heritage":
                        ids = [x for x in c.children if x.type in ("identifier", "extends_clause")]
                        for x in ids:
                            v = x.child_by_field_name("value") if x.type == "extends_clause" else x
                            ext_name = _t(v) if v is not None else _t(x)
                            break
                        if ext_name is None:
                            ext_name = _t(c).replace("extends", "").strip().split()[0] if _t(c) else None
                cid = define("class", name, n, parent, {"extends": ext_name})
                if ext_name:
                    pending_calls.append((cid, ext_name.split(".")[-1], None, fid, None))
                body = n.child_by_field_name("body")
                for m in (body.children if body is not None else []):
                    if m.type in ("method_definition", "method_signature", "abstract_method_signature"):
                        mn = _t(m.child_by_field_name("name"))
                        if mn and mn != "constructor" or m.type == "method_definition":
                            mid = define("method", f"{name}.{mn}", m, cid,
                                         {"signature": _t(m.child_by_field_name("parameters"))[:120]})
                            calls_in(m, mid, name)
            elif t in ("lexical_declaration", "variable_declaration"):
                for d in n.children:
                    if d.type != "variable_declarator":
                        continue
                    nm, val = d.child_by_field_name("name"), d.child_by_field_name("value")
                    if val is None or nm is None:
                        continue
                    if val.type in ("arrow_function", "function_expression", "function"):
                        eid = define("function", _t(nm), d, parent, {"signature": _t(val.child_by_field_name("parameters"))[:120]})
                        calls_in(val, eid, None)
                    elif val.type == "call_expression" and _t(val.child_by_field_name("function")) == "require":
                        args = val.child_by_field_name("arguments")
                        strs = [x for x in (args.children if args is not None else []) if x.type == "string"]
                        if strs:
                            mod = _t(strs[0]).strip("'\"`")
                            pending_imports.append((fid, mod))
                            if nm.type == "identifier":
                                binds[_t(nm)] = (mod, None)
                            elif nm.type == "object_pattern":
                                for p_ in nm.children:
                                    if p_.type in ("shorthand_property_identifier_pattern", "identifier"):
                                        binds[_t(p_)] = (mod, _t(p_))
                    elif parent == fid:
                        calls_in(d, fid, None)
            elif t == "expression_statement" and parent == fid:
                calls_in(n, fid, None)

    walk(tree.root_node, fid)
    # default export binding: `export default function x` / `export default X`
    for n in tree.root_node.children:
        if n.type == "export_statement" and any(c.type == "default" for c in n.children):
            for c in n.children:
                nm = c.child_by_field_name("name") if c.child_count else None
                if nm is not None and _t(nm) in defs:
                    defs.setdefault("default", defs[_t(nm)])
                elif c.type == "identifier" and _t(c) in defs:
                    defs.setdefault("default", defs[_t(c)])


def code_symbols(store: Store, name: str = "", project: str | None = None, types: list[str] | None = None,
                 path: str | None = None, limit: int = 50) -> list[dict]:
    where, args = ["type IN ('function','class','method')"], []
    if types:
        where = [f"type IN ({','.join('?' * len(types))})"]
        args += types
    if name:
        where.append("(name LIKE ? OR name LIKE ?)"); args += [f"%{name}%", f"%.{name}"]
    if project:
        where.append("project_id=?"); args.append(store.find_project(project) or "__none__")
    if path:
        where.append("path LIKE ?"); args.append(f"%{path}%")
    rows = store.q(f"SELECT id,type,name,path,line,meta FROM entities WHERE {' AND '.join(where)} "
                   f"ORDER BY (name=?) DESC, length(name) LIMIT ?", args + [name, limit])
    import json
    out = []
    for r in rows:
        m = json.loads(r["meta"] or "{}")
        out.append({"id": r["id"], "type": r["type"], "name": r["name"], "path": r["path"], "line": r["line"],
                    "signature": m.get("signature", ""), "doc": m.get("doc", "")})
    return out


def code_impact(store: Store, symbol: str, project: str | None = None, depth: int = 3, limit: int = 100) -> dict:
    """Reverse dependency walk: what calls/imports/inherits this symbol (transitively)."""
    if store.q("SELECT 1 FROM entities WHERE id=?", (symbol,)):
        roots = [symbol]
    else:
        roots = [s["id"] for s in code_symbols(store, symbol, project, limit=10)
                 if s["name"] == symbol or s["name"].endswith("." + symbol)]
        if not roots:
            roots = [r["id"] for r in store.q("SELECT id FROM entities WHERE type='file' AND path LIKE ?", (f"%{symbol}",))]
    seen, frontier, impacted = set(roots), list(roots), []
    for d in range(1, depth + 1):
        nxt = []
        for node in frontier:
            for r in store.q("SELECT src, type FROM relations WHERE dst=? AND type IN ('calls','imports')", (node,)):
                if r["src"] in seen:
                    continue
                seen.add(r["src"])
                nxt.append(r["src"])
                e = store.q("SELECT id,type,name,path,line FROM entities WHERE id=?", (r["src"],))
                if e:
                    impacted.append(dict(e[0]) | {"depth": d, "via": r["type"]})
            # a symbol is also impacted through its file's importers
            if d == 1:
                for r in store.q("SELECT src FROM relations WHERE dst=? AND type='defines'", (node,)):
                    if r["src"] not in seen:
                        seen.add(r["src"]); nxt.append(r["src"])
        frontier = nxt
        if len(impacted) >= limit:
            break
    files = sorted({i["path"] for i in impacted if i.get("path")})
    linked = []
    for root in roots:
        linked += [r["src"] for r in store.q("SELECT src FROM relations r JOIN items i ON i.id=r.src WHERE r.dst=?", (root,))]
    return {"roots": roots, "impacted": impacted[:limit], "files": files, "linked_memories": linked,
            "text": "\n".join(f"{'  ' * (i['depth'] - 1)}{i['name']} ({i['type']}, {i['path']}:{i['line']}) via {i['via']}"
                              for i in impacted[:limit]) or "(no dependents found)"}
