"""Parity oracle harness (Phase 0.8 of the spine refactor).

The high-risk core of the refactor is moving code-graph production out of the
Python read-surface build (``CodeGraph.build``) and into a native Rust code
delta that Python merely *applies*.  The technique that makes that safe is to
run *both* implementations side by side and compare the graphs they produce.

The comparison is **tiered**, not flatly byte-for-byte:

* **Structural tier (strict).**  The load-bearing facts a query consumer reads:
  the node set keyed by ``durable_id``; the per-node structural fields
  (``durable_id, name, qualified_name, kind, file, range, content_hash,
  external`` — see :data:`STRUCTURAL_NODE_FIELDS`); and the *relation* set of
  edges, i.e. the deduplicated ``(source_id, target_id, kind)`` triples
  (containment / references / imports / inherits / overrides).  A structural
  difference is **always a hard failure** — the native producer is wrong, and
  the cutover (Phase 4) must not proceed past it.

* **Cosmetic tier (downgradeable).**  Incidental payload that no graph query
  demonstrably reads: per-node ``selection_range``, ``content_hashes``,
  ``package`` (see :data:`COSMETIC_NODE_FIELDS`); and the full edge *multiset*
  — reference/import occurrence ranges, roles, originating files, and
  parallel-edge multiplicities.  These are reported but, by default, do **not**
  block the cutover.  ``cosmetic="strict"`` restores full byte-for-byte
  equality when you want it; ``cosmetic="ignore"`` silences the tier entirely.

Why tier it?  Demanding byte-equality on every reference-edge occurrence range
and every incidental node field forces the Rust producer to reproduce the old
Python builder's quirks bug-for-bug on fields nobody consumes — the single
hardest, lowest-payoff part of the cutover.  Keeping the structural tier strict
preserves the mechanical-check safety net where it matters; downgrading the
cosmetic tier keeps a known-acceptable divergence from blocking the whole
refactor.  The oracle's value is dominated by the *breadth* of project states
fed to it (diamonds, re-exports, decorators, overloads, moves, deletes), not by
the tightness of cosmetic fields — spend effort there.

Caveat the oracle does not hide: it proves ``native == legacy``, not
``native == correct``.  Both halves lean on the same analysis read surface for
resolution, so they can share a blind spot and agree while both being wrong.
It is a regression net pinned to legacy behaviour — exactly what a cutover
wants — not a correctness proof.

This module has these parts:

1. :func:`compare_graphs` — the pure, tiered comparator.  Given two
   :class:`~tyo3.graph.graph.CodeGraph` objects it returns a
   :class:`ParityReport` separating structural problems from cosmetic ones.
2. :func:`assert_graphs_equal` — asserts structural parity always, and cosmetic
   parity per the ``cosmetic`` mode.
3. :func:`legacy_graph` — builds a graph via the existing Python read-surface
   construction.  Authoritative until Phase 4.
4. :func:`native_delta_graph` / :func:`assert_parity` — build a fresh graph by
   applying the *native code delta* and compare it against the legacy build.
   The native code delta does not exist until Phase 2, so until then these
   raise :class:`ParityOracleNotReady`.

The comparator itself is exercised now by ``test_final_parity_oracle.py`` so
the safety net is trustworthy before it guards anything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from tyo3.graph.graph import CodeGraph
from tyo3.graph.models import SymbolNode

logger = logging.getLogger(__name__)

CosmeticMode = Literal["strict", "warn", "ignore"]

# Fields a graph query consumer demonstrably reads — compared strictly.
STRUCTURAL_NODE_FIELDS: tuple[str, ...] = (
    "durable_id",
    "name",
    "qualified_name",
    "kind",
    "file",
    "range",
    "content_hash",
    "external",
)

# Incidental payload no graph query demonstrably reads — compared in the
# downgradeable cosmetic tier.
COSMETIC_NODE_FIELDS: tuple[str, ...] = (
    "selection_range",
    "content_hashes",
    "package",
)


class ParityOracleNotReady(RuntimeError):
    """Raised when the native code-delta path the oracle compares against does
    not exist yet.

    Phase 2 introduces a native ``CodeDelta`` and a pure ``apply_code_delta``
    on :class:`CodeGraph`; until both are present, :func:`native_delta_graph`
    cannot produce its half of the comparison.  Phase 2–4 parity tests treat
    this as an expected (xfail) condition.
    """


# ── Graph projection: node set ──────────────────────────────────────────────


def node_payloads(graph: CodeGraph) -> dict[str, dict[str, Any]]:
    """Project a graph to ``{durable_id: full_payload_dict}``.

    Each payload is a fully-serialised :class:`SymbolNode` so every field is
    available; :func:`compare_graphs` then splits it into the structural and
    cosmetic tiers.
    """
    out: dict[str, dict[str, Any]] = {}
    g = graph._graph
    for idx in g.node_indices():
        node = g[idx]
        if isinstance(node, SymbolNode):
            payload = node.model_dump(mode="json")
            did = node.durable_id
        else:  # defensive: tolerate non-pydantic payloads
            payload = dict(getattr(node, "__dict__", {"repr": repr(node)}))
            did = getattr(node, "durable_id", repr(node))
        out[did] = payload
    return out


def _split_node_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a node payload into (structural, cosmetic) field subsets.

    Any field that is neither explicitly structural nor explicitly cosmetic is
    folded into the *structural* subset: an unclassified field is treated as
    load-bearing until someone deliberately classifies it, so nothing silently
    escapes the strict tier.
    """
    cosmetic = {k: payload.get(k) for k in COSMETIC_NODE_FIELDS if k in payload}
    structural = {k: v for k, v in payload.items() if k not in COSMETIC_NODE_FIELDS}
    return structural, cosmetic


