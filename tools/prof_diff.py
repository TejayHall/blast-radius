import cProfile, pstats, os, sys, time, marshal
sys.path.insert(0, "/app"); sys.path.insert(0, "/sqlglot"); os.chdir("/sqlglot")
import unittest, unittest.mock
from selector import fingerprint as F
F.import_all()
t0 = time.process_time()
pr = cProfile.Profile(); pr.enable()
s = F.snapshot()
pr.disable()
print("snapshot all cpu", round(time.process_time() - t0, 3))
pstats.Stats(pr).sort_stats("tottime").print_stats(10)
