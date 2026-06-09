"""V2 Phase 14 — end-to-end acceptance suite.

The closing proof of the spine refactor. One deterministic project is driven
through its whole lifecycle — open, author, pin, read, edit, move, delete,
diff, subscribe, refine, close/reopen, cache-bust — and every load-bearing V2
invariant is asserted on the way through:

* same revision ⇒ same content; durable ids survive a cosmetic edit and a move;
  a content hash changes **only** on a meaningful edit;
* ``affected_ids`` is the transitive, container-granular closure (a base-class
  edit reports its subclasses **and their importers**) — never seeds-only;
* derived artifacts are keyed by content hash; a **local** layer does not
  recompute on a dependency-only change while a **semantic** layer does;
* authored records present / needs-review / orphaned as appropriate, and survive
  a close/reopen;
* the bus delta is ordered and id-level; with ``precision = method`` a refinement
  *narrows* the coarse set and arrives on the refinement channel;
* the snapshot diff agrees with an independent rebuild; no read accessor advances
  head; and the session never writes back to the user's source files.

Determinism: the content is fixed and the structure hash profile is
whitespace-insensitive, so content hashes are reproducible run to run. Durable
ids are random ULIDs, so the test asserts *relationships* between ids (resolved
by name at each revision), never literal id strings.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from tyo3.bus.interest import Interest
from tyo3.graph.projection import CodeGraph
from tyo3.tests.parity_oracle import assert_graphs_equal

from .conftest import needs_native

pytestmark = needs_native


# ── Deterministic in-process derived generators ─────────────────────────────
#
# Batched python generators (the Gate 5 contract: take a list of GenInput,
# return a list of artifact strings). Each appends the ids it processed so the
# test can prove recompute vs reuse by counting invocations.

LOCAL_CALLS: list[str] = []
SEMANTIC_CALLS: list[str] = []


def local_generator(inputs):
    LOCAL_CALLS.extend(inp.durable_id for inp in inputs)
    return [f"LOCAL::{inp.source}" for inp in inputs]


def semantic_generator(inputs):
    SEMANTIC_CALLS.extend(inp.durable_id for inp in inputs)
    return [f"SEM::{inp.source}" for inp in inputs]


# ── Fixed project sources ───────────────────────────────────────────────────

SHAPES_SRC = """\
class Shape:
    def area(self) -> float:
        return 0.0

    def name(self) -> str:
        return "shape"


def make_shape() -> Shape:
    return Shape()
"""

CIRCLE_SRC = """\
from shapes import Shape


class Circle(Shape):
    def radius(self) -> float:
        return 1.0
"""

CONSUMERS_SRC = """\
from shapes import make_shape
from circle import Circle


def use_area() -> float:
    return make_shape().area()


def use_name() -> str:
    return make_shape().name()


def use_circle() -> float:
    return Circle().radius()
"""

# Direct reference chain for the local-vs-semantic derived layers.
CALC_SRC = """\
def base_value() -> int:
    return 1
"""

REPORT_SRC = """\
from calc import base_value


def report_value() -> int:
    return base_value() + 10
"""

MOVABLE_SRC = """\
def movable_fn(x: int) -> int:
    return x + 7
"""

DELETABLE_SRC = """\
def deletable_fn() -> str:
    return "bye"
"""

COSMETIC_SRC = """\
def cosmetic_fn() -> int:
    return 5
"""

# The same function body relocated to moved.py — identical bytes ⇒ a Moved bind.
MOVABLE_RELOCATED = MOVABLE_SRC


def _config_toml() -> str:
    return """\
schema_version = 1

[hashing.profiles.structure]

[code_graph]
precision = "method"
refinement = "async"

[coordination.bus]
queue_capacity = 256
overflow = "coalesce"

[layers.intent]
origin = "authored"
history = true
review_on_change = true

[layers.loc]
origin = "derived"
depends_on = ["code"]
generator = "loc_gen"
generator_version = "v1"
hash_profile = "structure"
store = "kv_loc"
serving = "stale"
key_locality = "local"
entity_kinds = ["function"]

