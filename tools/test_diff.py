import os, sys, time
sys.path.insert(0, "/app")
from selector import diffimport as D
sys.path.insert(0, "/lab")
from brlib import parse_patch, apply_filediff
mode = sys.argv[1]
if mode == "build":
    t0 = time.process_time(); n, failed = D.build_snapshot("/sqlglot", "/tmp/state.marshal"); t1 = time.process_time()
    print("snapshot entries", n, "failed", failed, "cpu", round(t1 - t0, 3), "bytes", os.path.getsize("/tmp/state.marshal"))
else:
    D.STATE_PATH = "/tmp/state.marshal"
    for pf in sys.argv[2:]:
        text = open(pf).read()
        files = {}
        for fd in parse_patch(text):
            files[fd.path] = apply_filediff(open("/sqlglot/" + fd.path).read(), fd)
        t0 = time.monotonic()
        res, cpu = D.run_diff("/sqlglot", files)
        print(os.path.basename(pf), "cpu", round(cpu, 3), "wall", round(time.monotonic() - t0, 3), "changes", sorted(res["changes"])[:8] if res else None, "failed", list(res["failed"])[:5] if res else None)
