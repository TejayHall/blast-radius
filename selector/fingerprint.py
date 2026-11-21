"""Stable fingerprints of sqlglot's import-time state (module globals and class attributes).

Used for *differential import*: the image build stores a snapshot of the pinned tree's state; at run time
a child imports the patched tree and diffs. The diff names exactly which (attribute, key) entries an
import-time change altered - including state derived by metaclasses, __init_subclass__ hooks and dicts
merged into subclasses. Fingerprints ignore line numbers (moved code is not a change) and are memoised
by object identity, composed bottom-up as 8-byte blake2b digests.
"""
import enum
import functools
import hashlib
import importlib
import logging
import pkgutil
import re
import sys
import types

_LIT = (bool, int, float, str, bytes, complex, type(None))
_B2 = hashlib.blake2b


def _d(*parts):
    return _B2(b"".join(p if isinstance(p, bytes) else str(p).encode("utf-8", "surrogatepass") for p in parts),
               digest_size=8).digest()


def _lit(t, v):
    # literals are encoded directly (no hashing): cheap and unambiguous
    r = repr(v).encode("utf-8", "surrogatepass")
    return b"L" + t.__name__.encode() + b":" + r if len(r) < 48 else _d("L", t.__name__, r)


class Fingerprinter:
    def __init__(self, ordered=True):
        self.memo = {}
        self.code_memo = {}
        self.ordered = ordered

    def code(self, co):
        r = self.code_memo.get(co)
        if r is not None:
            return r
        consts = []
        for c in co.co_consts:
            if isinstance(c, types.CodeType):
                consts.append(self.code(c))
            else:
                consts.append(self.v(c))
        r = _d("C", co.co_qualname, co.co_code, repr(co.co_names), repr(co.co_varnames), repr(co.co_freevars),
               repr(co.co_cellvars), b"".join(consts), co.co_flags, co.co_argcount, co.co_kwonlyargcount,
               co.co_posonlyargcount, co.co_exceptiontable)
        self.code_memo[co] = r
        return r

    def v(self, v, depth=0):
        t = type(v)
        if t in _LIT:
            return _lit(t, v)
        key = id(v)
        r = self.memo.get(key)
        if r is not None:
            return r
        if depth > 8:
            return _d("deep", t.__qualname__)
        self.memo[key] = _d("cycle", t.__qualname__)  # provisional (cycles)
        r = self._v(v, t, depth)
        self.memo[key] = r
        return r

    def _v(self, v, t, depth):
        if isinstance(v, enum.Enum):
            return _d("E", type(v).__module__, type(v).__qualname__, v.name)
        if isinstance(v, type):
            return _d("T", v.__module__, v.__qualname__)
        if t is types.FunctionType:
            clo = b""
            if v.__closure__:
                parts = []
                for c in v.__closure__:
                    try:
                        cv = c.cell_contents
                    except ValueError:
                        parts.append(b"<empty>")
                        continue
                    parts.append(self.v(cv, depth + 1))
                clo = b"".join(parts)
            return _d("F", v.__module__, v.__qualname__, self.code(v.__code__), self.v(v.__defaults__, depth + 1),
                      self.v(v.__kwdefaults__, depth + 1), clo)
        if t is types.ModuleType:
            return _d("Mod", v.__name__)
        if t is types.MethodType:
            s = v.__self__
            return _d("Mth", self.v(v.__func__, depth + 1), self.v(s, depth + 1) if isinstance(s, type) else type(s).__qualname__)
        if t is staticmethod or t is classmethod:
            return _d(t.__name__, self.v(v.__func__, depth + 1))
        if t is property:
            return _d("P", self.v(v.fget, depth + 1), self.v(v.fset, depth + 1), self.v(v.fdel, depth + 1))
        if isinstance(v, functools.partial):
            return _d("Pa", self.v(v.func, depth + 1), self.v(v.args, depth + 1), self.v(v.keywords, depth + 1))
        if isinstance(v, (types.BuiltinFunctionType, types.MethodDescriptorType, types.WrapperDescriptorType,
                          types.MethodWrapperType, types.ClassMethodDescriptorType, types.GetSetDescriptorType,
                          types.MemberDescriptorType)):
            return _d("B", getattr(v, "__qualname__", None) or repr(type(v)), getattr(v, "__module__", None) or "")
        if t is tuple or t is list:
            parts = [self.v(x, depth + 1) for x in v]
            if not self.ordered or (parts and all(isinstance(x, type) for x in v)):
                # tuples of classes (isinstance targets) are often built from sets: order is id-based
                parts.sort()
            return _d(t.__name__, b"".join(parts))
        if t is set or t is frozenset:
            return _d(t.__name__, b"".join(sorted(self.v(x, depth + 1) for x in v)))
        if t is dict:
            return _d("D", b"".join(sorted(self.v(k, depth + 1) + self.v(x, depth + 1) for k, x in v.items())))
        if isinstance(v, re.Pattern):
            return _d("R", v.pattern, v.flags)
        if isinstance(v, logging.Logger):
            return _d("logger", v.name)
        if isinstance(v, functools._lru_cache_wrapper):
            return _d("LRU", self.v(v.__wrapped__, depth + 1))
        if isinstance(v, (dict, list, tuple, set, frozenset)):  # subclasses
            return _d("sub", t.__module__, t.__qualname__, self._v(v, type.__mro__ and next(b for b in t.__mro__ if b in (dict, list, tuple, set, frozenset)), depth))
        args = getattr(v, "args", None)
        if isinstance(args, dict) and hasattr(v, "arg_types"):  # sqlglot Expression instance
            return _d("X", t.__module__, t.__qualname__, self.v(args, depth + 1))
        dd = getattr(v, "__dict__", None)
        if isinstance(dd, dict):
            return _d("O", t.__module__, t.__qualname__,
                      b"".join(sorted(_d(k) + self.v(x, depth + 1) for k, x in dd.items() if not k.startswith("__"))))
        sl = getattr(t, "__slots__", None)
        if sl:
            if isinstance(sl, str):
                sl = (sl,)
            return _d("S", t.__module__, t.__qualname__, b"".join(_d(k) + self.v(getattr(v, k, None), depth + 1) for k in sl))
        return _d("O", t.__module__, t.__qualname__)


