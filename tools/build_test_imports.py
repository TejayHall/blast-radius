"""Which sqlglot names each test file imports while it is being imported (module/class level, through other
test modules transitively): a name that disappears makes the whole file fail at collection.
Lazy package attributes (sqlglot.optimizer, sqlglot.dialects) resolve to the module that defines them.
Output JSON {"<module>:<name>": [test files]}.  usage: python tools/build_test_imports.py <root> <out.json>"""
import ast
import importlib
import json
import os
import sys


def module_level_imports(path, modname):
    """(module, name) pairs from module-level `from X import a` (not inside functions), and test modules."""
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except (OSError, SyntaxError):
        return set(), set()
    pkg = modname.rsplit(".", 1)[0] if "." in modname else ""
    names, mods = set(), set()

    def visit(body):
        for st in body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(st, ast.ImportFrom):
                if st.level:
                    base = pkg.split(".")
                    if st.level > 1:
                        base = base[: len(base) - (st.level - 1)]
                    src = ".".join(base + ([st.module] if st.module else []))
                else:
                    src = st.module or ""
                for a in st.names:
                    if a.name != "*":
                        names.add((src, a.name))
                mods.add(src)
            elif isinstance(st, ast.Import):
                for a in st.names:
                    mods.add(a.name)
            for field in ("body", "orelse", "finalbody"):
                sub = getattr(st, field, None)
                if isinstance(sub, list) and sub and isinstance(sub[0], ast.stmt):
                    visit(sub)
            if isinstance(st, ast.Try):
                for h in st.handlers:
                    visit(h.body)

    visit(tree.body)
    return names, mods


def main():
    root, out = sys.argv[1], sys.argv[2]
    sys.path.insert(0, root)
    os.chdir(root)
    import unittest  # noqa: F401

    lazy = {}
    try:
        opt = importlib.import_module("sqlglot.optimizer")
        for k, (m, a) in getattr(opt, "_LAZY_ATTRS", {}).items():
            lazy[("sqlglot.optimizer", k)] = (m, a)
    except Exception:  # noqa: BLE001
        pass
    try:
        dia = importlib.import_module("sqlglot.dialects")
        for k, m in getattr(dia, "MODULE_BY_ATTRIBUTE", {}).items():
            lazy[("sqlglot.dialects", k)] = (f"sqlglot.dialects.{m}", k)
    except Exception:  # noqa: BLE001
        pass

    direct = {}
    for dp, _dirs, files in os.walk(os.path.join(root, "tests")):
        if "fixtures" in dp.split(os.sep):
            continue
        for fn in files:
            if fn.endswith(".py"):
                rel = os.path.relpath(os.path.join(dp, fn), root).replace(os.sep, "/")
                mod = rel[:-3].replace("/", ".")
                if mod.endswith(".__init__"):
                    mod = mod[: -len(".__init__")]
                direct[mod] = (rel, *module_level_imports(os.path.join(root, rel), mod))

    def closure(mod, seen):
        if mod in seen or mod not in direct:
            return set()
        seen.add(mod)
        _rel, names, mods = direct[mod]
        acc = set(names)
        for m in mods | {src for src, _ in names}:
            if m.startswith("tests"):
                acc |= closure(m, seen)
                # `from tests.x import y` may also name a submodule
        for src, n in list(names):
            if src.startswith("tests") and f"{src}.{n}" in direct:
                acc |= closure(f"{src}.{n}", seen)
        return acc

    result = {}
    for mod, (rel, _n, _m) in direct.items():
        if not os.path.basename(rel).startswith("test_"):
            continue
        for src, n in closure(mod, set()):
            if not src.startswith("sqlglot"):
                continue
            src, n = lazy.get((src, n), (src, n))
            result.setdefault(f"{src}:{n}", set()).add(rel)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({k: sorted(v) for k, v in sorted(result.items())}, fh, separators=(",", ":"))
    print("names", len(result), "files", len({f for v in result.values() for f in v}), file=sys.stderr)


if __name__ == "__main__":
    main()
