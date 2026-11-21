"""Per-case coverage of fixture-driven tests (built by tools/trace_cases.py + tools/build_cases.py).

For a change confined to function bodies, a fixture-driven test can only fail through the cases whose run
executes a changed line. relevant() returns, per fixture file, the case indices to keep when verifying the
test locally; None means "all cases" (the change touches the test's base code or we know nothing).
"""
import gzip
import json
import marshal
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")
JSON_PATH = os.path.join(DATA, "cases.json.gz")
CACHE_PATH = os.path.join(DATA, "cases.marshal")


def build_cache():
    if not os.path.exists(JSON_PATH):
        return
    with gzip.open(JSON_PATH, "rt", encoding="utf-8") as fh:
        doc = json.load(fh)
    out = {}
    for tid, d in doc.items():
        base = {f: frozenset(ls) for f, ls in d["base"].items()}
        cases = {f: {int(ln): (v if v == "*" else tuple(v)) for ln, v in m.items()} for f, m in d["cases"].items()}
        out[tid] = (tuple(tuple(x) for x in d["fixtures"]), base, cases)
    with open(CACHE_PATH, "wb") as fh:
        marshal.dump(out, fh)


class Cases:
    def __init__(self):
        self.doc = {}
        try:
            if os.path.exists(CACHE_PATH):
                with open(CACHE_PATH, "rb") as fh:
                    self.doc = marshal.load(fh)
            elif os.path.exists(JSON_PATH):
                build_cache()
                with open(CACHE_PATH, "rb") as fh:
                    self.doc = marshal.load(fh)
        except Exception:  # noqa: BLE001
            self.doc = {}

    def has(self, tid):
        return tid in self.doc

    def fixtures(self, tid):
        d = self.doc.get(tid)
        return [fx for fx, _ in d[0]] if d else []

    def relevant(self, tid, spans):
        """spans: list of (file, a, b) changed line ranges (old coordinates, function bodies only).
        Returns (filters, n_relevant, n_total) with filters {fixture: set(idx)} or None for all cases."""
        d = self.doc.get(tid)
        if not d:
            return None, None, None
        fixtures, base, cases = d
        n_total = sum(n for _, n in fixtures)
        keep = set()
        for f, a, b in spans:
            bf = base.get(f, ())
            cf = cases.get(f, {})
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
