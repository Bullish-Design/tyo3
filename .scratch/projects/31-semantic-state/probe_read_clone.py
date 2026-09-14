"""Project 31 §9.3 — is the read clone's deep IdentityRegistry clone measurable?

Run from the repository root:
    devenv shell -- bash -c 'cd "$DEVENV_ROOT" && PYTHONPATH=src python \\
      .scratch/projects/31-semantic-state/probe_read_clone.py . repo-root'

Times head_view.files(), which goes through clone_locked_state (project.rs:245)
and therefore pays one registry deep clone per call. Compare repository scale
against a small fixture.
"""

import time, sys
from tyo3.session import TyO3Session

def run(root, label):
    s = TyO3Session(root)
    lv = s.latest
    n_files = len(s.files())
    n_anchors = len(s.snapshot().code.ids()) if False else None
    lv.files()  # warm
    N = 300
    a = time.perf_counter()
    for _ in range(N):
        lv.files()
    per = (time.perf_counter()-a)/N*1000
    # count registry anchors via orphaned + locate is awkward; use code.ids()
    with s.snapshot() as sn:
        ids = len(list(sn.code.ids()))
    print(f"{label:28s} files={n_files:4d} entity_ids={ids:5d}  latest.files(): {per:.3f} ms/call")
    s.close()

run(sys.argv[1], sys.argv[2])
