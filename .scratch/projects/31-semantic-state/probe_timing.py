"""Project 31 §6.3 — carried-layer vs fallback timing.

Run from the repository root:
    devenv shell -- bash -c 'cd "$DEVENV_ROOT" && PYTHONPATH=src python \\
      .scratch/projects/31-semantic-state/probe_timing.py .'

Edits one source file and restores it. Reports head/snapshot full_code_delta
with the initial layer materialized at open, after a commit (carried layer), and
for a time-travel snapshot (fallback by design).
"""

import time, sys
from tyo3.session import TyO3Session

root = sys.argv[1] if len(sys.argv) > 1 else "."
s = TyO3Session(root)
inner = s._inner

def t(fn, n=1):
    best = []
    for _ in range(n):
        a = time.perf_counter(); r = fn(); best.append(time.perf_counter()-a)
    return min(best), r

# 1. pre-commit head full_code_delta (initial layer was materialized at open)
d0, delta0 = t(inner.full_code_delta)
print(f"pre-commit  head full_code_delta : {d0:.3f}s  nodes={len(delta0['nodes_upserted'])} edges={len(delta0['edges_added'])}")
d0b, _ = t(inner.full_code_delta)
print(f"pre-commit  head full_code_delta again: {d0b:.3f}s   (carried layer)")

# 2. pre-commit snapshot
snap0 = s.snapshot()
d1, _ = t(snap0._inner.full_code_delta)
print(f"pre-commit  snapshot full_code_delta: {d1:.3f}s")
snap0.close()

# 3. commit one no-op-ish edit so the head layer is produced
files = [str(f) for f in s.files() if str(f).endswith(".py")]
target = files[0]
txt = open(target).read() if target.startswith("/") else None
import pathlib
p = pathlib.Path(root) / target if not target.startswith("/") else pathlib.Path(target)
orig = p.read_text()
a = time.perf_counter(); s.edit(str(p), orig + "\n# p31 probe\n"); print(f"commit: {time.perf_counter()-a:.3f}s")

# 4. post-commit head full_code_delta (carried layer -> diff_from)
d2, delta2 = t(inner.full_code_delta, 3)
print(f"post-commit head full_code_delta : {d2:.3f}s  nodes={len(delta2['nodes_upserted'])} edges={len(delta2['edges_added'])}")

# 5. post-commit head snapshot
snap = s.snapshot()
d3, _ = t(snap._inner.full_code_delta, 3)
print(f"post-commit head snapshot full_code_delta: {d3:.3f}s   rev={snap.revision}")
snap.close()

# 6. time-travel snapshot -> code_layer None -> full rebuild
head_rev = s.head
try:
    tt = s.snapshot(at=head_rev - 1)
    d4, _ = t(tt._inner.full_code_delta)
    print(f"time-travel snapshot(at={head_rev-1}) full_code_delta: {d4:.3f}s")
    tt.close()
except Exception as e:
    print("time-travel:", type(e).__name__, e)

# 7. cheap read-clone cost proxy: many head identity reads
n = 200
a = time.perf_counter()
for _ in range(n):
    inner.orphaned()
print(f"head orphaned() x{n}: {(time.perf_counter()-a)/n*1000:.3f} ms/call (no read_clone)")
a = time.perf_counter()
for _ in range(n):
    s.snapshot().close()
print(f"session.snapshot() x{n}: {(time.perf_counter()-a)/n*1000:.3f} ms/call (registry deep clone + db build)")

p.write_text(orig)
s.close()