# ── Graph projection: edge relation set + edge multiset ─────────────────────


def _range_key(range_obj: Any) -> tuple[int, int, int, int] | None:
    if range_obj is None:
        return None
    return (
        range_obj.start.line,
        range_obj.start.column,
        range_obj.end.line,
        range_obj.end.column,
    )


def edge_relation_set(graph: CodeGraph) -> set[tuple[Any, Any, Any]]:
    """Project a graph to the deduplicated set of ``(source, target, kind)``
    relations — the *structural* edge fact.

    This captures "is there a containment / reference / inherits / overrides
    relationship of this kind between these two nodes," independent of
    occurrence ranges, roles, or how many parallel edges carry it.  It is what
    the ``references_to`` / ``dependencies`` / cycle / reachability queries
    actually read.
    """
    out: set[tuple[Any, Any, Any]] = set()
    g = graph._graph
    for edge_idx in g.edge_indices():
        data = g.get_edge_data_by_index(edge_idx)
        src, tgt = g.get_edge_endpoints_by_index(edge_idx)
        out.add((g[src].durable_id, g[tgt].durable_id, getattr(data, "kind", None)))
    return out


def edge_multiset(graph: CodeGraph) -> dict[tuple[Any, ...], int]:
    """Project a graph to a multiset of fully-payloaded edges (the cosmetic
    tier).

    The key folds in the full edge payload (kind, originating file, occurrence
    range, role) plus the endpoint durable ids, so two edges differing in *any*
    payload field count as distinct.  The value is the multiplicity (parallel
    edges with identical payloads are common for references).
    """
    counts: dict[tuple[Any, ...], int] = {}
    g = graph._graph
    for edge_idx in g.edge_indices():
        data = g.get_edge_data_by_index(edge_idx)
        src, tgt = g.get_edge_endpoints_by_index(edge_idx)
        key = (
            g[src].durable_id,
            g[tgt].durable_id,
            getattr(data, "kind", None),
            getattr(data, "file", None),
            _range_key(getattr(data, "range", None)),
            getattr(data, "role", None),
        )
        counts[key] = counts.get(key, 0) + 1
    return counts


# ── The tiered comparator ───────────────────────────────────────────────────


@dataclass
class ParityReport:
    """The result of :func:`compare_graphs`, split by tier.

    ``structural_problems`` are always-fatal: a difference in the node set, a
    structural node payload field, or the edge relation set.  ``cosmetic_problems``
    are downgradeable: incidental node fields and edge occurrence-range / role /
    multiplicity differences.
    """

    structural_problems: list[str] = field(default_factory=list)
    cosmetic_problems: list[str] = field(default_factory=list)

    @property
    def structurally_equal(self) -> bool:
        return not self.structural_problems

    @property
    def fully_equal(self) -> bool:
        return not self.structural_problems and not self.cosmetic_problems

    def structural_message(self, label_expected: str, label_actual: str) -> str:
        return "\n".join(
            [f"structural parity mismatch ({label_expected} vs {label_actual}):", *self.structural_problems]
        )

    def cosmetic_message(self, label_expected: str, label_actual: str) -> str:
        return "\n".join(
            [f"cosmetic parity mismatch ({label_expected} vs {label_actual}):", *self.cosmetic_problems]
        )


