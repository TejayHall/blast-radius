"""Offline trace: per test, which keys of sqlglot's class/module-level dicts and sets it reads at run time,
and which Expression classes it instantiates.

Every test runs in its own fork of a zygote that has imported sqlglot, every dialect and every test module
(so nothing runs before the test). Containers are swapped for recording subclasses after import, so only
run-time accesses are recorded. Pools are replaced by the in-process executor used by the selector.

usage: python trace_keys.py <out_dir> <ids_file> [worker_index n_workers]
"""
import concurrent.futures
import enum
import importlib
import json
import os
import pickle
import pkgutil
import sys
import types
import unittest

sys.path.insert(0, "/sqlglot")
sys.path.insert(0, "/app")
os.chdir("/sqlglot")
os.environ.setdefault("SKIP_INTEGRATION", "0")

ACC = set()
INST = set()
ON = [False]


def kfp(k):
    """Stable key fingerprint."""
    if k is None or isinstance(k, (bool, int, float, str, bytes)):
        return repr(k)
    if isinstance(k, enum.Enum):
        return f"E:{type(k).__qualname__}.{k.name}"
    if isinstance(k, type):
        return f"T:{k.__module__}.{k.__qualname__}"
    if isinstance(k, tuple):
        return "(" + ",".join(kfp(x) for x in k) + ")"
    if isinstance(k, frozenset):
        return "{" + ",".join(sorted(kfp(x) for x in k)) + "}"
    return f"?:{type(k).__qualname__}"


def _rec(tag, key):
    if ON[0]:
        ACC.add((tag, key))


class TDict(dict):
    __slots__ = ("_tag",)

    def __getitem__(self, k):
        _rec(self._tag, kfp(k))
        return dict.__getitem__(self, k)

    def get(self, k, d=None):
        _rec(self._tag, kfp(k))
        return dict.get(self, k, d)

    def __contains__(self, k):
        _rec(self._tag, kfp(k))
        return dict.__contains__(self, k)

    def pop(self, k, *a):
        _rec(self._tag, kfp(k))
        return dict.pop(self, k, *a)

    def setdefault(self, k, d=None):
        _rec(self._tag, kfp(k))
        return dict.setdefault(self, k, d)

    def __setitem__(self, k, v):
        _rec(self._tag, kfp(k))
        return dict.__setitem__(self, k, v)

    def _all(self):
        _rec(self._tag, "*")

    def __iter__(self):
        self._all()
        return dict.__iter__(self)

    def keys(self):
        self._all()
        return dict.keys(self)

    def values(self):
        self._all()
        return dict.values(self)

    def items(self):
        self._all()
        return dict.items(self)

    def __len__(self):
        _rec(self._tag, "#len")
        return dict.__len__(self)

    def copy(self):
        self._all()
        return dict(dict.items(self))

    def __eq__(self, o):
        self._all()
        return dict.__eq__(self, o)

    def __ne__(self, o):
        self._all()
        return dict.__ne__(self, o)

    def __or__(self, o):
        self._all()
        return dict.__or__(dict(dict.items(self)), o)

    def __ror__(self, o):
        self._all()
        return dict.__or__(o, dict(dict.items(self)))

    def __repr__(self):
        self._all()
        return dict.__repr__(self)

    def __reduce_ex__(self, p):
        self._all()
        return (dict, (dict(dict.items(self)),))

    def __bool__(self):
        _rec(self._tag, "#len")
        return dict.__len__(self) > 0

    __hash__ = None


def _mkset(base):
    class TSet(base):
        __slots__ = ("_tag",)

        def __contains__(self, k):
            try:
                _rec(self._tag, kfp(k))
            except Exception:  # noqa: BLE001
                pass
            return base.__contains__(self, k)

        def _all(self):
            _rec(self._tag, "*")

        def __len__(self):
            _rec(self._tag, "#len")
            return base.__len__(self)

        def __bool__(self):
            _rec(self._tag, "#len")
            return base.__len__(self) > 0

    for m in ("__iter__", "__or__", "__and__", "__sub__", "__xor__", "__ror__", "__rand__", "__rsub__",
              "__rxor__", "union", "intersection", "difference", "symmetric_difference", "issubset", "issuperset",
              "isdisjoint", "copy", "__eq__", "__ne__", "__le__", "__lt__", "__ge__", "__gt__", "__repr__"):
        if not hasattr(base, m):
            continue
        orig = getattr(base, m)

        def wrap(self, *a, _o=orig):
            self._all()
            r = _o(self, *a)
            if isinstance(r, (set, frozenset)) and type(r) is not base:
                return r
            return r

        setattr(TSet, m, wrap)

    def reduce_ex(self, p, _b=base):
        self._all()
        return (_b, (list(_b.__iter__(self)),))

    TSet.__reduce_ex__ = reduce_ex
    if base is frozenset:
        TSet.__hash__ = frozenset.__hash__
    return TSet


