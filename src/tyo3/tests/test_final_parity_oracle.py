"""Self-tests for the parity oracle harness (Phase 0.8).

The parity oracle is the safety net for the high-risk Phase 2–4 cutover that
moves code-graph production into Rust.  Before it can guard anything, the
*comparator* itself must be trustworthy.  These tests exercise the comparator
directly and, crucially, pin its **tiering**:

- a structural difference (node set, a structural node payload field, or the
  edge relation set) is always a hard ``AssertionError``;
- a cosmetic difference (an incidental node field, or an edge occurrence-range /
  role / multiplicity) is downgradeable: surfaced under ``cosmetic="warn"`` /
  silent under ``"ignore"``, but a hard failure under ``cosmetic="strict"``.

They also pin the not-yet-ready native-delta half so it turns green at the right
phase:

- ``test_assert_parity_native_half_not_ready_yet`` (→ Phase 2 / Phase 4): the
  native code delta does not exist yet, so :func:`assert_parity` raises
  ``ParityOracleNotReady``.  Remove the xfail once Phase 4 wires the applier.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from tyo3 import TyO3Session
from tyo3.tests.parity_oracle import (
    ParityOracleNotReady,
    assert_graphs_equal,
    assert_parity,
    compare_graphs,
    edge_multiset,
    graphs_equal,
    graphs_structurally_equal,
    legacy_graph,
    node_payloads,
)


def _write(root: Path, files: dict[str, str]) -> None:
    (root / "pyproject.toml").write_text("[project]\nname = 'parity'\nversion = '0.1.0'\n")
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


_PROJECT_A = {
    "models.py": (
        "class Base:\n"
        "    def save(self):\n"
        "        return 1\n"
        "\n"
        "class User(Base):\n"
        "    def save(self):\n"
        "        return 2\n"
    ),
    "main.py": (
        "from models import User\n"
        "\n"
        "def run():\n"
        "    return User().save()\n"
    ),
}

_PROJECT_B = {
    "lib.py": (
        "def helper(x):\n"
        "    return x + 1\n"
    ),
}


def _unique_relation_edge_index(graph):
    """Return an edge index whose ``(source, target, kind)`` relation is unique
    in the graph (so removing it changes the *structural* relation set), or
    ``None`` if there is no such edge."""
    g = graph._graph
    rel_counts: dict[tuple, int] = {}
    per_edge: dict[int, tuple] = {}
    for ei in g.edge_indices():
        data = g.get_edge_data_by_index(ei)
        s, t = g.get_edge_endpoints_by_index(ei)
        rel = (g[s].durable_id, g[t].durable_id, getattr(data, "kind", None))
        rel_counts[rel] = rel_counts.get(rel, 0) + 1
        per_edge[ei] = rel
    for ei, rel in per_edge.items():
        if rel_counts[rel] == 1:
            return ei
    return None


def _role_bearing_edge_index(graph):
    """Return an edge index whose payload carries a non-``None`` ``role`` (a
    reference/import edge), or ``None``."""
    g = graph._graph
    for ei in g.edge_indices():
        data = g.get_edge_data_by_index(ei)
        if getattr(data, "role", None) is not None:
            return ei
    return None


# ── structural tier (always fatal) ──────────────────────────────────────────


def test_comparator_accepts_identical_legacy_builds(tmp_path):
    """Two legacy builds of the same project must compare fully equal."""
    _write(tmp_path, _PROJECT_A)
    with TyO3Session(str(tmp_path)) as session:
        session.sync_all()
        g1 = legacy_graph(session)
        g2 = legacy_graph(session)
        # Sanity: the build is non-trivial.
        assert g1.node_count > 0
        assert_graphs_equal(g1, g2, cosmetic="strict")
        assert graphs_equal(g1, g2)
        assert graphs_structurally_equal(g1, g2)


def test_comparator_rejects_different_node_sets(tmp_path):
    """Graphs from different projects differ in node set → structural reject."""
    proj_a = tmp_path / "a"
    proj_b = tmp_path / "b"
    proj_a.mkdir()
    proj_b.mkdir()
    _write(proj_a, _PROJECT_A)
    _write(proj_b, _PROJECT_B)
    with TyO3Session(str(proj_a)) as sa, TyO3Session(str(proj_b)) as sb:
        sa.sync_all()
        sb.sync_all()
        ga = legacy_graph(sa)
        gb = legacy_graph(sb)
        assert not graphs_structurally_equal(ga, gb)
        with pytest.raises(AssertionError, match="node set"):
            assert_graphs_equal(ga, gb)


def test_comparator_rejects_structural_node_field_difference(tmp_path):
    """A mutated *structural* node field (content_hash) is a hard failure even
    in the default (cosmetic-tolerant) mode."""
    _write(tmp_path, _PROJECT_A)
    with TyO3Session(str(tmp_path)) as session:
        session.sync_all()
        g1 = legacy_graph(session)
        g2 = legacy_graph(session)
        gg = g2._graph
        idx = next(iter(gg.node_indices()))
        node = gg[idx]
        gg[idx] = node.model_copy(update={"content_hash": "DELIBERATELY-WRONG"})
        with pytest.raises(AssertionError, match="payload differs"):
            assert_graphs_equal(g1, g2)  # default cosmetic="warn"
        assert not graphs_structurally_equal(g1, g2)


def test_comparator_rejects_structural_edge_removal(tmp_path):
    """Removing an edge whose relation is unique changes the structural edge
    relation set → hard failure."""
    _write(tmp_path, _PROJECT_A)
    with TyO3Session(str(tmp_path)) as session:
        session.sync_all()
        g1 = legacy_graph(session)
        g2 = legacy_graph(session)
        ei = _unique_relation_edge_index(g2)
        if ei is None:
            pytest.skip("fixture produced no unique-relation edge to remove")
        g2._graph.remove_edge_from_index(ei)
        with pytest.raises(AssertionError, match="edge set"):
            assert_graphs_equal(g1, g2)
        assert not graphs_structurally_equal(g1, g2)


# ── cosmetic tier (downgradeable) ───────────────────────────────────────────


def test_cosmetic_node_field_difference_is_downgradeable(tmp_path):
    """A mutated *cosmetic* node field (package) is surfaced, not fatal, under
    the default mode — but fatal under cosmetic='strict'."""
    _write(tmp_path, _PROJECT_A)
    with TyO3Session(str(tmp_path)) as session:
        session.sync_all()
        g1 = legacy_graph(session)
        g2 = legacy_graph(session)
        gg = g2._graph
        idx = next(iter(gg.node_indices()))
        node = gg[idx]
        gg[idx] = node.model_copy(update={"package": "DELIBERATELY-WRONG", "content_hashes": {"x": "y"}})

        # Structurally still equal: only cosmetic fields changed.
        assert graphs_structurally_equal(g1, g2)
        report = compare_graphs(g1, g2)
        assert report.structurally_equal
        assert not report.fully_equal
        assert any("cosmetic payload differs" in p for p in report.cosmetic_problems)

        # Default ("warn") and "ignore" do not raise; "strict" does.
        assert_graphs_equal(g1, g2)  # cosmetic="warn"
        assert_graphs_equal(g1, g2, cosmetic="ignore")
        with pytest.raises(AssertionError, match="cosmetic"):
            assert_graphs_equal(g1, g2, cosmetic="strict")


def test_cosmetic_edge_difference_is_downgradeable(tmp_path):
    """Mutating a reference edge's role (a cosmetic field) keeps the relation
    set intact, so it is downgradeable, not a structural failure."""
    _write(tmp_path, _PROJECT_A)
    with TyO3Session(str(tmp_path)) as session:
        session.sync_all()
        g1 = legacy_graph(session)
        g2 = legacy_graph(session)
        ei = _role_bearing_edge_index(g2)
        if ei is None:
            pytest.skip("fixture produced no role-bearing (reference) edge")
        gg = g2._graph
        data = gg.get_edge_data_by_index(ei)
        # Same source/target/kind (relation preserved); only role changes.
        gg.update_edge_by_index(ei, dataclasses.replace(data, role=None))

        assert graphs_structurally_equal(g1, g2)
        report = compare_graphs(g1, g2)
        assert report.structurally_equal
        assert report.cosmetic_problems  # the multiset shift is reported here
        assert_graphs_equal(g1, g2)  # warn → no raise
        with pytest.raises(AssertionError, match="cosmetic"):
            assert_graphs_equal(g1, g2, cosmetic="strict")


def test_projections_are_well_formed(tmp_path):
    """node_payloads keys by durable_id; edge_multiset values are positive."""
    _write(tmp_path, _PROJECT_A)
    with TyO3Session(str(tmp_path)) as session:
        session.sync_all()
        g = legacy_graph(session)
        nodes = node_payloads(g)
        assert all(isinstance(k, str) and isinstance(v, dict) for k, v in nodes.items())
        edges = edge_multiset(g)
        assert all(count >= 1 for count in edges.values())


def test_assert_parity_native_half_matches_legacy(tmp_path):
    """assert_parity passes now that the native code delta + applier exist (P2).

    The native ``full_code_delta()`` + the pure ``CodeGraph.apply_code_delta``
    build a graph that matches the legacy read-surface build structurally (and,
    for this fixture, cosmetically too). If the native half regresses to absent,
    this fails on ``ParityOracleNotReady`` instead of silently xfailing.
    """
    _write(tmp_path, _PROJECT_A)
    with TyO3Session(str(tmp_path)) as session:
        session.sync_all()
        try:
            assert_parity(session)
        except ParityOracleNotReady:
            pytest.fail("native code-delta path is not implemented yet")
