"""Handler tests — drive each RPC handler against a real ``TyO3Session``.

These prove the session-wrapping logic in isolation (no socket): ``open``,
``entity_at``, ``decorate``, ``sync_buffer`` deltas, ``author`` → ``authored``,
``derived``, ``diff``, ``locate``, and the ops methods, all against the shop
project from ``tyo3.demo.tour``.
"""

from __future__ import annotations

import pytest

from tyo3.daemon.protocol import INVALID_PARAMS, METHOD_NOT_FOUND, ProtocolError
from tyo3.daemon.tests.conftest import needs_native

pytestmark = needs_native


def _checkout_position(handlers) -> tuple[str, int, int]:
    """Resolve checkout's (file, line, col) from the decorate batch — robust to
    blank-line counting in the fixture."""
    deco = handlers.decorate({"path": "store.py"})
    entry = next(d for d in deco if d["name"] == "checkout")
    start = entry["range"]["start"]
    return "store.py", start["line"], start["column"]


# ── open ─────────────────────────────────────────────────────────────────────


def test_open_reports_session_and_layers(handlers):
    result = handlers.open({"root": handlers._root})
    assert result["revision"] >= 1
    assert result["session_id"]
    files = result["files"]
    assert any(f.endswith("store.py") for f in files)
    assert {"intent", "summary", "embed"} <= set(result["layers"])
    assert result["precision"] == "method"


# ── entity_at ────────────────────────────────────────────────────────────────


def test_entity_at_resolves_checkout(handlers):
    path, line, col = _checkout_position(handlers)
    card = handlers.entity_at({"path": path, "line": line, "col": col})
    assert card is not None
    assert card["qualified_name"] == "checkout"
    assert card["kind"] == "function"
    assert card["location"].endswith("checkout")
    assert "authored" in card and "derived" in card


def test_entity_at_off_entity_returns_null(handlers):
    # Column far past any token on a blank-ish position.
    card = handlers.entity_at({"path": "store.py", "line": 4, "col": 1})
    assert card is None


# ── QW4: one shared snapshot per card ─────────────────────────────────────────


def _count_snapshots(actor, monkeypatch):
    """Spy on the session's ``snapshot()`` and return a mutable call counter.

    Patches the live session instance (shared with the actor thread) so any
    explicit ``s.snapshot()`` in a handler is counted. Does not affect the
    cached head snapshot (``_native()`` uses the native handle directly)."""
    session = actor.submit(lambda s: s)
    real = session.snapshot
    calls = {"n": 0}

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(session, "snapshot", counting)
    return calls


def test_entity_at_card_is_multilayer_and_consistent(handlers, ids):
    """The card joins authored + derived layers — all read off one snapshot."""
    checkout_id = ids["checkout"]
    handlers.author({"layer": "intent", "durable_id": checkout_id, "value": {"note": "one snap"}})
    path, line, col = _checkout_position(handlers)
    card = handlers.entity_at({"path": path, "line": line, "col": col})
    assert card is not None
    assert card["authored"]["intent"]["value"] == {"note": "one snap"}
    assert card["derived"]["summary"]["status"] == "fresh"


def test_entity_at_opens_one_snapshot_per_card(handlers, ids, actor, monkeypatch):
    """A K-layer card opens exactly **one** snapshot, not one per layer (QW4)."""
    handlers.author({"layer": "intent", "durable_id": ids["checkout"], "value": {"note": "x"}})
    # Resolve the position *before* spying (decorate now opens a snapshot too).
    path, line, col = _checkout_position(handlers)
    calls = _count_snapshots(actor, monkeypatch)
    card = handlers.entity_at({"path": path, "line": line, "col": col})
    assert card is not None
    assert calls["n"] == 1, "the whole card reads off a single snapshot"


def test_decorate_opens_one_snapshot_for_the_file(handlers, actor, monkeypatch):
    """``decorate`` walks the file off one snapshot, not one per entity (QW4)."""
    calls = _count_snapshots(actor, monkeypatch)
    deco = handlers.decorate({"path": "store.py"})
    assert deco, "entities decorated"
    assert calls["n"] == 1, "one snapshot for the whole file walk"


# ── decorate ─────────────────────────────────────────────────────────────────


