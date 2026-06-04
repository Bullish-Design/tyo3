"""Probe: is snapshot isolation about lazy salsa file reads, not shared dbs?"""
import shutil
import tempfile
from pathlib import Path

from tyo3 import TyO3Session

SRC = Path("fixtures/simple_package")
SYM = "probe_added_symbol"
EDIT = f"\n\ndef {SYM}() -> int:\n    return 1\n"


def names(handle):
    return {s.name for s in handle.document_symbols("main.py")}


def scenario(label, materialize_before_edit, do_reload):
    d = Path(tempfile.mkdtemp())
    root = d / "project"
    shutil.copytree(SRC, root)
    main_py = root / "main.py"

    session = TyO3Session(str(root))
    snap = session.snapshot()
    try:
        if materialize_before_edit:
            _ = names(snap)  # force the snapshot to read main.py NOW

        main_py.write_text(main_py.read_text() + EDIT)  # edit on disk

        if do_reload:
            session.reload()

        leaked = SYM in names(snap)
        print(f"{label:55s} -> snapshot sees edit: {leaked}  (PINNED={not leaked})")
    finally:
        snap.close()
        session.close()
        shutil.rmtree(d, ignore_errors=True)


print("Expectation if hypothesis (lazy read) is correct:")
print("  materialize-before-edit  -> PINNED (cached old content)")
print("  no-materialize           -> NOT pinned (lazy reads new disk content)")
print("  reload should NOT change the answer (snap has its own db)\n")

scenario("A: no prior read, edit, NO reload", False, False)
scenario("B: no prior read, edit, reload", False, True)
scenario("C: read first, edit, NO reload", True, False)
scenario("D: read first, edit, reload", True, True)
