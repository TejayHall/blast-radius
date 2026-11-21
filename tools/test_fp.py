import marshal, os, sys, time, json
sys.path.insert(0, "/app"); sys.path.insert(0, "/sqlglot"); os.chdir("/sqlglot")
from selector import fingerprint as F
mode = sys.argv[1]
if mode == "snap":
    t0 = time.process_time(); F.import_all(); t1 = time.process_time()
    s = F.snapshot(); t2 = time.process_time()
    data = marshal.dumps(s)
    open("/tmp/snap.marshal", "wb").write(data)
    print("import_all cpu", round(t1 - t0, 3), "snapshot cpu", round(t2 - t1, 3), "entries", len(s), "bytes", len(data))
    t3 = time.process_time(); s2 = marshal.loads(data); t4 = time.process_time(); print("load", round(t4 - t3, 3))
else:
    from selector.engine import _install_overlay
    sys.path.insert(0, "/lab")
    from brlib import parse_patch, apply_filediff
    for pf in sys.argv[2:]:
        pid = os.fork()
        if pid == 0:
            text = open(pf).read()
            files = {}
            for fd in parse_patch(text):
                files[fd.path] = apply_filediff(open("/sqlglot/" + fd.path).read(), fd)
            _install_overlay("/sqlglot", files, "/tmp/ov")
            t0 = time.process_time(); F.import_all(); t1 = time.process_time()
            s = F.snapshot(); t2 = time.process_time()
            old = marshal.loads(open("/tmp/snap.marshal", "rb").read()); t3 = time.process_time()
            d = F.diff(old, s); t4 = time.process_time()
            print(os.path.basename(pf), "import", round(t1 - t0, 3), "snap", round(t2 - t1, 3), "load", round(t3 - t2, 3), "diff", round(t4 - t3, 3), "changes", len(d), sorted(d)[:12])
            sys.stdout.flush(); os._exit(0)
        os.waitpid(pid, 0)
