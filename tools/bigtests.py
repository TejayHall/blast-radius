import json, sys, re, collections
sys.path.insert(0, ".")
data, ans_path, out_path = sys.argv[1:4]
tests = json.load(open(data + "/tests.json"))["tests"]
dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
name = {t["number"]: t["id"].split("::", 1)[1] for t in tests}
ans = json.load(open(ans_path))["commits"]
BIG = [n for n, d in dur.items() if d >= 900]
stat = collections.defaultdict(lambda: [0, 0, 0, 0])  # in C, failed, in C & no cheap fail, failed & no cheap fail
for line in open(out_path):
    r = json.loads(line)
    cid = r["commit"]
    C = {t for w in r["workers"] for t in w}
    F = set(ans[cid]["failing"])
    cheapC = {t for t in C if dur[t] < 900}
    cheap_fail = bool(F & cheapC)
    for b in BIG:
        if b in C:
            s = stat[b]
            s[0] += 1
            s[1] += b in F
            if not cheap_fail:
                s[2] += 1
                s[3] += b in F
for b in sorted(BIG, key=lambda x: -dur[x]):
    s = stat[b]
    print(f"{dur[b]/1000:7.3f}s {name[b][:60]:60s} inC {s[0]:3d} fail {s[1]:3d} | noCheapFail inC {s[2]:3d} fail {s[3]:3d}")
