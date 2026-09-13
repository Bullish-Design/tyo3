"""Regression guards for the affected-set coverage model (§5.4 / §5.11).

The native producer builds ``reverse_deps`` from **named** references/imports/
inheritance only. A dependency that exists *only* through an inferred receiver
type — ``w = make_widget(); w.draw()`` — produces **no** edge to the method.
We characterised (see the spine-refactor brainstorm) that affected-set
*coverage* is nonetheless preserved at **container granularity**, because:

  1. a method-body edit also bumps the *enclosing class's* content_hash
     (the class hash subsumes its members' bodies), and
  2. the class is reachable from consumers via the **named** reference chain
     (``render -> make_widget -> Widget``).

So coverage rides on the conjunction of (1) and (2). These tests lock both in,
and add a canary on the missing inference-flow edge so a future ty bump that
*starts* emitting it fails loudly (at which point method-level precision
becomes available and the container-granular reasoning should be revisited).
"""

from __future__ import annotations

from pathlib import Path as StdPath

from tyo3 import TyO3Session
from tyo3.graph import CodeGraph, EdgeKind


def _name_to_hash(graph: CodeGraph) -> dict[str, str]:
    """Map ``file::name`` -> content_hash for every node."""
    return {
        f"{graph.graph[i].file}::{graph.graph[i].name}": graph.graph[i].content_hash for i in graph.graph.node_indices()
    }


def _edge_exists(
    graph: CodeGraph,
    *,
    src_name: str,
    src_file: str,
    dst_name: str,
    dst_file: str,
    kind: EdgeKind,
) -> bool:
    for ei in graph.graph.edge_indices():
        data = graph.graph.get_edge_data_by_index(ei)
        if data.kind != kind:
            continue
        s_idx, d_idx = graph.graph.get_edge_endpoints_by_index(ei)
        s, d = graph.graph[s_idx], graph.graph[d_idx]
        if s.name == src_name and s.file == src_file and d.name == dst_name and d.file == dst_file:
            return True
    return False


def test_container_hash_subsumes_member_bodies(tmp_path: StdPath) -> None:
    """INVARIANT (load-bearing for affected coverage): editing only a method
    body changes BOTH the method's and the enclosing class's content_hash.

    If this ever stops holding (e.g. a "finer cache keys" change that hashes
    methods independently of their class), the container-granular coverage of
    inference-flow dependencies silently breaks — a §5.4 no-miss regression
    that no other test would catch.
    """
    (tmp_path / "m.py").write_text("class C:\n    def foo(self):\n        return 1\n")
    with TyO3Session(str(tmp_path)) as s:
        before = _name_to_hash(s.graph)
        s.edit(
            "m.py",
            "class C:\n    def foo(self):\n        return 2\n",  # body-only change
        )
        after = _name_to_hash(s.graph)

        assert before["m.py::foo"] != after["m.py::foo"], "method body change must move the method's content_hash"
        assert before["m.py::C"] != after["m.py::C"], (
            "INVARIANT BROKEN: the enclosing class's content_hash must move when "
            "a member body changes — affected-set coverage of inference-flow "
            "dependencies relies on this. See test docstring."
        )


def test_nominal_chain_carries_coverage(tmp_path: StdPath) -> None:
    """The named reference chain that carries container-granular coverage must
    exist: ``render -> make_widget`` and ``make_widget -> Widget``.

    With a maintained reverse_deps, this chain is what lets a change to
    ``Widget`` (bumped by a ``draw`` edit, see the invariant above) reach
    ``render`` even though no ``render -> draw`` edge exists.
    """
    (tmp_path / "widget.py").write_text(
        "class Widget:\n    def draw(self):\n        return 'drawing'\n\ndef make_widget():\n    return Widget()\n"
    )
    (tmp_path / "app.py").write_text(
        "from widget import make_widget\n\ndef render():\n    w = make_widget()\n    return w.draw()\n"
    )
    with TyO3Session(str(tmp_path)) as s:
        g = s.graph
        assert _edge_exists(
            g,
            src_name="render",
            src_file="app.py",
            dst_name="make_widget",
            dst_file="widget.py",
            kind=EdgeKind.REFERENCES,
        ), "named reference render -> make_widget must exist"
        assert _edge_exists(
            g,
            src_name="make_widget",
            src_file="widget.py",
            dst_name="Widget",
            dst_file="widget.py",
            kind=EdgeKind.REFERENCES,
        ), "named reference make_widget -> Widget must exist"


def test_inference_flow_edge_absent_canary(tmp_path: StdPath) -> None:
    """CANARY: today the producer emits NO edge for a member access resolved
    through an inferred type (``w.draw()`` where ``w: Widget``).

    This documents the known method-level coverage gap. If a future ty version
    starts resolving such accesses, this assertion fails — and that is a *good*
    failure: it means method-level precision became available for free and the
    container-granular coverage model should be re-evaluated (and method
    precision potentially enabled). Do not "fix" by relaxing — investigate.
    """
    (tmp_path / "widget.py").write_text(
        "class Widget:\n    def draw(self):\n        return 'drawing'\n\ndef make_widget():\n    return Widget()\n"
    )
    (tmp_path / "app.py").write_text(
        "from widget import make_widget\n\ndef render():\n    w = make_widget()\n    return w.draw()\n"
    )
    with TyO3Session(str(tmp_path)) as s:
        g = s.graph
        # No direct edge of ANY dependency kind from render to the method draw.
        for kind in (EdgeKind.REFERENCES, EdgeKind.INSTANTIATES, EdgeKind.TYPE_OF, EdgeKind.RETURNS):
            assert not _edge_exists(
                g,
                src_name="render",
                src_file="app.py",
                dst_name="draw",
                dst_file="widget.py",
                kind=kind,
            ), (
                f"inference-flow edge render -> draw ({kind}) is now emitted — "
                "ty behavior changed; revisit container-granular coverage and "
                "consider enabling method-level precision. See test docstring."
            )
