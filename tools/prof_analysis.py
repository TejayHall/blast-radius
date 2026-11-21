import cProfile, pstats, time, os, sys
sys.path.insert(0, "/app")
t0 = time.process_time()
from selector.index import Index
ix = Index()
t1 = time.process_time()
print("index load cpu", round(t1 - t0, 3))
from selector.analysis import Analyzer
an = Analyzer(ix, "/sqlglot")
pr = cProfile.Profile(); pr.enable()
for fn in sorted(os.listdir("/data/commits")):
    an.analyze(fn[:-6], open("/data/commits/" + fn).read())
pr.disable()
print("analysis cpu", round(time.process_time() - t1, 3))
pstats.Stats(pr).sort_stats("tottime").print_stats(14)
