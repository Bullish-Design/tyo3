"""Project 31 §14.2 — measure what Arc<CodeLayer> sharing is worth.

Run from the repository root:
    devenv shell -- bash -c 'cd "$DEVENV_ROOT" && PYTHONPATH=src python \\
      .scratch/projects/31-semantic-state/probe_memory.py'

Compares process RSS for N snapshots at ONE revision (one shared layer) against
N snapshots at N revisions (N retained layers). RSS does not shrink on close —
the allocator retains — so read the deltas, not the absolutes.
"""

import os, time, pathlib, sys
from tyo3.session import TyO3Session

def rss():
    with open("/proc/self/statm") as f:
        return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")

root = "."
s = TyO3Session(root)
p = pathlib.Path("src/tyo3/__init__.py")
orig = p.read_text()
try:
    # one commit so the head layer exists
    s.edit(str(p), orig + "\n# p31 mem probe 0\n")
    s.snapshot().close()
    base = rss(); print(f"baseline RSS after 1 commit + 1 transient snapshot: {base/2**20:.1f} MiB")

    # A: 8 snapshots, all at the SAME revision -> one shared Arc<CodeLayer>
    same = [s.snapshot() for _ in range(8)]
    for sn in same: sn._inner.full_code_delta()   # force the layer to be exercised
    a = rss(); print(f"8 snapshots @ same revision : {a/2**20:.1f} MiB   (+{(a-base)/2**20:.1f} MiB, {(a-base)/8/2**20:.2f} MiB/snapshot)")
    for sn in same: sn.close()
    del same
    mid = rss(); print(f"after closing them          : {mid/2**20:.1f} MiB")

    # B: 8 snapshots, each at a DISTINCT revision -> 8 distinct retained layers
    distinct = []
    for i in range(8):
        s.edit(str(p), orig + f"\n# p31 mem probe {i+1}\n")
        sn = s.snapshot(); sn._inner.full_code_delta(); distinct.append(sn)
    b = rss(); print(f"8 snapshots @ 8 revisions   : {b/2**20:.1f} MiB   (+{(b-mid)/2**20:.1f} MiB, {(b-mid)/8/2**20:.2f} MiB/revision)")
    for sn in distinct: sn.close()
finally:
    p.write_text(orig)
    s.close()