def test_decorate_lists_entities_in_order(handlers):
    deco = handlers.decorate({"path": "store.py"})
    names = [d["name"] for d in deco]
    assert "checkout" in names and "show_label" in names
    # Sorted by start line.
    lines = [d["range"]["start"]["line"] for d in deco]
    assert lines == sorted(lines)
    # No module / external nodes.
    assert all(d["kind"] != "module" for d in deco)


def test_decorate_surfaces_authored_note(handlers, ids):
    checkout_id = ids["checkout"]
    handlers.author({"layer": "intent", "durable_id": checkout_id, "value": {"note": "load-bearing"}})
    deco = handlers.decorate({"path": "store.py"})
    entry = next(d for d in deco if d["durable_id"] == checkout_id)
    assert entry["note"] == "load-bearing"


# ── author / authored ────────────────────────────────────────────────────────


def test_author_then_authored_round_trip(handlers, ids):
    checkout_id = ids["checkout"]
    res = handlers.author({"layer": "intent", "durable_id": checkout_id, "value": {"note": "hot path"}})
    assert res["revision"] >= 1
    got = handlers.authored({"layer": "intent", "durable_id": checkout_id})
    assert got["value"] == {"note": "hot path"}
    assert got["status"] in ("present", "needs_review")


# ── sync_buffer (the editor write path) ──────────────────────────────────────

CATALOG_PRICE_250 = (
    "class Item:\n"
    "    def price(self) -> int:\n"
    "        return 250\n"
    "\n"
    "    def label(self) -> str:\n"
    '        return "item"\n'
)


def test_sync_buffer_returns_id_level_delta(handlers, ids):
    price_id = ids["Item.price"]
    delta = handlers.sync_buffer({"path": "catalog.py", "text": CATALOG_PRICE_250})
    assert price_id in delta["changed_ids"], "the edited method is in changed_ids"
    affected = set(delta["affected_ids"])
    assert affected >= set(delta["changed_ids"]), "affected_ids is a superset of the seeds"
    # The importer (checkout, via Book().price()) and the subclass light up.
    assert ids["checkout"] in affected
    assert ids["Book"] in affected
    assert "catalog.py" in delta["touched_files"]


def test_sync_buffer_is_overlay_only(handlers, shop_project):
    before = (shop_project / "catalog.py").read_text()
    handlers.sync_buffer({"path": "catalog.py", "text": CATALOG_PRICE_250})
    after = (shop_project / "catalog.py").read_text()
    assert before == after, "sync_buffer overlays in memory — never writes disk"


# ── sync_buffers (atomic move — the money shot) ──────────────────────────────


def test_sync_buffers_move_preserves_id_and_note(handlers, ids):
    """A function moved to another file in one commit keeps its durable id, and
    an authored note rides along — the demo's headline behaviour.

    Uses the tour's designed move pair: ``legacy.py`` → the pre-existing empty
    ``legacy_moved.py``. The destination must already be a project file and the
    body byte-identical for a ``Moved`` (same-hash) bind."""
    legacy_id = ids["legacy_helper"]
    handlers.author({"layer": "intent", "durable_id": legacy_id, "value": {"note": "rides along"}})

    body = "def legacy_helper(x: int) -> int:\n    return x + 1\n"
    delta = handlers.sync_buffers({"edits": {"legacy.py": "", "legacy_moved.py": body}})
    moved_ids = {m["id"] for m in delta["moved"]}
    assert legacy_id in moved_ids, "legacy_helper bound as a Move (same id, new file)"

    loc = handlers.locate({"durable_id": legacy_id})
    assert "legacy_moved.py" in loc["location"]
    # The note survives the move.
    deco = handlers.decorate({"path": "legacy_moved.py"})
    entry = next(d for d in deco if d["durable_id"] == legacy_id)
    assert entry["note"] == "rides along"


def test_sync_buffers_rejects_bad_edits(handlers):
    with pytest.raises(ProtocolError) as e:
        handlers.sync_buffers({"edits": {}})
    assert e.value.code == INVALID_PARAMS


# ── diff ─────────────────────────────────────────────────────────────────────


