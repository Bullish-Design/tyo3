"""THROWAWAY SPIKES for project 21 (spine-extensibility review).

Read-only review probes. Does NOT modify engine/plugin code — it only *uses*
the public engine + daemon surface against throwaway projects in a temp dir.
Run: `devenv shell -- python .scratch/projects/21-.../spikes/run_spikes.py`

Spikes:
  A  custom authored + derived layers via config alone (what core files break?)
  B  does entity_at's card auto-include arbitrary new layers? (real daemon path)
  C  reverse-dependency invalidation: is a callee's layer invalidated when a new
     caller appears? (the AB2 'references-as-a-layer' correctness wall)
  D  identity matrix: edit / body-change / rename / atomic-move — id + note fate,
     and whether 'rename via atomic edit_many' can preserve the id (QW2 feasibility)
  E  does find_references return callers? (is a references-layer content-possible)
  F  what derived status does the card emit? (the panel render-bug claim)
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # make spike_gen importable

from tyo3 import TyO3Session  # noqa: E402

RESULTS: list[str] = []


def log(s: str = "") -> None:
    print(s)
    RESULTS.append(s)


def banner(s: str) -> None:
    log("\n" + "=" * 78)
    log(s)
    log("=" * 78)


CONFIG = """\
schema_version = 1

[hashing.profiles.structure]

[layers.tests]
origin = "authored"
history = true
review_on_change = true

[layers.complexity]
origin = "derived"
depends_on = ["code"]
generator = "cc_gen"
generator_version = "v1"
hash_profile = "structure"
store = "kv_cc"
serving = "stale"
key_locality = "local"
entity_kinds = ["function"]

[layers.refs]
origin = "derived"
depends_on = ["code"]
generator = "ref_gen"
generator_version = "v1"
hash_profile = "structure"
store = "kv_refs"
serving = "stale"
key_locality = "semantic"
entity_kinds = ["function"]

[generators.cc_gen]
type = "python"
callable = "spike_gen:complexity"

[generators.ref_gen]
type = "python"
callable = "spike_gen:refcount"

[stores.kv_cc]
backend = "fs"
path = "cache/cc"

[stores.kv_refs]
backend = "fs"
path = "cache/refs"
"""

LIB_SRC = """\
def target(x: int) -> int:
    if x > 0:
        return x + 1
    return 0
"""

APP_SRC = """\
from lib import target


def caller() -> int:
    return target(5)