TSet = _mkset(set)
TFrozen = _mkset(frozenset)


def wrap_value(v, tag, memo):
    t = type(v)
    if t is dict:
        w = memo.get(id(v))
        if w is None:
            w = TDict(v)
            w._tag = tag
            memo[id(v)] = w
        return w
    if t is set:
        w = memo.get(id(v))
        if w is None:
            w = TSet(v)
            w._tag = tag
            memo[id(v)] = w
        return w
    if t is frozenset:
        w = memo.get(id(v))
        if w is None:
            w = TFrozen(v)
            w._tag = tag
            memo[id(v)] = w
        return w
    return None


def import_everything():
    import sqlglot
    import sqlglot.dialects
    from sqlglot.dialects import Dialects

    for d in Dialects:
        if d.value:
            try:
                sqlglot.Dialect.get_or_raise(d.value)
            except Exception:  # noqa: BLE001
                pass
    for pkg in ("sqlglot",):
        mod = importlib.import_module(pkg)
        for info in pkgutil.walk_packages(mod.__path__, pkg + "."):
            if info.name.startswith("sqlglot.executor") or True:
                try:
                    importlib.import_module(info.name)
                except Exception:  # noqa: BLE001
                    pass


def install_tracing():
    memo = {}
    n = 0
    for name, mod in list(sys.modules.items()):
        if not name.startswith("sqlglot") or mod is None:
            continue
        # module level containers
        for k, v in list(vars(mod).items()):
            if k.startswith("__"):
                continue
            w = wrap_value(v, f"M:{name}:{k}", memo)
            if w is not None:
                setattr(mod, k, w)
                n += 1
        # classes defined in this module (and nested classes)
        stack = [v for v in vars(mod).values() if isinstance(v, type) and v.__module__ == name]
        seen = set()
        while stack:
            c = stack.pop()
            if id(c) in seen:
                continue
            seen.add(id(c))
            for k, v in list(c.__dict__.items()):
                if isinstance(v, type) and v.__module__ == name:
                    stack.append(v)
                    continue
                if k.startswith("__") and k.endswith("__"):
                    continue
                w = wrap_value(v, f"C:{c.__module__}.{c.__qualname__}:{k}", memo)
                if w is not None:
                    try:
                        setattr(c, k, w)
                        n += 1
                    except (TypeError, AttributeError):
                        pass
    # Expression instantiation
    from sqlglot.expressions import core

    orig = core.Expr.__init__

    def init(self, *a, **kw):
        if ON[0]:
            INST.add(f"{type(self).__module__}.{type(self).__qualname__}")
        orig(self, *a, **kw)

    core.Expr.__init__ = init
    return n


def main():
    out_dir, ids_file = sys.argv[1], sys.argv[2]
    wi, nw = (int(sys.argv[3]), int(sys.argv[4])) if len(sys.argv) > 4 else (0, 1)
    os.makedirs(out_dir, exist_ok=True)
    from selector.engine import _InlineExecutor, _TimedResult, preload

    preload()
    concurrent.futures.ProcessPoolExecutor = _InlineExecutor
    specs = [l.strip() for l in open(ids_file) if l.strip()]
    import_everything()
    mods = {}
    for spec in specs:
        f = spec.split("=", 1)[1].split("::")[0]
        if f not in mods:
            m = importlib.import_module(f[:-3].replace("/", "."))
            if hasattr(m, "ProcessPoolExecutor"):
                m.ProcessPoolExecutor = _InlineExecutor
            mods[f] = m
    nwrapped = install_tracing()
    print("wrapped containers", nwrapped, file=sys.stderr)
    for j, spec in enumerate(specs):
        if j % nw != wi:
            continue
        n, tid = spec.split("=", 1)
        out = os.path.join(out_dir, f"{n}.json")
        if os.path.exists(out):
            continue
        pid = os.fork()
        if pid == 0:
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1)
                f, cls, meth = tid.split("::")
                case = getattr(mods[f], cls)(meth)
                res = _TimedResult({tid: 600.0}, 1e18)
                ON[0] = True
                unittest.TestSuite([case]).run(res)
                ON[0] = False
                st = res.status.get(tid, "unknown")
                with open(out, "w") as fh:
                    json.dump({"id": tid, "status": st, "acc": sorted(ACC), "inst": sorted(INST)}, fh)
            finally:
                os._exit(0)
        os.waitpid(pid, 0)


if __name__ == "__main__":
    main()