def test_diff_reports_changed_entity(handlers, ids, actor):
    price_id = ids["Item.price"]
    from_rev = actor.submit(lambda s: s.head)
    handlers.sync_buffer({"path": "catalog.py", "text": CATALOG_PRICE_250})
    to_rev = actor.submit(lambda s: s.head)
    d = handlers.diff({"from_rev": from_rev, "to_rev": to_rev})
    assert d["before_revision"] == from_rev
    assert d["after_revision"] == to_rev
    assert price_id in d["changed"]


def test_diff_to_head_when_to_rev_absent(handlers, ids, actor):
    from_rev = actor.submit(lambda s: s.head)
    handlers.sync_buffer({"path": "catalog.py", "text": CATALOG_PRICE_250})
    d = handlers.diff({"from_rev": from_rev})
    assert ids["Item.price"] in d["changed"]


# ── derived ──────────────────────────────────────────────────────────────────


def test_derived_summary_is_fresh(handlers, ids):
    checkout_id = ids["checkout"]
    dv = handlers.derived({"layer": "summary", "durable_id": checkout_id})
    assert dv["status"] == "fresh"
    assert dv["artifact"] is not None
    assert dv["artifact"].startswith("summary<")


# ── locate ───────────────────────────────────────────────────────────────────


def test_locate_resolves_id(handlers, ids):
    res = handlers.locate({"durable_id": ids["checkout"]})
    assert res["location"].endswith("checkout")
    assert "store.py" in res["location"]


# ── ops: reindex / gc / check ────────────────────────────────────────────────


def test_reindex_returns_rescan_delta(handlers):
    delta = handlers.reindex({})
    assert delta["revision"] >= 1
    assert delta["rescan"] is True


def test_gc_is_ok(handlers):
    res = handlers.gc({})
    assert res["ok"] is True


def test_check_returns_diagnostics_shape(handlers):
    res = handlers.check({})
    assert "diagnostics" in res
    assert isinstance(res["diagnostics"], list)
    assert "count" in res


# ── layers / layer_ids (QW3 / QW7) ───────────────────────────────────────────


def test_layers_describes_each_declared_layer(handlers):
    """``layers`` reports every config layer with its origin, so the editor can
    discover authored (writable) vs derived layers without hardcoding names."""
    res = handlers.layers({})
    by_name = {lyr["name"]: lyr for lyr in res["layers"]}
    assert {"intent", "docs", "summary", "embed"} <= set(by_name)
    assert by_name["intent"]["origin"] == "authored"
    assert by_name["docs"]["origin"] == "authored"
    assert by_name["summary"]["origin"] == "derived"
    # Each entry carries the discovery fields the author menu / renderer use.
    intent = by_name["intent"]
    for key in ("entity_kinds", "history", "review_on_change", "display"):
        assert key in intent
    # summary applies to functions only.
    assert by_name["summary"]["entity_kinds"] == ["function"]


def test_layer_ids_returns_exactly_authored_records(handlers, ids):
    """``layer_ids('intent')`` returns precisely the ids with a record — replaces
    the per-entity ``authored`` loop with one snapshot, one actor hop."""
    assert handlers.layer_ids({"layer": "intent"})["ids"] == [], "no notes authored yet"
    checkout_id, label_id = ids["checkout"], ids["show_label"]
    handlers.author({"layer": "intent", "durable_id": checkout_id, "value": {"note": "hot"}})
    handlers.author({"layer": "intent", "durable_id": label_id, "value": {"note": "cold"}})
    res = handlers.layer_ids({"layer": "intent"})
    assert set(res["ids"]) == {checkout_id, label_id}
    assert res["layer"] == "intent"


def test_layer_ids_with_values_includes_records(handlers, ids):
    checkout_id = ids["checkout"]
    handlers.author({"layer": "intent", "durable_id": checkout_id, "value": {"note": "hot"}})
    res = handlers.layer_ids({"layer": "intent", "with_values": True})
    assert res["values"][checkout_id] == {"note": "hot"}


def test_layer_ids_rejects_bad_with_values(handlers):
    with pytest.raises(ProtocolError) as e:
        handlers.layer_ids({"layer": "intent", "with_values": "yes"})
    assert e.value.code == INVALID_PARAMS


