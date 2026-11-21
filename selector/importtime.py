"""Map a differential-import result to the tests that can observe it.

Every changed piece of import-time state is one of:
  * a module that no longer imports      -> every test that imported it (they all error);
  * a class whose bases/MRO changed      -> every test that imported the class's module;
  * an entry of a dict/set attribute     -> tests that read that key at run time, or iterated the
                                            container, or (if keys were added/removed) took its size;
  * a function/method object             -> tests that call it (run-time coverage of its body);
  * any other value                      -> tests that execute a run-time line mentioning its name.
Because the diff enumerates *all* changed state (derived state included), each test outside the union
sees exactly the state it saw on the pinned commit.
"""
import ast
import gzip
import json
import marshal
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
KEYS_JSON = os.path.join(DATA, "keys.json.gz")
KEYS_CACHE = os.path.join(DATA, "keys.marshal")


def build_keys_cache():
    with gzip.open(KEYS_JSON, "rt", encoding="utf-8") as fh:
        doc = json.load(fh)
    doc["sets"] = [tuple(s) for s in doc["sets"]]
    with open(KEYS_CACHE, "wb") as fh:
        marshal.dump(doc, fh)


class Keys:
    def __init__(self, ix):
        if os.path.exists(KEYS_CACHE):
            with open(KEYS_CACHE, "rb") as fh:
                doc = marshal.load(fh)
        else:
            with gzip.open(KEYS_JSON, "rt", encoding="utf-8") as fh:
                doc = json.load(fh)
        # remap key-trace test order to the coverage index order
        order = doc["tests"]
        self.remap = [ix.pos[t] for t in order]
        self.sets = doc["sets"]
        self.acc = doc["acc"]
        self.inst = doc["inst"]

    def tests(self, sid):
        if sid is None:
            return set()
        rm = self.remap
        return {rm[i] for i in self.sets[sid]}

    def accessors(self, tag, keys):
        d = self.acc.get(tag)
        out = set()
        if not d:
            return out
        for k in keys:
            out |= self.tests(d.get(k))
        return out

    def inst_tests(self, qual):
        return self.tests(self.inst.get(qual))

    def inst_by_key(self, key):
        """Tests that instantiated any expression class whose key (lower-cased class name) is `key`."""
        out = set()
        for q, sid in self.inst.items():
            if q.rsplit(".", 1)[-1].lower() == key:
                out |= self.tests(sid)
        return out

    def any_access(self, tag):
        d = self.acc.get(tag)
        out = set()
        if d:
            for sid in d.values():
                out |= self.tests(sid)
        return out


def module_to_path(root, mod):
    base = os.path.join(root, *mod.split("."))
    if os.path.exists(base + ".py"):
        return mod.replace(".", "/") + ".py"
    if os.path.exists(os.path.join(base, "__init__.py")):
        return mod.replace(".", "/") + "/__init__.py"
    return mod.replace(".", "/") + ".py"


def _find_def(tree, qual):
    """Locate def/class by dotted qualname inside a module AST. Returns node or None."""
    parts = qual.split(".")
    body = tree.body
    node = None
    for p in parts:
        found = None
        for s in body:
            if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and s.name == p:
                found = s
        if found is None:
            return None
        node = found
        body = found.body
    return node


def candidates_from_diff(res, ix, keys, analyzer, root, notes, patched_modules=()):
    C = set()
    patched_modules = set(patched_modules)
    for m in sorted(res.get("failed", {})):
        if m.startswith("dialect:"):
            continue
        if m == "sqlglot":
            notes.append("diff: sqlglot itself fails to import -> all tests")
            return ix.all_tests()
        path = module_to_path(root, m)
        c = ix.file_any(path)
        notes.append(f"diff: {m} fails to import -> {len(c)} importers")
        C |= c
    for mod, tag, key, typ in res.get("changes", []):
        if typ == "function_code":
            continue
        path = module_to_path(root, mod)
        if typ == "ref":
            # a global that refers to an object defined elsewhere (an import) was added, removed or
            # re-pointed. Only meaningful in a module the patch edits (elsewhere it is lazy-attribute
            # noise): every run-time use of that name may now hit a NameError or a different object.
            if mod not in patched_modules or not tag.startswith("M:"):
                continue
            name = tag.rsplit(":", 1)[1]
            users = analyzer.runtime_name_users(name)
            c = users & ix.file_any(path)
            notes.append(f"diff: imported name {tag} changed -> name users {len(users)} in importers = {len(c)}")
            C |= c
            continue
        if tag.startswith("K:"):
            c = ix.file_any(path)
            notes.append(f"diff: class {tag[2:]} bases changed -> {len(c)}")
            C |= c
            continue
        if typ in ("dict", "set"):
            ks = [key, "*"]
            c = keys.accessors(tag, ks)
            notes.append(f"diff: {tag}[{key}] -> {len(c)}")
            C |= c
            continue
        if typ in ("d", "s"):
            c = keys.any_access(tag)
            notes.append(f"diff: {tag} (whole container) -> {len(c)}")
            C |= c
            continue
        kind, rest = tag.split(":", 1)
        qual, attr = rest.rsplit(":", 1)
        if typ in ("function", "staticmethod", "classmethod", "property", "cached_property"):
            cls_qual = qual[len(mod) + 1:] if qual.startswith(mod + ".") else ""
            if kind == "M":
                cls_qual = ""
            full = f"{cls_qual}.{attr}" if cls_qual else attr
            try:
                tree = analyzer._tree(path)
                node = _find_def(tree, full)
            except (SyntaxError, OSError):
                node = None
            if node is not None and getattr(node, "body", None):
                c = ix.fnr_tests(path, node.body[0].lineno, node.end_lineno) | ix.fn_tests(path, node.lineno, node.lineno)
                notes.append(f"diff: function {full} -> callers {len(c)}")
                C |= c
                continue
            if node is None and kind == "C":
                # a method the pinned tree does not have: it overrides an inherited one (whose callers now
                # reach it) or, for generators, `<key>_sql` is picked up by the dispatch table for that
                # expression class
                imp = ix.file_any(path)
                c = analyzer.same_name_callers(attr) & imp
                if attr.endswith("_sql"):
                    c |= keys.inst_by_key(attr[:-4]) & imp
                notes.append(f"diff: new method {full} -> same-name callers / instances {len(c)}")
                C |= c
                # and anything naming it, below
        # scalar / other object: readers by name at run time, restricted to tests that loaded the module
        users = analyzer.runtime_name_users(attr)
        imp = ix.file_any(path)
        c = users & imp
        subs = res.get("subclasses", {}).get(qual)
        if subs and kind == "C":
            # an Expression class attribute is read through instances of the class (or a subclass), by
            # code naming the class, or by generic walks over the class registries
            near = set()
            for q in subs:
                near |= keys.inst_tests(q)
            near |= analyzer.runtime_name_users(qual.rsplit(".", 1)[-1])
            # generic walks over every class: iterating the registries at run time
            for reg in ("M:sqlglot.expressions:FUNCTION_BY_NAME", "M:sqlglot.expressions:EXPR_CLASSES"):
                near |= keys.accessors(reg, ["*"])
            for reg in ("ALL_FUNCTIONS", "__subclasses__"):
                near |= analyzer.runtime_name_users(reg)
            c2 = c & near
            notes.append(f"diff: {tag} ({typ}) -> name users {len(users)} & importers {len(imp)} = {len(c)}; "
                         f"via instances/registries {len(c2)}")
            C |= c2
            continue
        notes.append(f"diff: {tag} ({typ}) -> name users {len(users)} & importers {len(imp)} = {len(c)}")
        C |= c
    return C
