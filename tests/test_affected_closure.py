"""Phase 6 (§6.3) — ``affected_ids`` is the transitive, container-granular
closure computed natively over the in-commit-maintained ``reverse_deps``.

Before Phase 6 the in-commit producer was deferred and ``head.code_layer`` was
empty, so ``affected_ids`` was *seeds-only* (``changed ∪ deleted``). With the
scoped producer landed, ``affected_ids`` follows the reverse-dependency chain:

  * a **base-class edit** reports its subclasses (and their importers);
  * a **member-body edit** reaches *named* consumers at container granularity
    (the class hash subsumes member bodies; the named chain carries coverage —
    see ``test_inference_flow_coverage.py``);
  * a **deletion** still reports its dependents, seeded from the *prior* layer's
    reverse-deps (the deleted node's inbound edges are gone from the new layer).

These prove the closure is transitive *at the source* — no Python graph walk.
"""

from __future__ import annotations

import textwrap
from pathlib import Path as StdPath

from tyo3 import TyO3Session


def _affected_names(session: TyO3Session, ids) -> set[str]:
    """Map ``affected_ids`` to the leaf names of the head graph's nodes."""
    g = session.graph
    by_id = {g.graph[i].durable_id: g.graph[i].name for i in g.graph.node_indices()}
    return {by_id[i] for i in ids if i in by_id}


def _full_delta_dependency_closure(session: TyO3Session, seeds, deleted) -> set[str]:
    """Recompute the dependency closure from the public full code delta.

    Deleted ids are intentionally omitted: they are absent from the current
    full delta and the commit's closure handles their prior-layer edges.
    """
    full = session._inner.full_code_delta()
    node_ids = {node["durable_id"] for node in full["nodes_upserted"]}
    reverse: dict[str, set[str]] = {}
    for edge in full["edges_added"]:
        if edge["kind"] in {"references", "imports", "inherits", "overrides"}:
            reverse.setdefault(edge["destination_id"], set()).add(edge["source_id"])

    closure: set[str] = set()
    queue = [seed for seed in set(seeds) - set(deleted) if seed in node_ids]
    while queue:
        current = queue.pop()
        if current in closure:
            continue
        closure.add(current)
        queue.extend(source for source in reverse.get(current, ()) if source in node_ids)
    return closure


def test_member_body_edit_reaches_named_consumers(tmp_path: StdPath) -> None:
    """Editing only ``Widget.draw``'s body reports ``Widget``, ``make_widget``
    and ``render`` — container-granular coverage via the class hash + the named
    reference chain ``render -> make_widget -> Widget`` (§5.2)."""
    (tmp_path / "widget.py").write_text(
        textwrap.dedent("""\
            class Widget:
                def draw(self):
                    return "drawing"

            def make_widget():
                return Widget()
        """)
    )
    (tmp_path / "app.py").write_text(
        textwrap.dedent("""\
            from widget import make_widget

            def render():
                w = make_widget()
                return w.draw()
        """)
    )
    with TyO3Session(str(tmp_path)) as s:
        _ = s.graph  # materialise the head graph
        delta = s.edit(
            "widget.py",
            textwrap.dedent("""\
                class Widget:
                    def draw(self):
                        return "DRAWN"

                def make_widget():
                    return Widget()
            """),
        )
        names = _affected_names(s, delta.affected_ids)
        assert {"Widget", "make_widget", "render"} <= names, (
            f"expected transitive container-granular affected, got {sorted(names)}"
        )


def test_base_class_edit_reports_subclass(tmp_path: StdPath) -> None:
    """Editing a base class reports its (cross-file) subclass — the closure
    walks the ``inherits`` reverse-dependency edge."""
    (tmp_path / "base.py").write_text(
        textwrap.dedent("""\
            class Base:
                def greet(self):
                    return "hi"
        """)
    )
    (tmp_path / "derived.py").write_text(
        textwrap.dedent("""\
            from base import Base

            class Derived(Base):
                pass
        """)
    )
    with TyO3Session(str(tmp_path)) as s:
        _ = s.graph
        delta = s.edit(
            "base.py",
            textwrap.dedent("""\
                class Base:
                    def greet(self):
                        return "hello"
            """),
        )
        names = _affected_names(s, delta.affected_ids)
        assert "Base" in names, names
        assert "Derived" in names, f"base-class edit must report its subclass via reverse_deps, got {sorted(names)}"


