"""Phase 7: Path independence tests.

Verify that graph symbol IDs are portable and snapshot-friendly.
Graphs built from the same fixture in different absolute directories
must produce identical first-party symbol IDs.
"""

from __future__ import annotations

import shutil
from pathlib import Path as StdPath

from tyo3.graph import CodeGraph
from tyo3.models.symbols import SymbolKind
from tyo3.session import TyO3Session
from tyo3.tests.conftest import needs_native

FIXTURES_DIR = StdPath(__file__).parent.parent.parent.parent / "fixtures"


@needs_native
def test_graph_ids_are_path_independent(tmp_path: StdPath) -> None:
    """Same fixture in two absolute directories produces identical IDs."""
    # Build from the original fixture location
    original_fixture = FIXTURES_DIR / "graph_test"
    with TyO3Session(original_fixture) as session:
        graph_original = CodeGraph.build(session)

    # Copy to a temporary directory and build again
    temp_fixture = tmp_path / "graph_test"
    shutil.copytree(original_fixture, temp_fixture)
    with TyO3Session(temp_fixture) as session:
        graph_temp = CodeGraph.build(session)

    # Collect first-party (non-external) symbol IDs from both graphs
    def first_party_ids(code_graph: CodeGraph) -> set[str]:
        ids: set[str] = set()
        for idx in code_graph.graph.node_indices():
            node = code_graph.graph[idx]
            if not node.external:
                ids.add(node.symbol_id)
        return ids

    original_ids = first_party_ids(graph_original)
    temp_ids = first_party_ids(graph_temp)

    assert original_ids == temp_ids, (
        "First-party symbol IDs must be path-independent. "
        "Differences:\n"
        f"  Only in original: {original_ids - temp_ids}\n"
        f"  Only in temp:     {temp_ids - original_ids}"
    )


@needs_native
def test_graph_ids_do_not_contain_absolute_paths(tmp_path: StdPath) -> None:
    """First-party symbol IDs must not contain absolute paths."""
    fixture = FIXTURES_DIR / "graph_test"
    with TyO3Session(fixture) as session:
        graph = CodeGraph.build(session)

    for idx in graph.graph.node_indices():
        node = graph.graph[idx]
        if node.external:
            continue
        assert not node.symbol_id.startswith("/"), (
            f"First-party symbol ID should be project-relative, "
            f"got: {node.symbol_id}"
        )


@needs_native
def test_external_symbols_remain_explicitly_external(
    tmp_path: StdPath,
) -> None:
    """External symbols must use package-name prefix, not absolute paths."""
    fixture = FIXTURES_DIR / "graph_test"
    with TyO3Session(fixture) as session:
        graph = CodeGraph.build(session)

    external_nodes = graph.external_symbols()
    for node in external_nodes:
        assert node.external is True
        assert node.package is not None, (
            f"External node {node.symbol_id} must have a package name"
        )
        # External nodes should not contain absolute paths
        assert "<external>" in node.file or not node.file.startswith("/"), (
            f"External node {node.symbol_id} file should be '<external>' "
            f"or a package name, got: {node.file}"
        )
