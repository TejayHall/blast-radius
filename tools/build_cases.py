"""Compress tools/trace_cases.py output into data/cases.json.gz.

Per traced test:
  fixtures: [[fixture, n_cases], ...]          case k enumerates (fixture, index) in this order
  base:     {file: [lines]}                    lines run with no case at all (+ setUpClass) -> any change
                                               there means every case is relevant
  cases:    {file: {line: "*" | [k, ...]}}     per line, the cases whose run executed it
Only tests whose every run passed on the pinned commit are kept.
usage: python tools/build_cases.py <cases_dir> data/cases.json.gz
"""
import glob
import gzip
import json
import os
import sys


def main():
    src, out = sys.argv[1], sys.argv[2]
    doc = {}
    for f in sorted(glob.glob(os.path.join(src, "*.json"))):
        d = json.load(open(f))
        runs = d["runs"]
        if d["full_status"] != "pass" or any(r["st"] != "pass" for r in runs.values()):
            print("skip (not all passing)", d["id"], file=sys.stderr)
            continue
        fixtures = sorted(d["counts"].items())
        if not fixtures or sum(n for _, n in fixtures) < 2:
            continue
        keys = [f"{fx}#{i}" for fx, n in fixtures for i in range(n)]
        if any(k not in runs for k in keys):
            print("skip (missing runs)", d["id"], file=sys.stderr)
            continue
        base = {}
        for part in (runs["base"]["lines"], d.get("setup", {})):
            for fl, ls in part.items():
                base.setdefault(fl, set()).update(ls)
        per = {}
        for k, key in enumerate(keys):
            for fl, ls in runs[key]["lines"].items():
                b = base.get(fl, ())
                m = per.setdefault(fl, {})
                for ln in ls:
                    if ln in b:
                        continue
                    m.setdefault(ln, []).append(k)
        n = len(keys)
        cases = {}
        for fl, m in per.items():
            cases[fl] = {str(ln): ("*" if len(ks) == n else ks) for ln, ks in m.items()}
        doc[d["id"]] = {"fixtures": fixtures, "base": {fl: sorted(s) for fl, s in base.items()}, "cases": cases}
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    print("tests", len(doc), "bytes", os.path.getsize(out))


if __name__ == "__main__":
    main()
