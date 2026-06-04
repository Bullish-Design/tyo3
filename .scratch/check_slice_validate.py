"""Validate the check-slice: correctness + proof the GIL is actually released."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from tyo3 import TyO3Session
from tyo3.exceptions import ProjectClosedError
from tyo3.session import Snapshot

FIXTURE = "fixtures/demo_repos"
N = 4


def banner(s: str) -> None:
    print(f"\n=== {s} ===")


# ── 1. Functional correctness ────────────────────────────────────────────────
banner("functional")
with TyO3Session(FIXTURE) as s:
    sess_result = s.check()
    snap = s.snapshot()
    assert isinstance(snap, Snapshot), type(snap)
    snap_result = snap.check()
    print(f"session.check() diagnostics: {len(sess_result.diagnostics)}")
    print(f"snapshot.check() diagnostics: {len(snap_result.diagnostics)}")
    assert len(sess_result.diagnostics) == len(snap_result.diagnostics), "snapshot != session"

    # lifecycle
    snap.close()
    snap.close()  # idempotent
    try:
        snap.check()
        raise SystemExit("FAIL: check after close did not raise")
    except ProjectClosedError:
        print("snapshot.check() after close raises ProjectClosedError  ✓")

    # terminal: no snapshot.snapshot()
    assert not hasattr(snap, "snapshot"), "snapshot should be terminal"
    print("snapshot is terminal (no .snapshot())  ✓")
print("functional: PASS")


# ── 2. GIL-release proof ──────────────────────────────────────────────────────
# Control: pure-Python CPU work holds the GIL -> threads do NOT speed up.
# Treatment: Rust check() releases the GIL -> threads DO speed up.

def py_busy() -> int:
    # ~comparable-duration pure-Python CPU work
    total = 0
    for i in range(3_000_000):
        total += i * i
    return total


def serial(fn, n):
    t0 = time.perf_counter()
    for _ in range(n):
        fn()
    return time.perf_counter() - t0


def parallel(fns):
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(fns)) as ex:
        list(ex.map(lambda f: f(), fns))
    return time.perf_counter() - t0


banner("control: pure-Python CPU (GIL held -> no speedup expected)")
py_busy()  # warm
c_serial = serial(py_busy, N)
c_parallel = parallel([py_busy] * N)
print(f"serial  {c_serial*1000:7.1f} ms")
print(f"parallel{c_parallel*1000:7.1f} ms   speedup x{c_serial/c_parallel:.2f}")

banner("treatment: Rust check() on N cold sessions (GIL released -> speedup expected)")
# Pre-open distinct cold sessions (open() is not what we measure).
serial_sessions = [TyO3Session(FIXTURE) for _ in range(N)]
parallel_sessions = [TyO3Session(FIXTURE) for _ in range(N)]

t0 = time.perf_counter()
for s in serial_sessions:
    s.check()  # cold first-check per session
t_serial = time.perf_counter() - t0

t0 = time.perf_counter()
with ThreadPoolExecutor(max_workers=N) as ex:
    list(ex.map(lambda s: s.check(), parallel_sessions))
t_parallel = time.perf_counter() - t0

print(f"serial  {t_serial*1000:7.1f} ms")
print(f"parallel{t_parallel*1000:7.1f} ms   speedup x{t_serial/t_parallel:.2f}")

for s in serial_sessions + parallel_sessions:
    s.close()

print("\n=== VERDICT ===")
print(f"Python CPU (GIL held)  speedup: x{c_serial/c_parallel:.2f}   (expect ~1.0)")
print(f"Rust check (GIL freed) speedup: x{t_serial/t_parallel:.2f}   (expect >1.0)")