def test_normal_edit_is_scoped_not_full_rescan(tmp_path: StdPath) -> None:
    """A normal single-file edit re-derives only the dirty scope (§6.4): it is
    incremental (``rescan = False``) and reconciliation is bounded to the edited
    file — unrelated files are never re-analyzed.

    This guards the perf contract (the full producer is the ~100x trap) with a
    deterministic structural assertion rather than a flaky wall-clock bound. The
    structural ``code_delta`` this once inspected was retired (Project 31, #2);
    scoping is now asserted via the public ``identity_scope_files`` debug counter
    (1 for a one-file scoped edit; 0 on a full rescan, which has no scope) and the
    id-level delta, which the bounded producer drives from the same scope.
    """
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    (tmp_path / "c.py").write_text("z = 3\n")
    with TyO3Session(str(tmp_path)) as s:
        _ = s.graph
        # First write of the session lazily full-builds the native layer; the
        # SECOND write is the scoped path under test.
        s.edit("a.py", "x = 1\n# warm\n")
        delta = s.edit("a.py", "x = 1\ndef helper():\n    return 7\n")

        assert not delta.rescan, "a normal edit must be incremental, not a full rescan"
        assert delta.identity_scope_files == 1, (
            f"a scoped one-file edit reconciles exactly the edited file, got "
            f"identity_scope_files={delta.identity_scope_files}"
        )
        # The new helper() is minted and maps to the edited file; the bounded
        # producer never reaches the untouched files' entities.
        created = _affected_names(s, delta.created_ids)
        assert "helper" in created, f"new helper() should be a created id, got {sorted(created)}"


def test_deleted_base_reports_dependents_from_prior_layer(tmp_path: StdPath) -> None:
    """Deleting a base class still reports its dependents — a deleted id's
    inbound edges are gone from the new layer, so the closure seeds them from
    the *prior* layer's reverse-deps (§6.3 deletion subtlety)."""
    (tmp_path / "base.py").write_text(
        textwrap.dedent("""\
            class Base:
                def greet(self):
                    return "hi"
        """)
    )
    (tmp_path / "derived.py").write_text(
        textwrap.dedent("""\
            from base import Base

            class Derived(Base):
                pass
        """)
    )
    with TyO3Session(str(tmp_path)) as s:
        g = s.graph
        # Capture the subclass id BEFORE the deletion (it survives; only Base goes).
        derived_id = next(
            g.graph[i].durable_id
            for i in g.graph.node_indices()
            if g.graph[i].name == "Derived" and g.graph[i].file == "derived.py"
        )
        # Warm the native layer so the deletion runs the scoped (not first-build)
        # path, exercising the prior-layer deletion seed.
        s.edit("derived.py", (tmp_path / "derived.py").read_text())

        (tmp_path / "base.py").unlink()
        delta = s.sync_path("base.py")

        assert delta.deleted, "expected a deleted file in the delta"
        assert derived_id in set(delta.affected_ids), (
            "deleting Base must still report its dependent Derived (seeded from "
            f"the prior layer's reverse-deps); affected={list(delta.affected_ids)}"
        )


def test_commit_affected_ids_cover_full_delta_dependency_closure(tmp_path: StdPath) -> None:
    """The commit funnel's affected ids cover the closure of current edges.

    This independently recomputes the expected closure from ``full_code_delta``
    after the commit, while excluding deleted seeds that only exist in the
    prior layer.
    """
    (tmp_path / "base.py").write_text(
        textwrap.dedent("""\
            class Base:
                def greet(self):
                    return "hi"
        """)
    )
    (tmp_path / "derived.py").write_text(
        textwrap.dedent("""\
            from base import Base

            class Derived(Base):
                pass
        """)
    )
    with TyO3Session(str(tmp_path)) as s:
        _ = s.graph
        delta = s.edit(
            "base.py",
            textwrap.dedent("""\
                class Base:
                    def greet(self):
                        return "hello"
            """),
        )

        expected = _full_delta_dependency_closure(
            s,
            set(delta.changed_ids) | set(delta.created_ids),
            delta.deleted_ids,
        )
        assert expected <= set(delta.affected_ids), (
            "commit affected_ids must cover the dependency closure recomputed "
            f"from full_code_delta: expected={sorted(expected)}, "
            f"actual={sorted(delta.affected_ids)}"
        )
