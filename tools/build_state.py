"""Image build step: snapshot the pinned tree's import-time state twice (separate processes) to find
order-unstable entries, then write data/state.marshal with those entries compared as multisets."""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)

if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else "/sqlglot"
    out = os.path.join(APP, "data", "state.marshal")
    code = "import sys; sys.path.insert(0, %r); from selector import diffimport as D; D.build_snapshot(%r, sys.argv[1])" % (APP, root)
    a, b = "/tmp/state_a.marshal", "/tmp/state_b.marshal"
    env = dict(os.environ, PYTHONHASHSEED="0")
    for p in (a, b):
        subprocess.run([sys.executable, "-c", code, p], check=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    from selector import diffimport as D
    noisy = D.noisy_tags(a, b)
    print("noisy tags:", sorted(noisy))
    subprocess.run([sys.executable, "-c", code.replace("sys.argv[1])", "sys.argv[1], noisy=set(sys.argv[2:]))"), out, *sorted(noisy)],
                   check=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("state written", os.path.getsize(out))