def key_fp(k):
    """Same key encoding the key-access tracer uses (tools/trace_keys.py::kfp)."""
    if k is None or isinstance(k, (bool, int, float, str, bytes)):
        return repr(k)
    if isinstance(k, enum.Enum):
        return f"E:{type(k).__qualname__}.{k.name}"
    if isinstance(k, type):
        return f"T:{k.__module__}.{k.__qualname__}"
    if isinstance(k, tuple):
        return "(" + ",".join(key_fp(x) for x in k) + ")"
    if isinstance(k, frozenset):
        return "{" + ",".join(sorted(key_fp(x) for x in k)) + "}"
    return f"?:{type(k).__qualname__}"


def _noncode(fpr, f):
    """Digest of a function object without its code: defaults, kw-defaults and closure cells."""
    clo = b""
    if f.__closure__:
        parts = []
        for c in f.__closure__:
            try:
                parts.append(fpr.v(c.cell_contents))
            except ValueError:
                parts.append(b"<empty>")
        clo = b"".join(parts)
    return _d("FN", f.__module__, f.__qualname__, fpr.v(f.__defaults__), fpr.v(f.__kwdefaults__), clo)


def attr_entry(fpr, v):
    """('d', {keyfp: digest}) for dicts, ('s', set(keyfp)) for sets, ('v', digest, typename[, noncode])."""
    t = type(v)
    if t is dict:
        return ("d", {key_fp(k): fpr.v(x) for k, x in v.items()})
    if t is set or t is frozenset:
        return ("s", {key_fp(x) for x in v})
    f = v
    if t in (staticmethod, classmethod):
        f = v.__func__
    if type(f) is types.FunctionType:
        return ("v", fpr.v(v), t.__name__, _noncode(fpr, f))
    return ("v", fpr.v(v), t.__name__)


def import_all(root_pkg="sqlglot"):
    """Import sqlglot, every dialect and every submodule. Returns {module: error repr} for failures."""
    failed = {}
    try:
        import sqlglot  # noqa: F401
    except BaseException as e:  # noqa: BLE001
        failed[root_pkg] = repr(e)
        return failed
    try:
        from sqlglot.dialects import Dialects

        for d in Dialects:
            if d.value:
                try:
                    sqlglot.Dialect.get_or_raise(d.value)
                except BaseException as e:  # noqa: BLE001
                    failed[f"dialect:{d.value}"] = repr(e)
    except BaseException as e:  # noqa: BLE001
        failed["sqlglot.dialects"] = repr(e)
    mod = sys.modules.get(root_pkg)
    for info in pkgutil.walk_packages(mod.__path__, root_pkg + "."):
        if info.name in sys.modules:
            continue
        try:
            importlib.import_module(info.name)
        except BaseException as e:  # noqa: BLE001
            failed[info.name] = repr(e)
    return failed


