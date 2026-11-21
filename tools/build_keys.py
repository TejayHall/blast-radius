"""Turn tools/trace_keys.py output into data/keys.json.gz:
  acc:  {tag: {key: setid}}   tests that read key of container `tag` at run time ('*' = iterated, '#len' = size)
  inst: {class: setid}        tests that instantiated the Expression class
usage: python tools/build_keys.py <keys_dir> <tests.json> data/keys.json.gz
"""
import collections
import glob
import gzip
import json
import os
import sys


def main():
    kdir, tests_json, out = sys.argv[1:4]
    tests = json.load(open(tests_json))["tests"]
    num_to_id = {t["number"]: t["id"] for t in tests}
    ids = sorted(num_to_id.values())
    idx = {tid: i for i, tid in enumerate(ids)}
    acc = collections.defaultdict(lambda: collections.defaultdict(set))
    inst = collections.defaultdict(set)
    status = {}
    seen = set()
    for f in glob.glob(os.path.join(kdir, "*.json")):
        r = json.load(open(f))
        i = idx[r["id"]]
        seen.add(i)
        if r["status"] != "pass":
            status[r["id"]] = r["status"]
        for tag, key in r["acc"]:
            acc[tag][key].add(i)
        for c in r["inst"]:
            inst[c].add(i)
    missing = [ids[i] for i in range(len(ids)) if i not in seen]
    set_ids, sets = {}, []

    def sid(s):
        k = tuple(sorted(s))
        if k not in set_ids:
            set_ids[k] = len(sets)
            sets.append(list(k))
        return set_ids[k]

    doc = {
        "tests": ids,
        "sets": None,
        "acc": {tag: {k: sid(s) for k, s in d.items()} for tag, d in acc.items()},
        "inst": {c: sid(s) for c, s in inst.items()},
        "traced": sorted(seen),
        "status": status,
    }
    doc["sets"] = sets
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    print("tags", len(acc), "pairs", sum(len(d) for d in acc.values()), "inst", len(inst), "sets", len(sets),
          "non-pass", status, "missing", missing[:5], len(missing), "bytes", os.path.getsize(out))


if __name__ == "__main__":
    main()
