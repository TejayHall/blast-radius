"""Fixture-case filtering for sqlglot's fixture-driven tests.

tests/helpers.py's load_sql_fixture_pairs / load_sql_fixtures feed every case of a fixture file to the
test (TestOptimizer.check_file, TestExecutor's TPC-H/TPC-DS lists). With the wrappers installed, each
fixture's items can be counted, or restricted to a set of indices. A case is identified by
(fixture file name as passed to the loader, index of the item in that file).
"""
import sys

MODE = ["off"]   # "off" | "count" | "filter"
FILTER = {}      # fixture -> set of allowed indices (fixtures absent from FILTER are unrestricted)
COUNTS = {}      # fixture -> items in the file (max over calls)

# executor classes load their TPC lists once in setUpClass
CLASS_LISTS = {
    "TestExecutor": {"tpch_sqls": "optimizer/tpc-h/tpc-h.sql", "tpcds_sqls": "optimizer/tpc-ds/tpc-ds.sql"},
}
_ORIG = {}


def _wrap(orig):
    def wrapper(filename, *a, **k):
        mode = MODE[0]
        if mode == "off":
            yield from orig(filename, *a, **k)
            return
        allowed = FILTER.get(filename) if mode == "filter" else None
        n = 0
        for item in orig(filename, *a, **k):
            if allowed is None or n in allowed:
                yield item
            n += 1
        if mode == "count":
            COUNTS[filename] = max(COUNTS.get(filename, 0), n)

    wrapper.__br_wrapped__ = True
    return wrapper


def install():
    """Wrap the loaders in tests.helpers and in every already-imported test module that bound them."""
    import tests.helpers as h

    for name in ("load_sql_fixture_pairs", "load_sql_fixtures"):
        fn = getattr(h, name)
        if not getattr(fn, "__br_wrapped__", False):
            _ORIG[name] = fn
            setattr(h, name, _wrap(fn))
    patch_modules()


def patch_modules():
    import tests.helpers as h

    for modname, mod in list(sys.modules.items()):
        if mod is None or not modname.startswith("tests."):
            continue
        for name in ("load_sql_fixture_pairs", "load_sql_fixtures"):
            cur = getattr(mod, name, None)
            if cur is not None and not getattr(cur, "__br_wrapped__", False):
                setattr(mod, name, getattr(h, name))


def apply_class_filters(klass):
    """Slice class-level case lists loaded (unfiltered) by an earlier setUpClass."""
    spec = CLASS_LISTS.get(klass.__name__)
    if not spec:
        return
    for attr, fixture in spec.items():
        allowed = FILTER.get(fixture)
        if allowed is None or not hasattr(klass, attr):
            continue
        items = getattr(klass, attr)
        setattr(klass, attr, [x for i, x in enumerate(items) if i in allowed])
