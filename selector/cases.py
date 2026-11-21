"""Per-case coverage of fixture-driven tests (built by tools/trace_cases.py + tools/build_cases.py).

For a change confined to function bodies, a fixture-driven test can only fail through the cases whose run
executes a changed line. relevant() returns, per fixture file, the case indices to keep when verifying the
test locally; None means "all cases" (the change touches the test's base code or we know nothing).

Storage: a small index (test -> fixtures and per-source-file blob offsets) plus one marshal blob per
(test, source file), so a lookup reads only the files a commit changed instead of the whole map.
"""
import gzip
import json
import marshal
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
JSON_PATH = os.path.join(DATA, "cases.json.gz")
INDEX_PATH = os.path.join(DATA, "cases_index.marshal")
BLOB_PATH = os.path.join(DATA, "cases_blobs.bin")


def build_cache():
    if not os.path.exists(JSON_PATH):
        return
    with gzip.open(JSON_PATH, "rt", encoding="utf-8") as fh:
        doc = json.load(fh)
    index = {}
    with open(BLOB_PATH, "wb") as out:
        pos = 0
        for tid, d in doc.items():
            per_file = {}
            files = set(d["base"]) | set(d["cases"])
            for f in sorted(files):
                base = frozenset(d["base"].get(f, ()))
                cases = {int(ln): (v if v == "*" else tuple(v)) for ln, v in d["cases"].get(f, {}).items()}
                blob = marshal.dumps((base, cases))
                out.write(blob)
                per_file[f] = (pos, len(blob))
                pos += len(blob)
            index[tid] = (tuple(tuple(x) for x in d["fixtures"]), per_file)
    with open(INDEX_PATH, "wb") as fh:
        marshal.dump(index, fh)


class Cases:
    def __init__(self):
        self.index = {}
        self._fh = None
        try:
            if not os.path.exists(INDEX_PATH) and os.path.exists(JSON_PATH):
                build_cache()
            with open(INDEX_PATH, "rb") as fh:
                self.index = marshal.load(fh)
            self._fh = open(BLOB_PATH, "rb")
        except Exception:  # noqa: BLE001
            self.index = {}

    def has(self, tid):
        return tid in self.index

    def fixtures(self, tid):
        d = self.index.get(tid)
        return [fx for fx, _ in d[0]] if d else []

    def _file(self, tid, f):
        loc = self.index[tid][1].get(f)
        if loc is None or self._fh is None:
            return frozenset(), {}
        self._fh.seek(loc[0])
        return marshal.loads(self._fh.read(loc[1]))

    def relevant(self, tid, spans):
        """spans: list of (file, a, b) changed line ranges (old coordinates, function bodies only).
        Returns (filters, n_relevant, n_total) with filters {fixture: set(idx)} or None for all cases."""
        d = self.index.get(tid)
        if not d:
            return None, None, None
        fixtures = d[0]
        n_total = sum(n for _, n in fixtures)
        keep = set()
        loaded = {}
        for f, a, b in spans:
            if f not in loaded:
                try:
                    loaded[f] = self._file(tid, f)
                except Exception:  # noqa: BLE001
                    return None, n_total, n_total
            bf, cf = loaded[f]
            for ln in range(a, b + 1):
                if ln in bf:
                    return None, n_total, n_total
                v = cf.get(ln)
                if v is None:
                    continue
                if v == "*":
                    return None, n_total, n_total
                keep.update(v)
        if not keep:
            # nothing recorded for these lines: be safe, run everything
            return None, n_total, n_total
        filters = {}
        k = 0
        for fx, n in fixtures:
            filters[fx] = {i for i in range(n) if (k + i) in keep}
            k += n
        return filters, len(keep), n_total
