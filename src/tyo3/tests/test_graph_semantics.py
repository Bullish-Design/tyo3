"""Focused regression tests for graph semantics.

Captures known bugs before refactoring — some tests will fail at first.
See TyO3_REVIEW_2_REFACTORING_GUIDE.md §Phase 0.
"""

from __future__ import annotations

from tyo3.graph import CodeGraph, EdgeData, EdgeKind, SymbolNode
from tyo3.models.analysis import Range
from tyo3.models.symbols import SymbolKind
from tyo3.tests.conftest import get_graph, needs_native
from tyo3.tests.graph_helpers import edges_of_kind, find_one

# ── Phase 0.2: References attach to functions, not modules ───────────────


@needs_native
def test_reference_from_create_user_targets_user_class() -> None:
    """A reference inside ``create_user()`` must target ``User`` in models.py."""
    graph = get_graph("graph_test")

    create_user = find_one(
        graph,
        file_suffix="app.py",
        name="create_user",
        kind=SymbolKind.FUNCTION,
    )
    user = find_one(
        graph,
        file_suffix="models.py",
        name="User",
        kind=SymbolKind.CLASS,
    )

    targets = {
        target.durable_id
        for target, edge in graph.references_from(create_user.durable_id)
        if edge.kind == EdgeKind.REFERENCES
    }

    assert user.durable_id in targets, f"create_user should reference User, got references: {targets}"


@needs_native
def test_reference_does_not_attach_to_app_module() -> None:
    """References from app.py should not land on ``app.py::<module>``."""
    graph = get_graph("graph_test")

    app_modules = [node for node in graph.symbols_of_kind(SymbolKind.MODULE) if node.file.endswith("app.py")]
    assert len(app_modules) == 1

    app_module_refs = graph.references_from(app_modules[0].durable_id)
    local_model_targets = [
        target for target, _edge in app_module_refs if target.file.endswith("models.py") and not target.external
    ]
    assert local_model_targets == [], (
        f"app.py::<module> should not have local references to models.py, "
        f"got: {[t.durable_id for t in local_model_targets]}"
    )


# ── Phase 0.3: Override edges ────────────────────────────────────────────


@needs_native
def test_user_save_overrides_base_save() -> None:
    """``User.save`` should have an OVERRIDES edge to ``Base.save``.

    Two ``save`` methods exist in models.py (Base.save and User.save),
    so ``find_one`` can't disambiguate by name + kind alone.  Select
    each explicitly using ``qualified_name``.
    """
    graph = get_graph("graph_test")

    save_methods = [
        node
        for node in graph.symbols_of_kind(SymbolKind.METHOD)
        if node.file.endswith("models.py") and node.name == "save" and not node.external
    ]
    assert len(save_methods) >= 2, (
        f"Expected at least 2 save methods in models.py, got {[n.durable_id for n in save_methods]}"
    )

    base_saves = [n for n in save_methods if n.qualified_name == "Base.save"]
    user_saves = [n for n in save_methods if n.qualified_name == "User.save"]
    assert len(base_saves) == 1, f"Expected 1 Base.save, got {[n.durable_id for n in base_saves]}"
    assert len(user_saves) == 1, f"Expected 1 User.save, got {[n.durable_id for n in user_saves]}"
    base_save = base_saves[0]
    user_save = user_saves[0]

    override_edges = edges_of_kind(graph, EdgeKind.OVERRIDES)
    assert (user_save.durable_id, base_save.durable_id) in override_edges, (
        f"Expected OVERRIDES edge from {user_save.durable_id} to {base_save.durable_id}, "
        f"got OVERRIDES edges: {override_edges}"
    )


# ── Phase 0.4: Dependency queries exclude structural edges ────────────────


def _range() -> Range:
    return Range.model_validate({"start": {"line": 1, "column": 1}, "end": {"line": 1, "column": 1}})


def test_dependencies_do_not_include_defined_children() -> None:
    """``dependencies(module)`` must exclude structurally defined children.

    A ``DEFINES`` edge from module to function is a structural edge,
    not a dependency.  ``dependencies()`` must return the empty set.
    This test should fail before Phase 5 of the refactoring guide.
    """
    graph = CodeGraph()
    module = SymbolNode(
        durable_id="a.py::<module>",
        name="a",
        qualified_name="<module>",
        kind=SymbolKind.MODULE,
        file="a.py",
        range=_range(),
    )
    function = SymbolNode(
        durable_id="a.py::f",
        name="f",
        qualified_name="f",
        kind=SymbolKind.FUNCTION,
        file="a.py",
        range=_range(),
    )
    graph._add_node(module)
    graph._add_node(function)
    graph._add_edge(
        module.durable_id,
        function.durable_id,
        EdgeData(kind=EdgeKind.DEFINES),
        "a.py",
    )

    assert graph.children(module.durable_id) == [function]
    assert graph.dependencies(module.durable_id) == set(), (
        f"dependencies() must exclude DEFINES edges; got {graph.dependencies(module.durable_id)}"
    )


def test_dependencies_include_references() -> None:
    """``dependencies()`` must include semantic REFERENCES edges."""
    graph = CodeGraph()
    f = SymbolNode(
        durable_id="a.py::f",
        name="f",
        qualified_name="f",
        kind=SymbolKind.FUNCTION,
        file="a.py",
        range=_range(),
    )
    g = SymbolNode(
        durable_id="a.py::g",
        name="g",
        qualified_name="g",
        kind=SymbolKind.FUNCTION,
        file="a.py",
        range=_range(),
    )
    graph._add_node(f)
    graph._add_node(g)
    graph._add_edge(
        f.durable_id,
        g.durable_id,
        EdgeData(kind=EdgeKind.REFERENCES),
        "a.py",
    )

    assert graph.dependencies(f.durable_id) == {g.durable_id}, (
        f"dependencies(f) should include REFERENCES target g, got {graph.dependencies(f.durable_id)}"
    )
    assert graph.dependents(g.durable_id) == {f.durable_id}, (
        f"dependents(g) should include REFERENCE source f, got {graph.dependents(g.durable_id)}"
    )


# NOTE: ``test_cross_file_project_target_mismatch_is_reported`` exercised
# ``CodeGraph._resolve_references_via_occurrences``, the Python per-occurrence
# reference resolver that reported a ``ProjectLocalTargetMismatch`` build
# failure when a cross-file target could not be resolved. Under Gate 3N,
# cross-file reference resolution (and its inbound revalidation) happens
# authoritatively in the native CodeDelta producer, so that Python path and
# its test were removed with the rewrite.
