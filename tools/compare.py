"""Per-commit comparison of output.jsonl against answers: our cost vs oracle."""
import json, sys, re
sys.path.insert(0, ".")
from selector.pack import best_schedule
data, ans_path, out_path, err_path = sys.argv[1:5]
tests = json.load(open(data + "/tests.json"))["tests"]
dur = {t["number"]: round(t["seconds"] * 1000) for t in tests}
ans = json.load(open(ans_path))["commits"]
info = {}
for line in open(err_path):
    m = re.match(r"^(\S+): kinds=(\[.*?\]) cand=(\d+) final=(\d+) cost=([\d.]+)s( verified)?", line)
    if m:
        info[m.group(1)] = (m.group(2), int(m.group(3)), int(m.group(4)), m.group(6) is not None)
vcpu = {}
for line in open(err_path):
    m = re.match(r"^  verified (\S+): .* cpu ([\d.]+)s", line)
    if m:
        vcpu[m.group(1)] = float(m.group(2))
rows = []
for line in open(out_path):
    r = json.loads(line)
    cid = r["commit"]
    sel = {t for w in r["workers"] for t in w}
    F = set(ans[cid]["failing"])
    ours = (max([sum(dur[t] for t in w) for w in r["workers"]] or [0]) + 2000 * len([w for w in r["workers"] if w])) / 1000
    miss = bool(F - sel)
    orc = best_schedule(F, dur)[0] / 1000
    rows.append((ours + (300 if miss else 0) + vcpu.get(cid, 0) - orc, cid, ans[cid]["kind"], ours, orc, miss, info.get(cid), vcpu.get(cid, 0), len(F)))
rows.sort(reverse=True)
tot_o = sum(r[4] for r in rows); tot = sum(r[3] for r in rows)
print(f"ours simulated {tot:.1f}  oracle {tot_o:.1f}  verify cpu {sum(vcpu.values()):.1f}")
for r in rows[:int(sys.argv[5]) if len(sys.argv) > 5 else 25]:
    print(f"{r[1]} {r[2]:8s} excess {r[0]:7.2f} ours {r[3]:6.2f} oracle {r[4]:6.2f} |F|={r[8]:4d} vcpu {r[7]:5.2f} {'MISS' if r[5] else ''} {r[6]}")
