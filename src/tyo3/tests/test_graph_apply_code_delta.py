"""Focused unit tests for the pure ``CodeGraph.apply_code_delta`` applier (Phase 4.1).

The applier is a **pure** function of ``(graph, code_delta)`` — no FFI, no
session, no snapshot — so these tests drive it with hand-built ``CodeDeltaDto``
dicts and assert structural outcomes directly. They pin the four properties the
Phase 4 guide §4.1 calls out:

* a full delta then an incremental ``nodes_upserted`` of one changed node leaves
  the rest of the graph identical;
* a ``nodes_moved`` entry changes only the node's file/range and leaves its
  edges intact (same DurableId, no edge churn — §5.5);
* an ``edges_removed`` for an IMPORTS edge prunes ``_file_importers``;
* a ``rescan=true`` delta replaces the whole graph.
"""

from __future__ import annotations

from tyo3.graph.projection import CodeGraph


def _rng(sl: int, sc: int, el: int, ec: int) -> dict:
    return {"start": {"line": sl, "column": sc}, "end": {"line": el, "column": ec}}


def _node(
    did: str,
    name: str,
    file: str,
    *,
    kind: str = "function",
    qn: str | None = None,
    rng: dict | None = None,
    content_hash: str | None = "abc",
) -> dict:
    return {
        "durable_id": did,
        "name": name,
        "qualified_name": qn or name,
        "kind": kind,
        "file": file,
        "range": rng or _rng(1, 1, 2, 1),
        "name_range": _rng(1, 5, 1, 5 + len(name)),
        "content_hash": content_hash,
        "content_hashes": {},
        "external": False,
        "package": None,
    }


def _edge(
    src: str, dst: str, kind: str, *, file: str | None = None, rng: dict | None = None, role: str | None = None
) -> dict:
    return {
        "source_id": src,
        "destination_id": dst,
        "kind": kind,
        "role": role,
        "file": file,
        "range": rng,
    }


def _full_delta(nodes: list[dict], edges: list[dict], revision: int = 1) -> dict:
    return {
        "revision": revision,
        "rescan": True,
        "nodes_upserted": nodes,
        "nodes_removed": [],
        "nodes_moved": [],
        "edges_added": edges,
        "edges_removed": [],
    }


# Two project files: models.py (module + class User + method save), main.py
# (module + function run). main imports models; run references User.
_MOD_MODELS = "<module>models.py"
_MOD_MAIN = "<module>main.py"
_USER = "01USER"
_SAVE = "01SAVE"
_RUN = "01RUN"


def _seed_graph() -> CodeGraph:
    nodes = [
        _node(_MOD_MODELS, "models", "models.py", kind="module", qn="<module>", content_hash=None),
        _node(_USER, "User", "models.py", kind="class_"),
        _node(_SAVE, "save", "models.py", kind="method", qn="User.save"),
        _node(_MOD_MAIN, "main", "main.py", kind="module", qn="<module>", content_hash=None),
        _node(_RUN, "run", "main.py", kind="function"),
    ]
    edges = [
        _edge(_MOD_MODELS, _USER, "defines"),
        _edge(_USER, _SAVE, "contains"),
        _edge(_MOD_MAIN, _RUN, "defines"),
        _edge(_MOD_MAIN, _MOD_MODELS, "imports", file="main.py"),
        _edge(_RUN, _USER, "references", file="main.py", rng=_rng(4, 11, 4, 15), role="read"),
    ]
    g = CodeGraph()
    g.apply_code_delta(_full_delta(nodes, edges))
    return g


def test_full_delta_builds_expected_graph():
    g = _seed_graph()
    assert g.node_count == 5
    assert {n.durable_id for n in g.symbols_in_file("models.py")} == {_MOD_MODELS, _USER, _SAVE}
    assert g.revision == 1
    # reverse-dep index: main.py imports models.py
    assert g._file_importers["models.py"] == {"main.py"}


