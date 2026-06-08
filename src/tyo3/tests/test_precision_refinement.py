"""Phase 9 (V2) — async precision refinement layer.

Proves the three properties that define the phase:

1. **Narrowing correctness** — with ``precision = method`` a refinement narrows
   the container-granular coarse ``affected`` set to the entities that actually
   depend on a changed *member*, retains ``changed_ids``, and stays a subset of
   the coarse set, all at the *same* revision (rides the Phase-7 channel).
2. **Graceful degradation** — a crashing/stalling worker never turns into a
   miss: the synchronous coarse set is still delivered and correct, the writer
   never raises, and no bogus refinement leaks.
3. **Container-mode no-op** — the default ``precision = container`` never builds
   or starts the refiner and never publishes a refinement; the writer pays
   nothing.
"""

from __future__ import annotations

from tyo3.bus.interest import Interest

from .conftest import needs_native

pytestmark = needs_native

# A small project: Widget.{draw,serialize}; a factory returning Widget; two
# consumers, one using draw, one using serialize. The nominal chain
# (consume_* → make_widget → Widget) makes both consumers land in the coarse
# affected set; only the draw user truly depends on draw's body.
WIDGET_SRC = '''\
class Widget:
    def draw(self) -> str:
        return "draw"

    def serialize(self) -> str:
        return "ser"


def make_widget() -> Widget:
    return Widget()


def consume_a() -> str:
    w = make_widget()
    return w.draw()


def consume_b() -> str:
    w = make_widget()
    return w.serialize()
'''


def _make_project(tmp_path, *, precision: str = "method", refinement: str = "async"):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
    (proj / "widget.py").write_text(WIDGET_SRC)
    cfg = proj / ".tyo3"
    cfg.mkdir()
    body = "schema_version = 1\n\n[hashing.profiles.structure]\n\n"
    if precision is not None:
        body += f'[code_graph]\nprecision = "{precision}"\nrefinement = "{refinement}"\n\n'
    body += '[coordination.bus]\nqueue_capacity = 256\noverflow = "coalesce"\n'
    (cfg / "config.toml").write_text(body)
    return proj


def _name_to_id(session) -> dict[str, str]:
    """Map unique entity names → durable ids from the live head graph."""
    g = session.graph
    out: dict[str, str] = {}
    for i in g._graph.node_indices():
        node = g._graph[i]
        out[node.name] = node.durable_id
    return out


def test_narrows_to_member_users(tmp_path):
    """A draw-body edit: coarse affected includes every Widget referrer; the
    async refinement narrows it to the draw user (+ changed ids), dropping the
    serialize-only user, at the same revision."""
    from tyo3 import TyO3Session

    proj = _make_project(tmp_path, precision="method", refinement="async")
    with TyO3Session(str(proj)) as session:
        session.sync_all()
        ids = _name_to_id(session)
        a, b = ids["consume_a"], ids["consume_b"]

        sub = session.subscribe(Interest.ALL)

        new_src = WIDGET_SRC.replace('return "draw"', 'return "DRAW_CHANGED"')
        result = session.edit("widget.py", new_src)
        revision = result.revision
        changed = set(result.changed_ids)

        # Primary (coarse) delta: both consumers are reachable via the named
        # chain, so both are in the container-granular affected set.
        primary = sub.poll(timeout=5.0)
        assert primary is not None
        assert primary.revision == revision
        coarse = set(primary.affected)
        assert a in coarse, "draw user must be in coarse affected"
        assert b in coarse, "serialize user must be in coarse affected (container-granular)"

        # Refinement: narrowed to the draw user + changed ids; serialize-only
        # user dropped; same revision; subset of coarse.
        ref = sub.poll_refinement(timeout=10.0)
        assert ref is not None, "a refinement must be published for the method-precision commit"
        assert ref.revision == revision
        assert ref.added == frozenset(), "narrowing mode never publishes added ids"
        assert a in ref.narrowed, "draw user must survive narrowing"
        assert b not in ref.narrowed, "serialize-only user must be narrowed away"
        assert changed <= ref.narrowed, "narrowing must always retain changed ids"
        assert ref.narrowed <= (coarse | changed), "narrowed must be a subset of the coarse set"

        sub.close()


def test_graceful_degradation_on_worker_crash(tmp_path, monkeypatch):
    """A worker that raises must not produce a miss: the coarse set is still
    delivered and correct, the writer never raises, and no refinement leaks."""
    from tyo3 import TyO3Session
    from tyo3.precision.refiner import PrecisionRefiner

    # Force every refinement computation to blow up.
    def _boom(self, *args, **kwargs):
        raise RuntimeError("induced refiner failure")

    monkeypatch.setattr(PrecisionRefiner, "_compute_narrowed", _boom)

    proj = _make_project(tmp_path, precision="method", refinement="async")
    with TyO3Session(str(proj)) as session:
        session.sync_all()
        ids = _name_to_id(session)
        a, b = ids["consume_a"], ids["consume_b"]

        sub = session.subscribe(Interest.ALL)

        # The write itself must succeed despite the doomed worker.
        new_src = WIDGET_SRC.replace('return "draw"', 'return "DRAW_CHANGED"')
        result = session.edit("widget.py", new_src)

        # Coarse set is delivered and complete (no miss).
        primary = sub.poll(timeout=5.0)
        assert primary is not None
        assert primary.revision == result.revision
        coarse = set(primary.affected)
        assert a in coarse and b in coarse

        # No refinement is published (the worker crashed before publishing).
        assert sub.poll_refinement(timeout=1.0) is None

        # The session/bus stay healthy: a second write still delivers.
        result2 = session.edit("widget.py", WIDGET_SRC)
        primary2 = sub.poll(timeout=5.0)
        assert primary2 is not None
        assert primary2.revision == result2.revision

        sub.close()


def test_container_mode_is_a_no_op(tmp_path):
    """Default precision=container: the refiner is never built/started and no
    refinement is ever published; primary delivery is unchanged."""
    from tyo3 import TyO3Session

    proj = _make_project(tmp_path, precision="container", refinement="async")
    with TyO3Session(str(proj)) as session:
        session.sync_all()
        assert session.config.code_graph.precision == "container"

        sub = session.subscribe(Interest.ALL)

        new_src = WIDGET_SRC.replace('return "draw"', 'return "DRAW_CHANGED"')
        result = session.edit("widget.py", new_src)

        primary = sub.poll(timeout=5.0)
        assert primary is not None
        assert primary.revision == result.revision

        # The refiner was never constructed and nothing arrived on the channel.
        assert session._refiner is None
        assert sub.poll_refinement(timeout=0.5) is None

        sub.close()


def test_sync_refinement_runs_inline(tmp_path):
    """refinement=sync computes the narrowing inline (after primary publish);
    the refinement is already queued by the time edit() returns."""
    from tyo3 import TyO3Session

    proj = _make_project(tmp_path, precision="method", refinement="sync")
    with TyO3Session(str(proj)) as session:
        session.sync_all()
        ids = _name_to_id(session)
        a, b = ids["consume_a"], ids["consume_b"]

        sub = session.subscribe(Interest.ALL)

        new_src = WIDGET_SRC.replace('return "draw"', 'return "DRAW_CHANGED"')
        result = session.edit("widget.py", new_src)

        # Sync mode: no daemon thread is started.
        assert session._refiner is not None
        assert session._refiner._thread is None

        # Refinement is available immediately (timeout=0, non-blocking).
        ref = sub.poll_refinement(timeout=0.0)
        assert ref is not None
        assert ref.revision == result.revision
        assert a in ref.narrowed
        assert b not in ref.narrowed

        sub.close()
