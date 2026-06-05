"""Step 0 — Reproduce (or refute) the inheritance ordering hazard.

Tests that the OVERRIDES edge is resolved correctly for a multi-level,
cross-file chain regardless of processing order.

§6.4: The single-pass resolver may miss OVERRIDES when the intermediate
ancestor's INHERITS edge doesn't exist yet. This test makes the hazard
observable and provides a permanent regression guard.

Fixture:
    c.py — class C (defines greet)
    b.py — class B(C) (intermediate, defines nothing)
    a.py — class A(B) (OVERRIDES C.greet, two levels up)

Current status after running: recorded in the commit message.
"""

from __future__ import annotations

import textwrap
from pathlib import Path as StdPath

from tyo3 import TyO3Session
from tyo3.graph import CodeGraph, EdgeKind
from tyo3.graph.models import SymbolNode
from tyo3.models.symbols import SymbolKind


def _edges_of_kind(graph: CodeGraph, kind: EdgeKind) -> list[tuple[str, str]]:
    """Return all (src_id, tgt_id) pairs for edges of *kind*."""
    result: list[tuple[str, str]] = []
    for ei in graph.graph.edge_indices():
        data = graph.graph.get_edge_data_by_index(ei)
        if data.kind != kind:
            continue
        s, t = graph.graph.get_edge_endpoints_by_index(ei)
        result.append((graph.graph[s].symbol_id, graph.graph[t].symbol_id))
    return result


def _overrides_pairs(graph: CodeGraph) -> list[tuple[str, str, str, str]]:
    """Return (src_name, src_file_suffix, tgt_name, tgt_file_suffix) for OVERRIDES edges."""
    result: list[tuple[str, str, str, str]] = []
    for src_id, tgt_id in _edges_of_kind(graph, EdgeKind.OVERRIDES):
        src = graph.symbol(src_id)
        tgt = graph.symbol(tgt_id)
        if src and tgt:
            result.append((src.name, src.file, tgt.name, tgt.file))
    return result


def _node_by_name_and_file(graph: CodeGraph, name: str, file_suffix: str) -> SymbolNode | None:
    """Find a non-external node matching name and file suffix."""
    for node in graph.symbols_of_kind(SymbolKind.METHOD):
        if node.name == name and node.file.endswith(file_suffix) and not node.external:
            return node
    return None


# ── Fixture: three-level chain, override on the top of the chain ──────────

FIXTURE = {
    "c.py": textwrap.dedent("""\
        class C:
            def greet(self) -> str:
                return "hello from C"
    """),
    "b.py": textwrap.dedent("""\
        from c import C

        class B(C):
            pass
    """),
    "a.py": textwrap.dedent("""\
        from b import B

        class A(B):
            def greet(self) -> str:
                return "hello from A"
    """),
}


def _write_fixture(root: StdPath) -> None:
    for name, content in FIXTURE.items():
        (root / name).write_text(content)


def _check_override_exists(graph: CodeGraph) -> bool:
    """Check that A.greet --OVERRIDES--> C.greet edge exists."""
    overrides = _overrides_pairs(graph)
    for src_name, src_file, tgt_name, tgt_file in overrides:
        if (src_name == "greet" and src_file.endswith("a.py")
                and tgt_name == "greet" and tgt_file.endswith("c.py")):
            return True
    return False


class TestInheritanceOrderingFullBuild:
    """Full build: build the graph from scratch with all three files."""

    def test_full_build_override_edge_exists(self, tmp_path: StdPath) -> None:
        """Build the graph — check whether A.greet OVERRIDES C.greet."""
        _write_fixture(tmp_path)
        with TyO3Session(str(tmp_path)) as s:
            g = CodeGraph.build(s)
            exists = _check_override_exists(g)
            # Record status: LIVE (bug — override missing) or LATENT (works today)
            assert exists, (
                "STATUS=LIVE: OVERRIDES edge A.greet→C.greet NOT FOUND in full build. "
                "The single-pass inheritance resolver misses multi-level cross-file overrides "
                "when the intermediate ancestor's INHERITS edge doesn't exist yet."
            )


class TestInheritanceOrderingIncremental:
    """Incremental update: edit all three files in one batch, apply delta."""

    def test_incremental_override_edge_exists(self, tmp_path: StdPath) -> None:
        """Edit all three files in one batch — check OVERRIDES survives."""
        _write_fixture(tmp_path)
        with TyO3Session(str(tmp_path)) as s:
            g = CodeGraph.build(s)

            # Edit all three files in one atomic batch (add a comment to each).
            edited = {
                "c.py": FIXTURE["c.py"] + "\n# edited\n",
                "b.py": FIXTURE["b.py"] + "\n# edited\n",
                "a.py": FIXTURE["a.py"] + "\n# edited\n",
            }
            result = s.edit_many(edited)
            with s.snapshot() as snap:
                g.apply_delta(snap, result)

            exists = _check_override_exists(g)
            assert exists, (
                "STATUS=LIVE: OVERRIDES edge A.greet→C.greet NOT FOUND after incremental update. "
                "The single-pass inheritance resolver misses multi-level cross-file overrides "
                "when dirty files are processed in an order where the intermediate ancestor's "
                "INHERITS edge doesn't exist yet during A's BFS walk."
            )


class TestInheritanceOrderingRebuildAfterEdit:
    """Rebuild after edits: full rebuild should match incremental result."""

    def test_rebuild_after_edits_preserves_override(self, tmp_path: StdPath) -> None:
        """Full rebuild after edits should still have the OVERRIDES edge."""
        _write_fixture(tmp_path)
        with TyO3Session(str(tmp_path)) as s:
            g = CodeGraph.build(s)

            # Edit all three, then rebuild.
            edited = {
                "c.py": FIXTURE["c.py"] + "\n# v2\n",
                "b.py": FIXTURE["b.py"] + "\n# v2\n",
                "a.py": FIXTURE["a.py"] + "\n# v2\n",
            }
            s.edit_many(edited)

            rebuilt = CodeGraph.build(s)
            exists = _check_override_exists(rebuilt)
            assert exists, (
                "STATUS=LIVE: OVERRIDES edge A.greet→C.greet NOT FOUND in rebuild after edits. "
                "The full rebuild over the same content should produce the override edge."
            )
