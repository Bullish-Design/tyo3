"""THROWAWAY SPIKE C2 — does a READ self-heal a reverse-dep (references) layer
after a new caller appears? Distinguishes 'invalidation misses but read heals'
from 'neither catches it' (stale-forever). Read-only review probe."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spike_gen  # noqa: E402
from run_spikes import CONFIG, LIB_SRC, APP_SRC  # noqa: E402
from tyo3 import TyO3Session  # noqa: E402


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tyo3-spikec2-"))
    root = tmp / "c2"
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "spike"\n')
    (root / "lib.py").write_text(LIB_SRC)
    (root / "app.py").write_text(APP_SRC)
    cfg = root / ".tyo3"
    cfg.mkdir()
    (cfg / "config.toml").write_text(CONFIG)

    s = TyO3Session(str(root))
    try:
        s.sync_all()
        tid = s.id_for("lib.py", 1, 5)

        # First read of the semantic 'refs' layer on the callee → primes the cache.
        spike_gen.REFCOUNT_CALLS.clear()
        s.derived("refs", tid)
        print(f"[read #1 of refs(callee)] recomputes = {len(spike_gen.REFCOUNT_CALLS)} (expect 1, cold)")

        # Add a NEW caller (callee's real reference set changes; its OWN body does not).
        s.edit("app.py", APP_SRC.replace("return target(5)", "return target(5) + target(6)"))
        tid2 = s.id_for("lib.py", 1, 5)
        print(f"callee id stable across caller edit? {tid2 == tid}")

        # Re-READ refs on the callee. With forward-dep 'semantic' locality the
        # callee's content_hash + forward-dep fingerprint are UNCHANGED (a new
        # caller is a *reverse* dep), so the key is identical → cache hit → NO
        # recompute → the references value is STALE-FOREVER.
        spike_gen.REFCOUNT_CALLS.clear()
        s.derived("refs", tid2)
        print(f"[read #2 of refs(callee) after new caller] recomputes = {len(spike_gen.REFCOUNT_CALLS)}")
        print("  → 0 means a forward-dep 'semantic' references layer is STALE-FOREVER")
        print("    (neither affected-invalidation nor forward-fingerprint catches a new caller)")
    finally:
        s.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
