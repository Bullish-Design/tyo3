"""Step 0 — Reproduce (or refute) the inheritance ordering hazard.

Tests that the OVERRIDES edge is resolved correctly for a multi-level
chain regardless of processing order (§6.4).

Fixture: single-file three-level chain — C → B → A, where A overrides
C.greet. The Rust engine resolves supertypes within a file but not
cross-file, so all three classes are in models.py.

Current status: recorded in the commit message.
"""

from __future__ import annotations

import textwrap
from pathlib import Path as StdPath

from tyo3 import TyO3Session
from tyo3.graph import CodeGraph, EdgeKind


def _overrides_pairs(graph: CodeGraph) -> list[tuple[str, str, str, str]]:
    """Return (src_name, src_file, tgt_name, tgt_file) for OVERRIDES edges."""
    result: list[tuple[str, str, str, str]] = []
    for ei in graph.graph.edge_indices():
        data = graph.graph.get_edge_data_by_index(ei)
        if data.kind != EdgeKind.OVERRIDES:
            continue
        s, t = graph.graph.get_edge_endpoints_by_index(ei)
        src = graph.graph[s]
        tgt = graph.graph[t]
        if src and tgt:
            result.append((src.name, src.file, tgt.name, tgt.file))
    return result


# ── Fixture: three-level chain within a single file ─────────────────────
# The Rust engine resolves supertypes within a file but not across files,
# so all three classes are defined in one file to exercise the two-pass
# inheritance resolver.

FIXTURE = {
    "models.py": textwrap.dedent("""\
        class C:
            def greet(self) -> str:
                return "hello from C"


        class B(C):
            pass


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
    for src_name, src_file, tgt_name, tgt_file in _overrides_pairs(graph):
        if (src_name == "greet" and src_file == "models.py"
                and tgt_name == "greet" and tgt_file == "models.py"):
            return True
    return False


class TestInheritanceOrderingFullBuild:
    """Full build: build the graph from scratch with the fixture."""

    def test_full_build_override_edge_exists(self, tmp_path: StdPath) -> None:
        """Build the graph — check whether A.greet OVERRIDES C.greet."""
        _write_fixture(tmp_path)
        with TyO3Session(str(tmp_path)) as s:
            s.sync_all()  # populate identity registry for id_for()
            g = CodeGraph.build(s)
            exists = _check_override_exists(g)
            assert exists, (
                "STATUS=LIVE: OVERRIDES edge A.greet→C.greet NOT FOUND in full build."
            )


class TestInheritanceOrderingIncremental:
    """Incremental update: edit the file, apply delta."""

    def test_incremental_override_edge_exists(self, tmp_path: StdPath) -> None:
        """Edit the file — check OVERRIDES survives incremental update."""
        _write_fixture(tmp_path)
        with TyO3Session(str(tmp_path)) as s:
            g = CodeGraph.build(s)

            # Edit the file (add a comment).
            edited = {
                "models.py": FIXTURE["models.py"] + "\n# edited\n",
            }
            result = s.edit_many(edited)
            g.apply_delta(s, result)

            exists = _check_override_exists(g)
            assert exists, (
                "STATUS=LIVE: OVERRIDES edge missing after incremental update."
            )


class TestInheritanceOrderingRebuildAfterEdit:
    """Rebuild after edits: full rebuild should match incremental result."""

    def test_rebuild_after_edits_preserves_override(self, tmp_path: StdPath) -> None:
        """Full rebuild after edits should still have the OVERRIDES edge."""
        _write_fixture(tmp_path)
        with TyO3Session(str(tmp_path)) as s:
            s.edit_many({
                "models.py": FIXTURE["models.py"] + "\n# v2\n",
            })
            g = CodeGraph.build(s)
            exists = _check_override_exists(g)
            assert exists, (
                "STATUS=LIVE: OVERRIDES edge missing in rebuild after edits."
            )