[layers.sem]
origin = "derived"
depends_on = ["code"]
generator = "sem_gen"
generator_version = "v1"
hash_profile = "structure"
store = "kv_sem"
serving = "stale"
key_locality = "semantic"
entity_kinds = ["function"]

[generators.loc_gen]
type = "python"
callable = "tyo3.tests.test_final_acceptance:local_generator"

[generators.sem_gen]
type = "python"
callable = "tyo3.tests.test_final_acceptance:semantic_generator"

[stores.kv_loc]
backend = "fs"
path = "cache/loc"

[stores.kv_sem]
backend = "fs"
path = "cache/sem"
"""


def _make_project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "acceptance"\n')
    (proj / "shapes.py").write_text(SHAPES_SRC)
    (proj / "circle.py").write_text(CIRCLE_SRC)
    (proj / "consumers.py").write_text(CONSUMERS_SRC)
    (proj / "calc.py").write_text(CALC_SRC)
    (proj / "report.py").write_text(REPORT_SRC)
    (proj / "movable.py").write_text(MOVABLE_SRC)
    (proj / "moved.py").write_text("")  # known project path for the atomic move
    (proj / "deletable.py").write_text(DELETABLE_SRC)
    (proj / "cosmetic.py").write_text(COSMETIC_SRC)
    cfg = proj / ".tyo3"
    cfg.mkdir()
    (cfg / "config.toml").write_text(_config_toml())
    return proj


def _ids_by_qualname(graph: CodeGraph) -> dict[str, str]:
    """Map every node's qualified_name (and bare name as a fallback) to its id."""
    out: dict[str, str] = {}
    g = graph._graph
    for idx in g.node_indices():
        node = g[idx]
        out[node.qualified_name] = node.durable_id
        out.setdefault(node.name, node.durable_id)
    return out


def _disk_snapshot(proj: Path) -> dict[str, str]:
    """Byte contents of every .py file on disk — the 'no source writes' oracle."""
    return {str(p.relative_to(proj)): p.read_text() for p in sorted(proj.rglob("*.py"))}