def _entries(mod, name, fpr, out):
    for k, v in list(vars(mod).items()):
        if k.startswith("__") and k.endswith("__"):
            continue
        if isinstance(v, types.ModuleType):
            continue
        if isinstance(v, (type, types.FunctionType)) and getattr(v, "__module__", name) != name:
            out[f"M:{name}:{k}"] = ("v", _d("ref", getattr(v, "__module__", ""), getattr(v, "__qualname__", "")), "ref")
            continue
        try:
            out[f"M:{name}:{k}"] = attr_entry(fpr, v)
        except Exception:  # noqa: BLE001
            out[f"M:{name}:{k}"] = ("v", b"?", "?")
    stack = [v for v in vars(mod).values() if isinstance(v, type) and v.__module__ == name]
    seen = set()
    while stack:
        c = stack.pop()
        if id(c) in seen:
            continue
        seen.add(id(c))
        out[f"K:{c.__module__}.{c.__qualname__}"] = ("v", _d("bases", *[(b.__module__, b.__qualname__) for b in c.__mro__]), "class")
        for k, v in list(c.__dict__.items()):
            if isinstance(v, type) and v.__module__ == name and v.__qualname__.startswith(c.__qualname__ + "."):
                stack.append(v)
                continue
            if k in ("__dict__", "__weakref__", "__doc__", "__module__", "__qualname__", "__firstlineno__",
                     "__static_attributes__", "__annotations__", "__annotate__"):
                continue
            try:
                out[f"C:{c.__module__}.{c.__qualname__}:{k}"] = attr_entry(fpr, v)
            except Exception:  # noqa: BLE001
                out[f"C:{c.__module__}.{c.__qualname__}:{k}"] = ("v", b"?", "?")


def snapshot(prefix="sqlglot", only=None, noisy=None):
    """{module: {tag: entry}} for module globals ('M:mod:name') and class attributes ('C:mod.Qual:name').
    Tags in `noisy` are fingerprinted order-insensitively (their element order is not reproducible)."""
    fpr = Fingerprinter()
    fpu = Fingerprinter(ordered=False) if noisy else None
    snap = {}
    for name, mod in list(sys.modules.items()):
        if mod is None or not (name == prefix or name.startswith(prefix + ".")):
            continue
        if only is not None and name not in only:
            continue
        out = {}
        _entries(mod, name, fpr, out)
        if noisy:
            for tag in list(out):
                if tag in noisy:
                    v = _resolve(tag)
                    if v is not _MISSING:
                        out[tag] = attr_entry(fpu, v)
        snap[name] = out
    return snap


_MISSING = object()


def _resolve(tag):
    kind, rest = tag.split(":", 1)
    try:
        if kind == "M":
            modname, attr = rest.rsplit(":", 1)
            return getattr(sys.modules[modname], attr)
        if kind == "C":
            qual, attr = rest.rsplit(":", 1)
            parts = qual.split(".")
            for i in range(len(parts), 0, -1):
                m = sys.modules.get(".".join(parts[:i]))
                if m is not None:
                    obj = m
                    for part in parts[i:]:
                        obj = getattr(obj, part)
                    return obj.__dict__[attr]
    except Exception:  # noqa: BLE001
        pass
    return _MISSING


def module_deps(prefix="sqlglot"):
    """{module: [modules whose objects it references at module level or inherits from]}."""
    deps = {}
    for name, mod in list(sys.modules.items()):
        if mod is None or not (name == prefix or name.startswith(prefix + ".")):
            continue
        d = set()
        for v in list(vars(mod).values()):
            if isinstance(v, types.ModuleType):
                d.add(v.__name__)
                continue
            m = getattr(v, "__module__", None)
            if isinstance(m, str):
                d.add(m)
            if isinstance(v, type):
                for b in v.__mro__:
                    d.add(b.__module__)
                for vv in list(v.__dict__.values()):
                    if isinstance(vv, type):
                        for b in vv.__mro__:
                            d.add(b.__module__)
        d.discard(name)
        deps[name] = sorted(x for x in d if x == prefix or x.startswith(prefix + "."))
    return deps


def diff(old, new):
    """List of (tag, key, typename). key '*' = whole attribute, '#len' = key set changed."""
    out = []
    for tag in set(old) | set(new):
        a = old.get(tag)
        b = new.get(tag)
        if a == b:
            continue
        if a is None or b is None or a[0] != b[0] or a[0] == "v":
            tn = (a or b)[2] if (a or b)[0] == "v" else (a or b)[0]
            if (a is not None and b is not None and a[0] == "v" and len(a) > 3 and len(b) > 3
                    and a[3] == b[3]):
                tn = "function_code"  # only the code differs: a body edit, already covered by line coverage
            out.append((tag, "*", tn))
            continue
        if a[0] == "d":
            da, db = a[1], b[1]
            if set(da) != set(db):
                out.append((tag, "#len", "dict"))
                if bool(da) != bool(db):
                    out.append((tag, "#bool", "dict"))
            for k in set(da) | set(db):
                if da.get(k) != db.get(k):
                    out.append((tag, k, "dict"))
        else:
            sa, sb = a[1], b[1]
            if len(sa) != len(sb):
                out.append((tag, "#len", "set"))
                if bool(sa) != bool(sb):
                    out.append((tag, "#bool", "set"))
            for k in sa ^ sb:
                out.append((tag, k, "set"))
    return out