# ── dispatch / protocol faults ───────────────────────────────────────────────


def test_dispatch_unknown_method(handlers):
    with pytest.raises(ProtocolError) as e:
        handlers.dispatch("nope", {})
    assert e.value.code == METHOD_NOT_FOUND


def test_dispatch_missing_param(handlers):
    with pytest.raises(ProtocolError) as e:
        handlers.dispatch("entity_at", {"path": "store.py", "line": 1})
    assert e.value.code == INVALID_PARAMS


def test_dispatch_known_methods_present(handlers):
    for m in ("open", "sync_buffer", "entity_at", "decorate", "author", "locate", "diff", "derived"):
        assert m in handlers.methods


# ── convert/ read surface (QW1): references / hover / rename / hierarchy ──────


def test_new_convert_verbs_registered(handlers):
    """The QW1 verbs are reachable (ping's method list grows)."""
    for m in (
        "references",
        "document_highlights",
        "hover",
        "type_hierarchy",
        "can_rename",
        "rename",
        "diagnostics_at",
    ):
        assert m in handlers.methods


def test_references_finds_cross_file_caller(handlers):
    """``references`` on the ``usd`` definition in money.py returns the call site
    in store.py — the find-callers surface (mirrors Spike E)."""
    res = handlers.references({"path": "money.py", "line": 1, "col": 5})
    refs = res["references"]
    paths = [r["path"] for r in refs]
    assert any(p.endswith("store.py") for p in paths), f"call site in store.py expected, got {paths}"
    # Every reference carries a 1-based range and a kind.
    for r in refs:
        assert "range" in r and "start" in r["range"]
        assert r["range"]["start"]["line"] >= 1
        assert isinstance(r["kind"], str)


def test_references_requires_position(handlers):
    with pytest.raises(ProtocolError) as e:
        handlers.references({"path": "money.py", "line": 1})
    assert e.value.code == INVALID_PARAMS


def test_document_highlights_in_file(handlers):
    res = handlers.document_highlights({"path": "money.py", "line": 1, "col": 5})
    highlights = res["highlights"]
    assert highlights, "the definition itself is highlighted"
    assert all(h["path"].endswith("money.py") for h in highlights), "scoped to the file"


def test_hover_returns_contents(handlers):
    res = handlers.hover({"path": "money.py", "line": 1, "col": 5})
    assert res is not None
    assert "contents" in res and isinstance(res["contents"], list)
    assert "location" in res


def test_type_hierarchy_reports_supertype(handlers):
    """``Book`` (book.py) subclasses ``Item`` — the hierarchy reports it."""
    res = handlers.type_hierarchy({"path": "book.py", "line": 4, "col": 7})
    assert res is not None
    assert res["item"]["name"] == "Book"
    supers = [s["name"] for s in res["supertypes"]]
    assert "Item" in supers


def test_can_rename_reports_range(handlers):
    res = handlers.can_rename({"path": "money.py", "line": 1, "col": 5})
    assert res["can_rename"] is True
    assert res["range"]["start"]["line"] == 1


def test_rename_serialises_changes_by_path(handlers):
    """``rename`` returns ``{new_name, changes}`` with ``changes`` keyed by path
    and each edit carrying the new text. (Pure edit computation — no rebind.)"""
    res = handlers.rename({"path": "money.py", "line": 1, "col": 5, "new_name": "dollars"})
    assert res is not None
    assert res["new_name"] == "dollars"
    changes = res["changes"]
    assert any(p.endswith("money.py") for p in changes), f"definition file edited, got {list(changes)}"
    for edits in changes.values():
        for e in edits:
            assert e["new_text"] == "dollars"
            assert "range" in e


def test_diagnostics_at_returns_filtered_shape(handlers):
    """``diagnostics_at`` returns a position-filtered diagnostics list (live, not
    cached). The clean fixture has none at this position → empty, well-shaped."""
    res = handlers.diagnostics_at({"path": "money.py", "line": 1, "col": 5})
    assert "diagnostics" in res and isinstance(res["diagnostics"], list)
    assert res["count"] == len(res["diagnostics"])


def test_layer_discovery_verbs_registered(handlers):
    for m in ("layers", "layer_ids"):
        assert m in handlers.methods
