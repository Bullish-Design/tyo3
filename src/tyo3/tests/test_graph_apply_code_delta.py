"""Focused unit tests for the pure ``CodeGraph.apply_code_delta`` applier.

The applier is a **pure** function of ``(graph, code_delta)`` — no FFI, no
session, no snapshot — so these tests drive it with hand-built ``CodeDeltaDto``
dicts and assert structural outcomes directly.

After Project 31 (#1) the applier only ever receives a *full* delta (the
``rescan`` shape ``full_code_delta()`` emits): every graph is built on demand, so
the incremental machinery (revision-gating, node/edge removals, in-place re-emit,
moves) was retired. These tests pin the two properties that remain:

* a full delta builds the expected graph + secondary indexes (``_file_importers``);
* a second full delta replaces the whole graph wholesale.
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
