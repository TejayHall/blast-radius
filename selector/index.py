"""Coverage index: which tests execute which (file, line), which tests read which files.

Built offline by tools/build_maps.py from isolated per-test traces of the pinned sqlglot commit.
At image build, build_cache() writes data/coverage.marshal: per-file line maps stored as separate
marshal blobs and test sets as one packed uint16 array, so the selector only decodes what a batch
of patches actually touches.
"""
import array
import gzip
import json
import marshal
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
JSON_PATH = os.path.join(DATA, "coverage.json.gz")
CACHE_PATH = os.path.join(DATA, "coverage.marshal")


def build_cache():
    with gzip.open(JSON_PATH, "rt", encoding="utf-8") as fh:
        doc = json.load(fh)
    blob = array.array("H")
    offs = array.array("I", [0])
    for s in doc["sets"]:
        blob.extend(s)
        offs.append(len(blob))
    files = {}
    for f in set(doc["fn"]) | set(doc["fnr"]) | set(doc["top"]):
        files[f] = marshal.dumps(tuple({ln: s for ln, s in doc[k].get(f, [])} for k in ("fn", "fnr", "top")))
    out = {
        "tests": doc["tests"],
        "sets_blob": blob.tobytes(),
        "sets_off": offs.tobytes(),
        "files": files,
        "readers": doc["readers"],
        "listed": doc["listed"],
        "e": doc["e"],
    }
    with open(CACHE_PATH, "wb") as fh:
        marshal.dump(out, fh)


class Index:
    def __init__(self):
        if not os.path.exists(CACHE_PATH):
            build_cache()
        with open(CACHE_PATH, "rb") as fh:
            d = marshal.load(fh)
        self.ids = d["tests"]
        self._blob = array.array("H")
        self._blob.frombytes(d["sets_blob"])
        self._off = array.array("I")
        self._off.frombytes(d["sets_off"])
        self._files = d["files"]
        self._decoded = {}
        self.readers_map = d["readers"]
        self.listed_map = d["listed"]
        self.e = d["e"]
        self._pos = None
        self._setcache = {}
        self.fni = {}
        try:
            with open(os.path.join(DATA, "import_fn.json"), encoding="utf-8") as fh:
                self.fni = {f: set(ls) for f, ls in json.load(fh).items()}
        except OSError:
            pass
        # import-time lines that test files run while *they* are imported (module-level subclasses of sqlglot
        # classes, `from sqlglot.dialects import X` through the lazy loader): {file: {line: [test files]}}
        self.tfi = {}
        try:
            with open(os.path.join(DATA, "test_import_fn.json"), encoding="utf-8") as fh:
                self.tfi = json.load(fh)
        except OSError:
            pass

    @property
    def pos(self):
        if self._pos is None:
            self._pos = {tid: i for i, tid in enumerate(self.ids)}
        return self._pos

    def _set(self, sid):
        s = self._setcache.get(sid)
        if s is None:
            s = self._blob[self._off[sid]:self._off[sid + 1]]
            self._setcache[sid] = s
        return s

    def _maps(self, path):
        m = self._decoded.get(path)
        if m is None:
            b = self._files.get(path)
            m = marshal.loads(b) if b is not None else ({}, {}, {})
            self._decoded[path] = m
        return m

    def runs_at_import(self, path, a, b):
        s = self.fni.get(path)
        return bool(s) and any(a <= ln <= b for ln in s)

    def test_import_tests(self, path, a, b):
        """Tests of every test file whose import runs a line of path[a..b] (tests/helpers.py: all tests)."""
        m = self.tfi.get(path)
        if not m:
            return set()
        tfiles = set()
        for ln, tfs in m.items():
            if a <= int(ln) <= b:
                tfiles.update(tfs)
        if not tfiles:
            return set()
        if "tests/helpers.py" in tfiles or "tests/__init__.py" in tfiles:
            return self.all_tests()
        return {i for i, tid in enumerate(self.ids) if tid.split("::", 1)[0] in tfiles}

    def _lines(self, which, path, a, b):
        fm = self._maps(path)[which]
        out = set()
        if not fm:
            return out
        for ln in range(a, b + 1):
            s = fm.get(ln)
            if s is not None:
                out.update(self._set(s))
        return out

    def fn_tests(self, path, a, b):
        return self._lines(0, path, a, b)

    def fnr_tests(self, path, a, b):
        return self._lines(1, path, a, b)

    def top_tests(self, path, a, b):
        return self._lines(2, path, a, b)

    def has_fnr(self, path):
        return path in self._files

    def file_any(self, path):
        """Tests that executed any line of path (fn or top) - i.e. imported/used the module."""
        out = set()
        fn, _, top = self._maps(path)
        for m in (fn, top):
            for s in set(m.values()):
                out.update(self._set(s))
        return out

    def readers(self, path):
        s = self.readers_map.get(path)
        if s is None or path.endswith((".py", ".pyc")):
            return set()
        return set(self._set(s))

    def listers(self, dirpath):
        s = self.listed_map.get(dirpath.rstrip("/"))
        return set(self._set(s)) if s is not None else set()

    def all_tests(self):
        return set(range(len(self.ids)))
