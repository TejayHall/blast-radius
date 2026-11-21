"""Turn per-test trace records (from tools/trace_one.py) into data/coverage.json.gz.

Run offline:  python tools/build_maps.py <trace_dir> <tests.json> <e_costs.json> data/coverage.json.gz

Every map is keyed by the test *id* (not number), so the selector can remap to whatever numbering
/data/tests.json uses at run time.
"""
import collections
import glob
import gzip
import json
import os
import sys

ROOT = "/sqlglot/"


def rel(p):
    return p[len(ROOT):] if p.startswith(ROOT) else p


def main():
    trace_dir, tests_json, e_json, out = sys.argv[1:5]
    tests = json.load(open(tests_json))["tests"]
    num_to_id = {t["number"]: t["id"] for t in tests}
    ids = sorted(num_to_id.values())
    idx = {tid: i for i, tid in enumerate(ids)}
    recs = {}
    for f in glob.glob(os.path.join(trace_dir, "*.json")):
        n = int(os.path.basename(f).split(".")[0])
        recs[idx[num_to_id[n]]] = json.load(open(f))
    assert len(recs) == len(ids), (len(recs), len(ids))

    maps = {k: collections.defaultdict(set) for k in ("fn", "fnr", "top")}
    readers = collections.defaultdict(set)
    listed = collections.defaultdict(set)
    for i, r in recs.items():
        for key, phases in (("fn", ("lines_import", "lines_test", "lines_workers")),
                            ("fnr", ("lines_test", "lines_workers")),
                            ("top", ("top_import", "top_test", "top_workers"))):
            for ph in phases:
                for f, ls in r.get(ph, {}).items():
                    rf = rel(f)
                    for ln in ls:
                        maps[key][(rf, ln)].add(i)
        for p in r.get("opens", []):
            if "__pycache__" in p or p.endswith(".pyc"):
                continue
            readers[rel(p)].add(i)
        for p in r.get("listed", []):
            listed[rel(p).rstrip("/")].add(i)

    set_ids = {}
    sets = []

    def sid(s):
        k = tuple(sorted(s))
        if k not in set_ids:
            set_ids[k] = len(sets)
            sets.append(list(k))
        return set_ids[k]

    out_maps = {}
    for key, m in maps.items():
        per_file = collections.defaultdict(list)
        for (f, ln), s in m.items():
            per_file[f].append([ln, sid(s)])
        for f in per_file:
            per_file[f].sort()
        out_maps[key] = dict(per_file)
    e = json.load(open(e_json))
    e_cost = {}
    for n, rr in e["tests"].items():
        e_cost[idx[num_to_id[int(n)]]] = round(rr["cpu"], 4)
    doc = {
        "tests": ids,
        "sets": sets,
        "fn": out_maps["fn"],
        "fnr": out_maps["fnr"],
        "top": out_maps["top"],
        "readers": {f: sid(s) for f, s in readers.items()},
        "listed": {f: sid(s) for f, s in listed.items()},
        "e": [e_cost.get(i, 0.05) for i in range(len(ids))],
        "outcomes": {ids[i]: r["outcome"] for i, r in recs.items() if r["outcome"] != "passed"},
    }
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    print("tests", len(ids), "sets", len(sets), "files", len(out_maps["fn"]), "bytes", os.path.getsize(out))


if __name__ == "__main__":
    main()