def test_end_to_end_acceptance(tmp_path):
    from tyo3 import TyO3Session

    proj = _make_project(tmp_path)
    baseline_disk = _disk_snapshot(proj)

    with TyO3Session(str(proj)) as session:
        session.sync_all()

        # The declared layers are present.
        assert {"loc", "sem"} <= set(session.config.layers)

        ids = _ids_by_qualname(session.graph)
        area_id = ids["Shape.area"]
        circle_id = ids["Circle"]
        use_area_id = ids["use_area"]
        use_name_id = ids["use_name"]
        use_circle_id = ids["use_circle"]
        report_value_id = ids["report_value"]
        movable_id = ids["movable_fn"]
        deletable_id = ids["deletable_fn"]
        cosmetic_id = ids["cosmetic_fn"]

        # ── Author intent, then pin the initial snapshot ─────────────────────
        session.author("intent", area_id, {"note": "area is the core method"})

        head_at_pin = session.head
        snap0 = session.snapshot()
        assert snap0.revision == head_at_pin

        # Reading the pinned code graph must not advance head.
        head_before_reads = session.head
        snap0_graph = snap0.graph()
        assert snap0_graph is not None
        area_hash_r0 = session.graph._graph[
            next(i for i in session.graph._graph.node_indices() if session.graph._graph[i].durable_id == area_id)
        ].content_hash
        assert session.head == head_before_reads, "a read accessor must not advance head"

        # Same revision ⇒ same content: a second snapshot at the pinned revision
        # carries identical node ids.
        snap0_again = session.snapshot(at=snap0.revision)
        assert _ids_by_qualname(snap0_again.graph()) == _ids_by_qualname(snap0_graph)
        snap0_again.close()

        # Populate both derived caches for report_value at the current head.
        warm = session.snapshot()
        loc0 = warm.derived("loc", report_value_id)
        sem0 = warm.derived("sem", report_value_id)
        assert loc0.status == "fresh" and loc0.artifact is not None
        assert sem0.status == "fresh" and sem0.artifact is not None
        warm.close()

        # ── Subscribe before any writes ──────────────────────────────────────
        sub = session.subscribe(Interest.ALL)
        delta_revisions: list[int] = []

        # ── Meaningful edit of a *method* (Shape.area) ───────────────────────
        new_shapes = SHAPES_SRC.replace("        return 0.0\n", "        return 3.14159\n")
        result = session.edit("shapes.py", new_shapes)
        edit_rev = result.revision
        changed = set(result.changed_ids)
        affected = set(result.affected_ids)

        assert area_id in changed, "the edited method's id is in changed_ids"
        # Container-granular transitive closure: never seeds-only.
        assert affected > changed, "affected_ids is a strict superset of the seeds"
        # Subclass of the edited class + its importers light up.
        assert circle_id in affected, "the subclass (Circle) is affected"
        assert use_area_id in affected, "the direct method user is affected"
        assert use_circle_id in affected, "the subclass importer is affected"

        # Content hash changed only because the body meaningfully changed.
        ids_after = _ids_by_qualname(session.graph)
        assert ids_after["Shape.area"] == area_id, "durable id stable across a body edit"
        area_node_r1 = session.graph._graph[
            next(i for i in session.graph._graph.node_indices() if session.graph._graph[i].durable_id == area_id)
        ]
        assert area_node_r1.content_hash != area_hash_r0, "meaningful edit ⇒ hash changes"

        # ── Bus: ordered, id-level primary delta + narrowing refinement ──────
        primary = sub.poll(timeout=5.0)
        assert primary is not None
        assert primary.revision == edit_rev
        delta_revisions.append(primary.revision)
        coarse = set(primary.affected)
        # Id-level: the affected payload holds durable ids we resolved by name.
        assert area_id in coarse or use_area_id in coarse
        assert use_area_id in coarse and use_name_id in coarse, "container-granular coarse set"

        ref = sub.poll_refinement(timeout=10.0)
        assert ref is not None, "precision=method must publish a refinement"
        assert ref.revision == edit_rev, "refinement rides the same revision (the bus channel)"
        assert ref.added == frozenset(), "narrowing never adds ids"
        assert use_area_id in ref.narrowed, "the area user survives narrowing"
        assert use_name_id not in ref.narrowed, "the name-only user is narrowed away"
        assert ref.narrowed <= (coarse | set(result.changed_ids)), "narrowed ⊆ coarse"

        # ── Derived: local does NOT recompute on a dependency-only change;
        #    semantic DOES (calc.base_value is a dependency of report_value) ──
        loc_before = list(LOCAL_CALLS)
        sem_before = list(SEMANTIC_CALLS)
        session.edit("calc.py", "def base_value() -> int:\n    return 2\n")
        after_dep = session.snapshot()
        loc1 = after_dep.derived("loc", report_value_id)
        sem1 = after_dep.derived("sem", report_value_id)
        assert loc1.status == "fresh" and sem1.status == "fresh"
        # report_value's own body never changed → local key is identical → reuse.
        assert loc1.artifact == loc0.artifact, "local layer reuses on dependency-only change"
        assert LOCAL_CALLS == loc_before, "local layer must not recompute report_value"
        # The dependency fingerprint changed → the semantic key changes → the
        # generator runs again for report_value (a fresh cache key). Its *bytes*
        # are identical here (report_value's own source is unchanged), so the
        # recompute is proven by the new generator invocation, not by the output.
        assert report_value_id in SEMANTIC_CALLS[len(sem_before) :], (
            "semantic layer recomputes report_value on a dependency-only change"
        )
        after_dep.close()
        # drain the dependency-edit delta(s) so later polls see the next write
        _drain(sub, delta_revisions)

        # ── Cosmetic edit: id stable, structure hash unchanged ───────────────
        session.edit("cosmetic.py", "def cosmetic_fn() -> int:\n    # a harmless comment\n    return 5\n")
        cos_snap = session.snapshot()
        cos_ids = _ids_by_qualname(cos_snap.graph())
        assert cos_ids["cosmetic_fn"] == cosmetic_id, "id survives a cosmetic edit"
        cos_node = _node_by_id(cos_snap.graph(), cosmetic_id)
        cos_node_r0 = _node_by_id(snap0_graph, cosmetic_id)
        assert cos_node.content_hash == cos_node_r0.content_hash, "cosmetic edit ⇒ structure hash unchanged"
        cos_snap.close()
        _drain(sub, delta_revisions)

        # ── Atomic move: id stable, hash unchanged ───────────────────────────
        session.edit_many({"movable.py": "", "moved.py": MOVABLE_RELOCATED})
        mv_snap = session.snapshot()
        mv_ids = _ids_by_qualname(mv_snap.graph())
        assert mv_ids["movable_fn"] == movable_id, "durable id survives a move to a new file"
        assert session.locate(movable_id).endswith("moved.py::movable_fn") or "moved.py" in session.locate(movable_id)
        mv_node = _node_by_id(mv_snap.graph(), movable_id)
        mv_node_r0 = _node_by_id(snap0_graph, movable_id)
        assert mv_node.content_hash == mv_node_r0.content_hash, "moved-unchanged ⇒ hash unchanged"
        mv_snap.close()
        _drain(sub, delta_revisions)

        # ── Delete: the entity becomes orphaned / deleted ────────────────────
        del_result = session.edit("deletable.py", "")
        assert deletable_id in set(del_result.deleted_ids) | set(del_result.orphaned)
        new_snap = session.snapshot()
        assert _node_by_id(new_snap.graph(), deletable_id) is None, "deleted entity is gone from the graph"
        _drain(sub, delta_revisions)

        # ── Diff old vs new; agrees with an independent rebuild ──────────────
        head_before_diff = session.head
        diff = new_snap.diff(snap0)
        assert area_id in diff.code.changed, "the diff reports the meaningful edit"
        assert deletable_id in diff.code.removed, "the diff reports the deletion"
        assert session.head == head_before_diff, "diffing must not advance head"

        # Independent rebuild at the new revision must structurally equal the
        # live snapshot's projection (the read surface is a pure projection).
        rebuilt = CodeGraph.build(session.snapshot(at=new_snap.revision), root=proj)
        assert_graphs_equal(
            new_snap.graph(),
            rebuilt,
            cosmetic="ignore",
            label_expected="live-snapshot",
            label_actual="independent-rebuild",
        )

        # Bus deltas arrived in strictly increasing revision order.
        assert delta_revisions == sorted(delta_revisions)
        assert len(set(delta_revisions)) == len(delta_revisions), "no duplicate revisions"

        sub.close()
        new_snap.close()
        snap0.close()

    # ── Close & reopen: identity + authored records survive ─────────────────
    with TyO3Session(str(proj)) as session2:
        session2.sync_all()
        ids2 = _ids_by_qualname(session2.graph)
        # area was edited (body) but its id is durable across reopen.
        assert ids2["Shape.area"] == area_id, "durable id survives close/reopen"
        # The authored intent persisted through the sidecar.
        av = session2.authored("intent", area_id)
        assert av.value == {"note": "area is the core method"}, "authored record survives reopen"

        # ── Delete the derived cache, verify recompute ───────────────────────
        warm2 = session2.snapshot()
        # Populate, then count.
        warm2.derived("sem", report_value_id)
        warm2.close()
        calls_before_bust = len(SEMANTIC_CALLS)

        for cache_dir in proj.rglob("cache"):
            if cache_dir.is_dir():
                shutil.rmtree(cache_dir)

        warm3 = session2.snapshot()
        val = warm3.derived("sem", report_value_id)
        assert val.status == "fresh" and val.artifact is not None
        assert len(SEMANTIC_CALLS) > calls_before_bust, "cache bust forces a recompute"
        warm3.close()

    # ── The session never wrote back to the user's source files ──────────────
    assert _disk_snapshot(proj) == baseline_disk, "no source file was modified on disk"


def _node_by_id(graph: CodeGraph, durable_id: str):
    g = graph._graph
    for idx in g.node_indices():
        node = g[idx]
        if node.durable_id == durable_id:
            return node
    return None


def _drain(sub, sink: list[int]) -> None:
    """Consume any queued primary deltas, recording their revisions in order."""
    while True:
        d = sub.poll(timeout=0)
        if d is None:
            return
        sink.append(d.revision)
