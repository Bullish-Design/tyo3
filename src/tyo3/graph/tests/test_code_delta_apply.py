"""Gate 3N Step 0 — pure CodeDelta applier.

Hand-build small ``CodeDelta`` payloads and assert the rustworkx replica updates
correctly, with no calls into any session/snapshot read surface.
"""

from __future__ import annotations

from tyo3.graph.graph import CodeGraph
from tyo3.models.analysis import (
    CodeDelta,
    EdgeDelta,
    Range,
    SymbolNodeDelta,
)


def _range(line: int = 1, col: int = 1) -> Range:
    return Range.model_validate(
        {"start": {"line": line, "column": col}, "end": {"line": line, "column": col}}
    )


def _module(file: str) -> SymbolNodeDelta:
    return SymbolNodeDelta(
        durable_id=f"<module>{file}",
        kind="module",
        qualified_name="<module>",
        file=file,
        range=_range(),
        content_hash=None,
    )


def test_apply_nodes_and_edges_round_trip() -> None:
    """Two nodes + a containment edge + a reference edge apply cleanly."""
    file = "pkg/mod.py"
    delta = CodeDelta(
        revision=1,
        rescan=True,
        nodes_upserted=[
            _module(file),
            SymbolNodeDelta(
                durable_id="01CLASS",
                kind="class_",
                qualified_name=f"{file}::User",
                file=file,
                range=_range(1, 1),
                content_hash="111",
            ),
            SymbolNodeDelta(
                durable_id="01METHOD",
                kind="method",
                qualified_name=f"{file}::User::save",
                file=file,
                range=_range(2, 5),
                content_hash="222",
            ),
        ],
        edges_added=[
            EdgeDelta(src_id=f"<module>{file}", dst_id="01CLASS", kind="containment"),
            EdgeDelta(src_id="01CLASS", dst_id="01METHOD", kind="containment"),
            EdgeDelta(src_id="01METHOD", dst_id="01CLASS", kind="references", role="read"),
        ],
    )

    g = CodeGraph()
    g.apply_code_delta(delta)

    assert g.node_count == 3
    assert g.edge_count == 3
    assert g.symbol("01CLASS") is not None
    # The method is contained by the class.
    assert [n.durable_id for n in g.children("01CLASS")] == ["01METHOD"]
    # The reference edge into the class is visible.
    assert len(g.references_to("01CLASS")) == 1
    assert g.revision == 1


def test_apply_moved_does_not_churn_edges() -> None:
    """A moved node changes its file/range payload but keeps its edges."""
    file = "a.py"
    g = CodeGraph()
    g.apply_code_delta(
        CodeDelta(
            revision=1,
            rescan=True,
            nodes_upserted=[
                _module(file),
                SymbolNodeDelta(
                    durable_id="01A",
                    kind="function",
                    qualified_name=f"{file}::a",
                    file=file,
                    range=_range(1, 1),
                    content_hash="aaa",
                ),
                SymbolNodeDelta(
                    durable_id="01B",
                    kind="function",
                    qualified_name=f"{file}::b",
                    file=file,
                    range=_range(5, 1),
                    content_hash="bbb",
                ),
            ],
            edges_added=[
                EdgeDelta(src_id="01A", dst_id="01B", kind="references", role="read"),
            ],
        )
    )
    before = g.references_from("01A")

    # Apply a move of the referencing node only.
    g.apply_code_delta(
        CodeDelta(
            revision=2,
            rescan=False,
            nodes_moved=[("01A", file, _range(10, 1))],
        )
    )

    moved = g.symbol("01A")
    assert moved is not None
    assert moved.range.start.line == 10
    # Edges identical — no churn.
    assert g.references_from("01A") == before
    assert g.revision == 2
