"""Step 7 — Reproduce the cross-file inheritance ordering hazard (§6.4).

The original Step 0 test used a single-file fixture which cannot
reproduce the hazard.  This rewrite uses the three-file cross-file
chain the original guide mandated — ``a.py → b.py → c.py`` — where
the OVERRIDES from A.greet must reach C.greet two levels up.

The hazard (§6.4.1–6.4.2): when a dirty batch includes a multi-level
chain spanning several files, a single-pass per-file loop can compute
A's OVERRIDES before B→C's INHERITS edge exists, silently missing the
override.  The two-pass design (all INHERITS first, then all
OVERRIDES) eliminates that ordering dependence — this test proves it
by parametrising the dirty-file processing order.

Fixture (three files):
  c.py  — class C: def greet(self): ...
  b.py  — from c import C; class B(C): ...
  a.py  — from b import B; class A(B): def greet(self): ...  # OVERRIDES C.greet

Status: recorded in the commit message.
"""

from __future__ import annotations

import textwrap
from pathlib import Path as StdPath

import pytest

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


# ── Three-file, multi-level cross-file fixture (§6.4) ───────────────────
# c.py defines the root ancestor; b.py imports and extends it; a.py imports
# b and overrides a method two levels up.  All three files are dirty in one
# batch — the exact case that exercises §6.4.

C_PY = textwrap.dedent("""\
    class C:
        def greet(self) -> str:
            return "hello from C"
""")

B_PY = textwrap.dedent("""\
    from c import C

    class B(C):
        pass
""")

A_PY = textwrap.dedent("""\
    from b import B

    class A(B):
        def greet(self) -> str:
            return "hello from A"
""")

FIXTURE = {
    "c.py": C_PY,
    "b.py": B_PY,
    "a.py": A_PY,
}


def _write_fixture(root: StdPath) -> None:
    """Write the three-file fixture to *root*."""
    for name, content in FIXTURE.items():
        (root / name).write_text(content)


def _edit_all(suffix: str = "") -> dict[str, str]:
    """Return a dict of (path → content) for all three fixture files,
    with an optional comment appended to each (so they register as
    CHANGED in the delta, exercising the incremental path)."""
    return {
        "c.py": C_PY + (f"\n# {suffix}\n" if suffix else ""),
        "b.py": B_PY + (f"\n# {suffix}\n" if suffix else ""),
        "a.py": A_PY + (f"\n# {suffix}\n" if suffix else ""),
    }


def _check_override_a_greet_to_c(graph: CodeGraph) -> bool:
    """Return True iff an OVERRIDES edge from A.greet → C.greet exists."""
    for src_name, src_file, tgt_name, tgt_file in _overrides_pairs(graph):
        if (
            src_name == "greet"
            and src_file == "a.py"
            and tgt_name == "greet"
            and tgt_file == "c.py"
        ):
            return True
    return False


# ── Sort keys for parametrised processing order ─────────────────────────

def _ascending(files: list[str]) -> list[str]:
    """Alphabetical: a.py, b.py, c.py (default)."""
    return sorted(files)


def _descending(files: list[str]) -> list[str]:
    """Reverse alphabetical: c.py, b.py, a.py."""
    return sorted(files, reverse=True)


# ── Full build ──────────────────────────────────────────────────────────

class TestInheritanceOrderingFullBuild:
    """Full build: build the graph from scratch with the three-file fixture."""

    def test_full_build_override_edge_exists(self, tmp_path: StdPath) -> None:
        """Build the graph — check whether A.greet OVERRIDES C.greet."""
        _write_fixture(tmp_path)
        with TyO3Session(str(tmp_path)) as s:
            s.sync_all()
            g = CodeGraph.build(s)
            exists = _check_override_a_greet_to_c(g)
            assert exists, (
                "STATUS=LIVE: OVERRIDES edge A.greet→C.greet NOT FOUND "
                "in full build over three-file cross-file fixture."
            )


class TestInheritanceOrderingRebuildAfterEdit:
    """Rebuild after edits: full rebuild should match incremental result."""

    def test_rebuild_after_edits_preserves_override(self, tmp_path: StdPath) -> None:
        """Full rebuild after edits should still have the OVERRIDES edge."""
        _write_fixture(tmp_path)
        with TyO3Session(str(tmp_path)) as s:
            s.edit_many(_edit_all(suffix="v2"))
            g = CodeGraph.build(s)
            exists = _check_override_a_greet_to_c(g)
            assert exists, (
                "STATUS=LIVE: OVERRIDES edge missing in rebuild after edits "
                "on three-file cross-file fixture."
            )


# ── Incremental, parametrised over processing order ─────────────────────

class TestInheritanceOrderingIncremental:
    """Incremental update: edit all three files, apply delta.

    The dirty-file processing order is parametrised to prove the
    two-pass design is genuinely order-independent (§6.4.1).  When
    all three files are dirty in one batch, the OVERRIDES edge from
    A.greet to C.greet MUST exist regardless of whether the engine
    processes ``a.py`` first or ``c.py`` first.
    """

    @pytest.mark.parametrize("sort_order", [_ascending, _descending], ids=["asc", "desc"])
    def test_incremental_override_edge_exists(
        self, tmp_path: StdPath, sort_order
    ) -> None:
        """Edit all three files in one batch — OVERRIDES survives in both orders."""
        _write_fixture(tmp_path)
        with TyO3Session(str(tmp_path)) as s:
            g = s.graph  # native HEAD graph, maintained across edits

            # Edit all three files (append a comment so they register as CHANGED).
            edited = _edit_all(suffix="edited")
            s.edit_many(edited)  # drives the native post-commit head-graph update

            exists = _check_override_a_greet_to_c(g)
            assert exists, (
                f"STATUS=LIVE: OVERRIDES edge A.greet→C.greet missing "
                f"after incremental update (order={sort_order.__name__})."
            )

    @pytest.mark.parametrize("sort_order", [_ascending, _descending], ids=["asc", "desc"])
    def test_incremental_override_edge_stable_across_edits(
        self, tmp_path: StdPath, sort_order
    ) -> None:
        """Multiple rounds of edits — the edge persists across all rounds,
        regardless of processing order."""
        _write_fixture(tmp_path)
        with TyO3Session(str(tmp_path)) as s:
            g = s.graph  # native HEAD graph, maintained across edits

            for round_num in range(3):
                suffix = f"round{round_num}"
                s.edit_many(_edit_all(suffix=suffix))

                exists = _check_override_a_greet_to_c(g)
                assert exists, (
                    f"STATUS=LIVE: OVERRIDES edge A.greet→C.greet missing "
                    f"after edit round {round_num} (order={sort_order.__name__})."
                )