def _format_node_set_diff(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    msgs: list[str] = []
    missing = sorted(expected.keys() - actual.keys())
    extra = sorted(actual.keys() - expected.keys())
    if missing:
        msgs.append(f"  nodes only in expected ({len(missing)}): {missing[:10]}")
    if extra:
        msgs.append(f"  nodes only in actual ({len(extra)}): {extra[:10]}")
    return msgs


def _format_payload_field_diff(
    expected: dict[str, dict[str, Any]],
    actual: dict[str, dict[str, Any]],
    *,
    descriptor: str,
) -> list[str]:
    msgs: list[str] = []
    for did in sorted(expected.keys() & actual.keys()):
        e, a = expected[did], actual[did]
        if e != a:
            fields = sorted(set(e) | set(a))
            diffs = [f"{f}: {e.get(f)!r} != {a.get(f)!r}" for f in fields if e.get(f) != a.get(f)]
            msgs.append(f"  node {did} {descriptor} differs: {'; '.join(diffs)}")
    return msgs


def compare_graphs(
    expected: CodeGraph,
    actual: CodeGraph,
) -> ParityReport:
    """Compare two graphs and return a tiered :class:`ParityReport`.

    Structural tier: node-set membership, per-node structural fields
    (:data:`STRUCTURAL_NODE_FIELDS` plus any unclassified field), and the edge
    relation set.  Cosmetic tier: per-node cosmetic fields
    (:data:`COSMETIC_NODE_FIELDS`) and the edge multiset (occurrence ranges,
    roles, files, parallel multiplicities) for relations present on both sides.
    """
    exp_full, act_full = node_payloads(expected), node_payloads(actual)

    exp_struct = {did: _split_node_payload(p)[0] for did, p in exp_full.items()}
    act_struct = {did: _split_node_payload(p)[0] for did, p in act_full.items()}
    exp_cos = {did: _split_node_payload(p)[1] for did, p in exp_full.items()}
    act_cos = {did: _split_node_payload(p)[1] for did, p in act_full.items()}

    report = ParityReport()

    # ── structural: node set membership ──
    if exp_struct.keys() != act_struct.keys():
        report.structural_problems.append("node set mismatch:")
        report.structural_problems.extend(_format_node_set_diff(exp_struct, act_struct))

    # ── structural: per-node structural payload ──
    report.structural_problems.extend(
        _format_payload_field_diff(exp_struct, act_struct, descriptor="payload")
    )

    # ── structural: edge relation set ──
    exp_rel, act_rel = edge_relation_set(expected), edge_relation_set(actual)
    if exp_rel != act_rel:
        only_e = sorted(exp_rel - act_rel, key=repr)
        only_a = sorted(act_rel - exp_rel, key=repr)
        report.structural_problems.append("edge set (relation) mismatch:")
        if only_e:
            report.structural_problems.append(f"  relations only in expected ({len(only_e)}): {only_e[:10]}")
        if only_a:
            report.structural_problems.append(f"  relations only in actual ({len(only_a)}): {only_a[:10]}")

    # ── cosmetic: per-node cosmetic payload ──
    report.cosmetic_problems.extend(
        _format_payload_field_diff(exp_cos, act_cos, descriptor="cosmetic payload")
    )

    # ── cosmetic: edge multiset, restricted to relations present on both sides ──
    exp_multi, act_multi = edge_multiset(expected), edge_multiset(actual)
    shared_rel = exp_rel & act_rel
    for key in sorted(exp_multi.keys() | act_multi.keys(), key=repr):
        relation = key[:3]
        if relation not in shared_rel:
            continue  # a relation-set difference is reported structurally, not here
        ec, ac = exp_multi.get(key, 0), act_multi.get(key, 0)
        if ec != ac:
            report.cosmetic_problems.append(f"  edge {key!r}: expected x{ec}, actual x{ac}")

    return report


def assert_graphs_equal(
    expected: CodeGraph,
    actual: CodeGraph,
    *,
    cosmetic: CosmeticMode = "warn",
    label_expected: str = "expected",
    label_actual: str = "actual",
) -> ParityReport:
    """Assert structural parity (always) and cosmetic parity (per ``cosmetic``).

    A structural difference — node set, a structural node payload field, or the
    edge relation set — **always** raises ``AssertionError``.  A cosmetic
    difference is handled per mode:

    * ``"strict"`` — raise (full byte-for-byte equality, the original contract).
    * ``"warn"`` (default) — log a warning and continue; the cutover is not
      blocked by a known-acceptable cosmetic divergence.
    * ``"ignore"`` — say nothing.

    Returns the :class:`ParityReport` so callers can inspect both tiers.
    """
    report = compare_graphs(expected, actual)

    if report.structural_problems:
        raise AssertionError(report.structural_message(label_expected, label_actual))

    if report.cosmetic_problems:
        if cosmetic == "strict":
            raise AssertionError(report.cosmetic_message(label_expected, label_actual))
        if cosmetic == "warn":
            logger.warning(report.cosmetic_message(label_expected, label_actual))
        # "ignore" → say nothing

    return report


def graphs_equal(expected: CodeGraph, actual: CodeGraph) -> bool:
    """Boolean form: ``True`` iff the graphs are *fully* equal (both tiers)."""
    return compare_graphs(expected, actual).fully_equal


def graphs_structurally_equal(expected: CodeGraph, actual: CodeGraph) -> bool:
    """Boolean form: ``True`` iff the graphs are *structurally* equal (the
    strict tier only; cosmetic differences are tolerated)."""
    return compare_graphs(expected, actual).structurally_equal


# ── The two halves the oracle compares ──────────────────────────────────────


def legacy_graph(source: Any, *, root: Path | None = None) -> CodeGraph:
    """Build a graph via the legacy Python read-surface construction.

    *source* is a :class:`~tyo3.session.TyO3Session` or
    :class:`~tyo3.session.Snapshot`.  Authoritative until the Phase 4 cutover.
    """
    return CodeGraph.build(source, root=root)


def native_delta_graph(source: Any, *, root: Path | None = None) -> CodeGraph:
    """Build a fresh graph by applying the native code delta.

    Until Phase 2 ships the native ``CodeDelta`` and Phase 4 ships the pure
    ``CodeGraph.apply_code_delta`` applier, the native half does not exist and
    this raises :class:`ParityOracleNotReady`.

    The expected end state (Phase 4): obtain a full ``code_delta`` for *source*
    at its current revision from the native side, create an empty
    :class:`CodeGraph`, and ``apply_code_delta`` it — with no FFI read-surface
    walk.  This function is the single seam Phase 2–4 fill in.
    """
    code_delta = _extract_native_code_delta(source)
    if code_delta is None:
        raise ParityOracleNotReady(
            "native code delta is not available yet — lands in Phase 2 "
            "(producer) and becomes applier-ready in Phase 4"
        )
    graph = CodeGraph()
    if root is not None:
        graph._root = root
    elif hasattr(source, "root"):
        graph._root = Path(source.root).resolve()
    # Phase 4: graph.apply_code_delta(code_delta)
    apply = getattr(graph, "apply_code_delta", None)
    if apply is None:
        raise ParityOracleNotReady(
            "CodeGraph.apply_code_delta does not exist yet — lands in Phase 4"
        )
    apply(code_delta)
    return graph


def _extract_native_code_delta(source: Any) -> Any | None:
    """Return a full native code delta for *source*, or ``None`` if the native
    side does not expose one yet.

    Phase 2 attaches a code delta to the commit result and provides a way to
    obtain a *full* (cold-start / ``rescan``) code delta for the current state;
    this probes for that surface without assuming its final name.
    """
    inner = getattr(source, "_inner", source)
    for attr in ("full_code_delta", "code_delta", "_code_delta"):
        producer = getattr(inner, attr, None)
        if producer is None:
            continue
        return producer() if callable(producer) else producer
    return None


def assert_parity(
    source: Any,
    *,
    cosmetic: CosmeticMode = "warn",
    root: Path | None = None,
) -> ParityReport:
    """Assert the native-delta graph matches the legacy read-surface graph.

    The single entry point Phase 2–4 tests call.  Raises
    :class:`ParityOracleNotReady` while the native half is absent (xfail).
    Once it exists, structural parity is asserted unconditionally and cosmetic
    parity per ``cosmetic`` (default ``"warn"`` — surfaced, not blocking).
    """
    legacy = legacy_graph(source, root=root)
    native = native_delta_graph(source, root=root)
    return assert_graphs_equal(
        legacy, native, cosmetic=cosmetic, label_expected="legacy", label_actual="native"
    )