"""


def make_project(root: Path, *, lib=LIB_SRC, app=APP_SRC) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text('[project]\nname = "spike"\n')
    (root / "lib.py").write_text(lib)
    (root / "app.py").write_text(app)
    (root / "moved.py").write_text("")  # known path for atomic-move tests
    cfg = root / ".tyo3"
    cfg.mkdir()
    (cfg / "config.toml").write_text(CONFIG)


def id_of(s, path, name_line, col=5):
    return s.id_for(path, name_line, col)


# ── Spike A + B + E + F (single project) ────────────────────────────────────


def spike_abef(tmp: Path) -> None:
    root = tmp / "abef"
    make_project(root)
    s = TyO3Session(str(root))
    try:
        s.sync_all()
        banner("SPIKE A — custom layers via config alone")
        log(f"declared layers (from validated native config): {sorted(s.config.layers)}")
        for name in ("tests", "complexity", "refs"):
            lc = s.config.layers.get(name)
            log(f"  {name}: origin={lc.origin} kinds={lc.entity_kinds} key_locality={lc.key_locality}")

        tid = id_of(s, "lib.py", 1)  # def target
        log(f"target durable id: {tid}")

        # Author a NEW authored layer 'tests' that ships in no engine code.
        s.author("tests", tid, {"paths": ["tests/test_lib.py::test_target"], "n": 2})
        av = s.authored("tests", tid)
        log(f"authored read-back: status={av.status} value={av.value}")

        # Derive the NEW derived layer 'complexity'.
        dv = s.derived("complexity", tid)
        log(f"derived complexity: status={dv.status} artifact={dv.artifact!r}")

        banner("SPIKE F — derived status the card emits (panel render-bug check)")
        log(f"derived status literal = {dv.status!r}  (panel/card.lua gate on == 'present')")

        banner("SPIKE B — does the REAL daemon card auto-include the new layers?")
        from tyo3.daemon.session_actor import SessionActor
        from tyo3.daemon.handlers import Handlers

        actor = SessionActor(str(root))
        actor.start()
        try:
            h = Handlers(actor)
            card = h.entity_at({"path": "lib.py", "line": 1, "col": 5})
            log(f"card.durable_id = {card.get('durable_id')}")
            log(f"card.authored keys = {sorted(card.get('authored', {}))}")
            log(f"card.derived  keys = {sorted(card.get('derived', {}))}")
            log(f"card.authored['tests'] = {card.get('authored', {}).get('tests')}")
            log(f"card.derived['complexity'] = {card.get('derived', {}).get('complexity')}")
            log(f"open() layers list = {h.open({}).get('layers')}")
        finally:
            actor.stop()

        banner("SPIKE E — does find_references return the caller? (references-layer possible?)")
        refs = s.find_references("lib.py", 1, 5)  # on 'target'
        log(f"find_references(target) -> {len(refs)} refs:")
        for r in refs:
            log(f"  {r.path}  {r.range.start.line}:{r.range.start.column}")

        banner("SPIKE A (negative) — is there ANY programmatic registration API?")
        import tyo3
        log(f"hasattr(tyo3, 'register_layer') = {hasattr(tyo3, 'register_layer')}")
        log(f"hasattr(tyo3, 'register_generator') = {hasattr(tyo3, 'register_generator')}")
        try:
            import tyo3.extend  # noqa: F401
            log("import tyo3.extend = OK")
        except Exception as e:
            log(f"import tyo3.extend -> {type(e).__name__}: {e}")
    finally:
        s.close()


# ── Spike C — reverse-dependency invalidation correctness wall ──────────────


def spike_c(tmp: Path) -> None:
    banner("SPIKE C — reverse-dep invalidation (the 'references-as-a-layer' wall)")
    root = tmp / "c"
    make_project(root)
    import spike_gen

    s = TyO3Session(str(root))
    try:
        s.sync_all()
        tid = id_of(s, "lib.py", 1)  # target (callee)
        cid = id_of(s, "app.py", 4)  # caller
        log(f"callee target id = {tid}")
        log(f"caller caller id = {cid}")

        # Prime: derive 'refs' (semantic) on the callee so it has a binding.
        spike_gen.REFCOUNT_CALLS.clear()
        s.derived("refs", tid)
        log(f"primed refs on callee; refcount calls so far = {len(spike_gen.REFCOUNT_CALLS)}")

        # 1) Edit the CALLEE body — expect callee changed, caller in affected.
        spike_gen.REFCOUNT_CALLS.clear()
        new_lib = LIB_SRC.replace("return x + 1", "return x + 2")
        d1 = s.edit("lib.py", new_lib)
        log("\n[edit callee body] lib.py")
        log(f"  changed_ids  = {d1.changed_ids}")
        log(f"  affected_ids = {d1.affected_ids}")
        log(f"  callee in affected? {tid in set(d1.affected_ids)}")
        log(f"  caller in affected? {cid in set(d1.affected_ids)}  (caller depends on callee)")

        # 2) Add a NEW caller (edit app.py to call target twice) — the callee's
        #    'references' value would change. Is the CALLEE invalidated?
        spike_gen.REFCOUNT_CALLS.clear()
        new_app = APP_SRC.replace("return target(5)", "return target(5) + target(6)")
        d2 = s.edit("app.py", new_app)
        # ids may have changed across revisions; re-resolve by name.
        tid2 = id_of(s, "lib.py", 1)
        log("\n[add a new caller] app.py  (callee's reference set changes)")
        log(f"  changed_ids  = {d2.changed_ids}")
        log(f"  affected_ids = {d2.affected_ids}")
        log(f"  callee(target) id now = {tid2}")
        log(f"  callee in affected_ids? {tid2 in set(d2.affected_ids)}   <-- KEY QUESTION")
        # Did the engine recompute the callee's refs layer?
        log(f"  refcount-generator invocations during this commit = {len(spike_gen.REFCOUNT_CALLS)}")
        log(f"  (callee re-derived? {tid2 in set(spike_gen.REFCOUNT_CALLS)})")

        # 3) Add a brand-new caller in a NEW file — strongest test.
        spike_gen.REFCOUNT_CALLS.clear()
        (root / "app2.py").write_text("from lib import target\n\n\ndef other() -> int:\n    return target(9)\n")
        d3 = s.sync_path("app2.py")
        tid3 = id_of(s, "lib.py", 1)
        log("\n[add a new caller in a NEW file] app2.py")
        log(f"  created_ids  = {d3.created_ids}")
        log(f"  affected_ids = {d3.affected_ids}")
        log(f"  callee in affected_ids? {tid3 in set(d3.affected_ids)}   <-- KEY QUESTION")
        log(f"  refcount invocations = {len(spike_gen.REFCOUNT_CALLS)}")
    finally:
        s.close()


# ── Spike D — identity matrix ───────────────────────────────────────────────


def fresh(tmp: Path, sub: str, lib=LIB_SRC) -> TyO3Session:
    root = tmp / sub
    make_project(root, lib=lib)
    s = TyO3Session(str(root))
    s.sync_all()
    return s


def spike_d(tmp: Path) -> None:
    banner("SPIKE D — identity matrix (edit / body / rename / atomic move) + QW2")

    # Case 0: baseline + author a note on target.
    s = fresh(tmp, "d0")
    try:
        tid0 = id_of(s, "lib.py", 1)
        s.author("tests", tid0, {"note": "load-bearing"})
        log(f"baseline target id = {tid0}; note authored")

        # Case 1: cosmetic whitespace edit (body semantics unchanged).
        cosmetic = LIB_SRC.replace("    return 0\n", "    return 0  \n")
        s.edit("lib.py", cosmetic)
        tid_c = id_of(s, "lib.py", 1)
        note_c = s.authored("tests", tid_c).status if tid_c else None
        log(f"[cosmetic edit]   id same? {tid_c == tid0}  note status@newid={note_c}")

        # Case 2: meaningful body change.
        s2body = LIB_SRC.replace("return x + 1", "return x + 99")
        s.edit("lib.py", s2body)
        tid_b = id_of(s, "lib.py", 1)
        log(f"[body change]     id same? {tid_b == tid0}  (expect True, binds 'changed')")
        log(f"                  note rides? {s.authored('tests', tid_b).status!r}")
    finally:
        s.close()

    # Case 3: in-place rename via a single edit.
    s = fresh(tmp, "d3")
    try:
        tid0 = id_of(s, "lib.py", 1)
        s.author("tests", tid0, {"note": "load-bearing"})
        renamed = LIB_SRC.replace("def target(", "def renamed(")
        d = s.edit("lib.py", renamed)
        tid_new = id_of(s, "lib.py", 1)
        log(f"\n[rename in place] old id = {tid0}")
        log(f"                  new id = {tid_new}  same? {tid_new == tid0}")
        log(f"                  delta: created={d.created_ids} changed={d.changed_ids} deleted={d.deleted_ids} moved={[m.id for m in d.moved]}")
        log(f"                  note @old id = {s.authored('tests', tid0).status!r}")
        log(f"                  note @new id = {s.authored('tests', tid_new).status!r}")
    finally:
        s.close()

    # Case 4: atomic MOVE (identical body to another file, one commit).
    s = fresh(tmp, "d4")
    try:
        tid0 = id_of(s, "lib.py", 1)
        s.author("tests", tid0, {"note": "load-bearing"})
        d = s.edit_many({"lib.py": "", "moved.py": LIB_SRC})  # remove + add verbatim
        # target now lives in moved.py
        tid_m = id_of(s, "moved.py", 1)
        log(f"\n[atomic move]     old id = {tid0}")
        log(f"                  new id = {tid_m}  same? {tid_m == tid0}")
        log(f"                  delta: moved={[(m.id, m.old_file, m.new_file) for m in d.moved]}")
        log(f"                  note rides? {s.authored('tests', tid_m).status!r} value={s.authored('tests', tid_m).value}")
    finally:
        s.close()

    # Case 5: QW2 — rename via atomic edit_many. Can ANY single commit preserve
    # the id across a NAME change? (rename changes name -> qualified_path AND,
    # if name is hashed, content_hash.)
    s = fresh(tmp, "d5")
    try:
        tid0 = id_of(s, "lib.py", 1)
        s.author("tests", tid0, {"note": "load-bearing"})
        renamed = LIB_SRC.replace("def target(", "def renamed(")
        # Atomic: relocate the renamed body to moved.py in one commit.
        d = s.edit_many({"lib.py": "", "moved.py": renamed})
        tid_new = id_of(s, "moved.py", 1)
        log(f"\n[QW2: rename+move atomically] old id = {tid0}")
        log(f"                  new id = {tid_new}  same? {tid_new == tid0}")
        log(f"                  delta: created={d.created_ids} changed={d.changed_ids} deleted={d.deleted_ids} moved={[m.id for m in d.moved]}")
        log(f"                  note @old={s.authored('tests', tid0).status!r}  @new={s.authored('tests', tid_new).status!r}")
    finally:
        s.close()


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="tyo3-spike-"))
    log(f"spike workspace: {tmp}")
    try:
        spike_abef(tmp)
        spike_c(tmp)
        spike_d(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    banner("SPIKES COMPLETE")


if __name__ == "__main__":
    main()
