import os, sys, time, json, resource, gc
sys.path.insert(0, "/app"); sys.path.insert(0, "/lab")
from selector import engine
from brlib import parse_patch, apply_filediff
os.environ["SKIP_INTEGRATION"] = "0"
patch = open(sys.argv[1]).read()
files = {}
for fd in parse_patch(patch):
    files[fd.path] = apply_filediff(open("/sqlglot/" + fd.path).read(), fd)
ids = sys.argv[2:]
if os.environ.get("PRELOAD"):
    engine.preload()
if os.environ.get("FREEZE"):
    gc.freeze()
engine._install_overlay("/sqlglot", files, "/tmp/ov")
sys.path.insert(0, "/sqlglot"); os.chdir("/sqlglot")
import sqlglot
import concurrent.futures
for tid in ids:
    t0 = resource.getrusage(resource.RUSAGE_SELF); c0 = resource.getrusage(resource.RUSAGE_CHILDREN); w0 = time.monotonic()
    res = engine._run_tests([tid], 60.0, time.monotonic() + 600)
    t1 = resource.getrusage(resource.RUSAGE_SELF); c1 = resource.getrusage(resource.RUSAGE_CHILDREN)
    print(f"{(t1.ru_utime + t1.ru_stime) - (t0.ru_utime + t0.ru_stime):7.3f}s +children {(c1.ru_utime + c1.ru_stime) - (c0.ru_utime + c0.ru_stime):7.3f}s wall {time.monotonic()-w0:6.2f} {res[tid]:7s} {tid[-60:]}", flush=True)