def test_incremental_upsert_leaves_rest_identical():
    g = _seed_graph()
    before = {n.durable_id: n.model_dump(mode="json") for n in [g._graph[i] for i in g._graph.node_indices()]}

    # Re-emit only `save` with a new content hash (a body edit).
    changed = _node(_SAVE, "save", "models.py", kind="method", qn="User.save", content_hash="deadbeef")
    g.apply_code_delta(
        {
            "revision": 2,
            "rescan": False,
            "nodes_upserted": [changed],
            "nodes_removed": [],
            "nodes_moved": [],
            "edges_added": [],
            "edges_removed": [],
        }
    )

    after = {n.durable_id: n.model_dump(mode="json") for n in [g._graph[i] for i in g._graph.node_indices()]}

    assert after[_SAVE]["content_hash"] == "deadbeef"
    # Every other node is byte-identical.
    for did in before:
        if did != _SAVE:
            assert after[did] == before[did], f"node {did} changed unexpectedly"
    assert g.revision == 2
    # Edges untouched: User still contains save.
    assert any(n.durable_id == _SAVE for n in g.children(_USER))


def test_stale_incremental_delta_is_noop():
    g = _seed_graph()  # revision 1
    changed = _node(_SAVE, "save", "models.py", kind="method", qn="User.save", content_hash="STALE")
    # revision 1 <= current 1 → stale, must be ignored.
    g.apply_code_delta(
        {
            "revision": 1,
            "rescan": False,
            "nodes_upserted": [changed],
            "nodes_removed": [],
            "nodes_moved": [],
            "edges_added": [],
            "edges_removed": [],
        }
    )
    assert g.symbol(_SAVE).content_hash == "abc"
    assert g.revision == 1


def test_moved_node_updates_location_only_and_keeps_edges():
    g = _seed_graph()
    save_edges_before = g.children(_USER)
    assert any(n.durable_id == _SAVE for n in save_edges_before)

    # Move `run` to a new file/range (same id, unchanged body).
    g.apply_code_delta(
        {
            "revision": 2,
            "rescan": False,
            "nodes_upserted": [],
            "nodes_removed": [],
            "nodes_moved": [
                {
                    "durable_id": _RUN,
                    "file": "app.py",
                    "range": _rng(10, 1, 12, 1),
                    "name_range": _rng(10, 5, 10, 8),
                }
            ],
            "edges_added": [],
            "edges_removed": [],
        }
    )

    run = g.symbol(_RUN)
    assert run.file == "app.py"
    assert run.range.start.line == 10
    # The reference edge run -> User survives the move (no edge churn).
    refs = {tgt.durable_id for tgt, _ in g.references_from(_RUN)}
    assert _USER in refs
    # File index moved.
    assert _RUN in {n.durable_id for n in g.symbols_in_file("app.py")}
    assert _RUN not in {n.durable_id for n in g.symbols_in_file("main.py")}


def test_edges_removed_prunes_file_importers():
    g = _seed_graph()
    assert g._file_importers["models.py"] == {"main.py"}

    g.apply_code_delta(
        {
            "revision": 2,
            "rescan": False,
            "nodes_upserted": [],
            "nodes_removed": [],
            "nodes_moved": [],
            "edges_added": [],
            "edges_removed": [_edge(_MOD_MAIN, _MOD_MODELS, "imports", file="main.py")],
        }
    )

    assert "main.py" not in g._file_importers.get("models.py", set())
    assert g._importers_of({"models.py"}) == set()


def test_rescan_replaces_whole_graph():
    g = _seed_graph()
    assert g.node_count == 5

    # A fresh rescan with a completely different node set.
    nodes = [
        _node(_MOD_MAIN, "main", "main.py", kind="module", qn="<module>", content_hash=None),
        _node("01NEW", "brand_new", "main.py", kind="function"),
    ]
    edges = [_edge(_MOD_MAIN, "01NEW", "defines")]
    g.apply_code_delta(_full_delta(nodes, edges, revision=7))

    assert g.node_count == 2
    assert g.symbol(_USER) is None
    assert g.symbol("01NEW") is not None
    assert g.revision == 7
    # Stale index from the old graph is gone.
    assert g.symbols_in_file("models.py") == []
