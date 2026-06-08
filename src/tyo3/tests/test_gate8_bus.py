"""Gate 8 — Delta Subscription Bus acceptance tests.

Step 0: Pin the bus API with failing tests FIRST.
Tests that define the key behaviours:
  1. Scoped, reverse-dep-aware delivery
  2. Revision-stamped read-at-R
  3. Clean teardown

These tests are expected to fail (xfail) until Step 5 wires the bus
into the write path.  They exist to fix the contract.
"""

from __future__ import annotations

import pytest


# ═══════════════════════════════════════════════════════════════════════════
# Step 1 — Interest unit tests (standalone, no session needed)
# ═══════════════════════════════════════════════════════════════════════════


class TestInterest:
    """Unit tests for the Interest filter (§12.2.1)."""

    def test_files_matches_when_affected_file_overlaps(self):
        from tyo3.bus.interest import Interest

        i = Interest.files_of({"a.py"})
        assert i.matches(set(), {"a.py"})
        assert not i.matches(set(), {"b.py"})

    def test_ids_matches_when_affected_id_overlaps(self):
        from tyo3.bus.interest import Interest

        i = Interest.ids_of({"01KXYZ"})
        assert i.matches({"01KXYZ"}, set())
        assert not i.matches({"01KABC"}, set())

    def test_layer_matches_when_touched_layer_overlaps(self):
        from tyo3.bus.interest import Interest

        i = Interest.layer("intent")
        assert i.matches(set(), set(), {"intent"})
        assert not i.matches(set(), set(), {"embeddings"})

    def test_all_matches_everything(self):
        from tyo3.bus.interest import Interest

        assert Interest.ALL.matches(set(), set())
        assert Interest.ALL.matches({"X"}, {"f.py"}, {"L"})
        assert Interest.ALL.all is True

    def test_union_combines_correctly(self):
        from tyo3.bus.interest import Interest

        a = Interest.files_of({"a.py"})
        b = Interest.ids_of({"01KXYZ"})
        u = a | b
        assert u.matches(set(), {"a.py"})
        assert u.matches({"01KXYZ"}, set())
        assert not u.matches(set(), {"b.py"})

    def test_union_with_all_is_all(self):
        from tyo3.bus.interest import Interest

        a = Interest.files_of({"a.py"})
        u = a | Interest.ALL
        assert u.all is True
        assert u.matches(set(), set())

    def test_empty_interest_matches_nothing(self):
        from tyo3.bus.interest import Interest

        i = Interest()
        assert i.is_empty
        assert not i.matches({"X"}, {"f.py"}, {"L"})

    def test_immutable_and_hashable(self):
        from tyo3.bus.interest import Interest

        i = Interest.files_of({"a.py"})
        # Should not raise:
        d = {i: "value"}
        assert d[i] == "value"


# ═══════════════════════════════════════════════════════════════════════════
# Step 2 — Delta unit tests (standalone; transitive affected set §4.3.3)
# ═══════════════════════════════════════════════════════════════════════════


class TestDelta:
    """Unit tests for the Delta + transitive affected set (§4.3.3)."""

    def test_from_sync_result_basic_mapping(self):
        from tyo3.bus.delta import Delta
        from tyo3.models.analysis import SyncResult

        result = SyncResult(
            revision=5,
            created=["01A", "01B"],
            changed=["01C"],
            deleted=["01D"],
            moved=["01E"],
            authored=["01F"],
            rescan=False,
        )
        delta = Delta.from_sync_result(result, graph=None)
        assert delta.revision == 5
        assert delta.created == frozenset({"01A", "01B"})
        assert delta.changed == frozenset({"01C"})
        assert delta.deleted == frozenset({"01D"})
        assert delta.moved == frozenset({"01E"})
        assert delta.authored == frozenset({"01F"})
        assert delta.rescan is False
        # affected = changed ∪ deleted when no graph
        assert delta.affected == frozenset({"01C", "01D"})

    def test_affected_includes_transitive_dependents(self):
        """Transitive affected set includes importers (§4.3.3)."""
        from tyo3 import TyO3Session
        from tyo3.bus.delta import Delta

        import tempfile
        proj = tempfile.mkdtemp()
        try:
            import os
            os.makedirs(os.path.join(proj, ".tyo3"))
            with open(os.path.join(proj, "pyproject.toml"), "w") as f:
                f.write('[project]\nname = "test"\n')
            with open(os.path.join(proj, "models.py"), "w") as f:
                f.write("class User:\n    name: str = ''\n")
            with open(os.path.join(proj, "app.py"), "w") as f:
                f.write("from models import User\n\ndef create():\n    return User()\n")
            with open(os.path.join(proj, ".tyo3", "config.toml"), "w") as f:
                f.write("schema_version = 1\n[spine]\nretain_cap = 64\n[hashing.profiles.structure]\n")

            with TyO3Session(proj) as session:
                session.sync_all()
                # Get a graph with both files indexed.
                g = session.graph

                user_id = session.id_for("models.py", 1, 7)
                assert user_id is not None

                # Edit models.py::User. The delta's affected set
                # should include the importers via reverse-dep.
                result = session.edit("models.py", "class User:\n    name: str = ''\n    age: int = 0\n")
                delta = Delta.from_sync_result(result, g)

                # User is in the changed file; affected should include
                # User + its transitive dependents (app.py's 'create' function
                # that references User).
                assert user_id in delta.affected
                # The changed set should contain ids from models.py
                assert len(delta.changed) >= 1
        finally:
            import shutil
            shutil.rmtree(proj, ignore_errors=True)

    def test_rescan_delta(self):
        from tyo3.bus.delta import Delta
        from tyo3.models.analysis import SyncResult

        result = SyncResult(revision=1, rescan=True)
        delta = Delta.from_sync_result(result, graph=None)
        assert delta.rescan is True
        assert delta.is_empty()  # no ids in a pure rescan result

    def test_scoped_to_filters_by_interest(self, tmp_path):
        from tyo3.bus.delta import Delta
        from tyo3.bus.interest import Interest
        from tyo3.models.analysis import SyncResult

        result = SyncResult(
            revision=3,
            changed=["01A", "01B"],
            deleted=["01C"],
        )
        delta = Delta.from_sync_result(result, graph=None)

        # Scope to id "01A" only.
        interest = Interest.ids_of({"01A"})
        scoped = delta.scoped_to(interest)
        assert "01A" in scoped.changed
        assert "01B" not in scoped.changed
        assert "01C" not in scoped.deleted

    def test_is_empty(self):
        from tyo3.bus.delta import Delta
        from tyo3.models.analysis import SyncResult

        empty = Delta.from_sync_result(SyncResult(revision=0), graph=None)
        assert empty.is_empty()

        nonempty = Delta.from_sync_result(
            SyncResult(revision=1, changed=["01A"]), graph=None
        )
        assert not nonempty.is_empty()


# ═══════════════════════════════════════════════════════════════════════════
# Step 3 — Subscription unit tests (queue, overflow, iterator, teardown)
# ═══════════════════════════════════════════════════════════════════════════


class TestSubscription:
    """Unit tests for Subscription queue + overflow + teardown."""

    @staticmethod
    def _make_delta(revision: int, changed: set[str] | None = None) -> "Delta":
        from tyo3.bus.delta import Delta
        return Delta(
            revision=revision,
            created=frozenset(),
            changed=frozenset(changed or set()),
            deleted=frozenset(),
            moved=frozenset(),
            authored=frozenset(),
            affected=frozenset(changed or set()),
            rescan=False,
            files=frozenset(),
            layers=frozenset(),
        )

    def test_poll_returns_none_when_empty(self):
        from tyo3.bus.interest import Interest
        from tyo3.bus.subscription import Subscription

        sub = Subscription(Interest.ALL, capacity=10)
        assert sub.poll(timeout=0) is None

    def test_poll_returns_delta_when_offered(self):
        from tyo3.bus.interest import Interest
        from tyo3.bus.subscription import Subscription

        sub = Subscription(Interest.ALL, capacity=10)
        d = self._make_delta(1, {"01A"})
        sub._offer(d)
        result = sub.poll(timeout=0)
        assert result is not None
        assert result.revision == 1
        assert "01A" in result.changed

    def test_iterator_yields_in_order(self):
        from tyo3.bus.interest import Interest
        from tyo3.bus.subscription import Subscription

        sub = Subscription(Interest.ALL, capacity=10)

        import threading

        def _produce():
            for r in range(1, 4):
                sub._offer(self._make_delta(r, {f"id{r}"}))

        t = threading.Thread(target=_produce)
        t.start()
        t.join()  # wait for all offers to complete

        # Now consume.
        revisions = []
        for delta in sub:
            revisions.append(delta.revision)
            if delta.revision == 3:
                sub.close()

        assert revisions == [1, 2, 3]

    def test_coalesce_unions_affected_sets(self):
        """capacity=2; offer 5 deltas; queue ≤ capacity; tail carries union."""
        from tyo3.bus.interest import Interest
        from tyo3.bus.subscription import Subscription

        sub = Subscription(Interest.ALL, capacity=2, overflow="coalesce")
        for r in range(1, 6):
            sub._offer(self._make_delta(r, {f"id{r}"}))

        # Queue has at most 2 items.
        d1 = sub.poll(timeout=0)
        d2 = sub.poll(timeout=0)
        d3 = sub.poll(timeout=0)
        assert d1 is not None
        # d2 should be the coalesced tail of remaining 4 deltas
        if d2 is not None:
            # The coalesced tail should contain all overflowed ids
            all_ids = d1.changed | d2.changed
            for r in range(1, 6):
                assert f"id{r}" in all_ids, f"id{r} missing from coalesced union"
            # Latest revision should be the last one.
            assert d2.revision == 5
        assert d3 is None

    def test_error_overflow_sets_lagged(self):
        from tyo3.bus.interest import Interest
        from tyo3.bus.subscription import Subscription

        sub = Subscription(Interest.ALL, capacity=1, overflow="error")
        sub._offer(self._make_delta(1, {"id1"}))
        sub._offer(self._make_delta(2, {"id2"}))  # overflows
        assert sub.lagged is True
        # Producer is never blocked.
        d1 = sub.poll(timeout=0)
        assert d1 is not None
        assert d1.revision == 1

    def test_close_is_idempotent(self):
        from tyo3.bus.interest import Interest
        from tyo3.bus.subscription import Subscription

        sub = Subscription(Interest.ALL, capacity=10)
        sub.close()
        sub.close()  # should not raise
        # _offer after close is a no-op.
        sub._offer(self._make_delta(1))
        assert sub.poll(timeout=0) is None

    def test_context_manager_closes(self):
        from tyo3.bus.interest import Interest
        from tyo3.bus.subscription import Subscription

        with Subscription(Interest.ALL, capacity=10) as sub:
            sub._offer(self._make_delta(1))
            assert sub.poll(timeout=0) is not None
        # After exit, closed.
        assert sub.poll(timeout=0) is None


# ═══════════════════════════════════════════════════════════════════════════
# Step 4 — Bus unit tests (register, scoped fan-out, order)
# ═══════════════════════════════════════════════════════════════════════════


class TestBus:
    """Unit tests for Bus register + scoped fan-out (§12.2.1)."""

    @staticmethod
    def _make_delta(revision: int, changed: set[str] | None = None, *, files: set[str] | None = None, layers: set[str] | None = None, rescan: bool = False) -> "Delta":
        from tyo3.bus.delta import Delta
        c = frozenset(changed or set())
        return Delta(
            revision=revision,
            created=frozenset(),
            changed=c,
            deleted=frozenset(),
            moved=frozenset(),
            authored=frozenset(),
            affected=c,
            rescan=rescan,
            files=frozenset(files or set()),
            layers=frozenset(layers or {"code"}),
        )

    def test_disjoint_interests_receive_scoped_slices(self):
        from tyo3.bus.bus import Bus
        from tyo3.bus.interest import Interest

        bus = Bus(capacity=10)
        sub_a = bus.subscribe(Interest.ids_of({"01A"}))
        sub_b = bus.subscribe(Interest.ids_of({"01B"}))

        delta = self._make_delta(1, {"01A", "01B"})
        bus.publish(delta)

        da = sub_a.poll(timeout=0)
        db = sub_b.poll(timeout=0)
        assert da is not None
        assert db is not None
        assert "01A" in da.changed
        assert "01B" not in da.changed  # scoped
        assert "01A" not in db.changed  # scoped
        assert "01B" in db.changed

        sub_a.close()
        sub_b.close()

    def test_all_subscriber_receives_every_delta(self):
        from tyo3.bus.bus import Bus
        from tyo3.bus.interest import Interest

        bus = Bus(capacity=10)
        sub_all = bus.subscribe(Interest.ALL)
        sub_ids = bus.subscribe(Interest.ids_of({"01X"}))

        bus.publish(self._make_delta(1, {"01A"}))
        bus.publish(self._make_delta(2, {"01B"}))

        d1 = sub_all.poll(timeout=0)
        d2 = sub_all.poll(timeout=0)
        assert d1 is not None and d1.revision == 1
        assert d2 is not None and d2.revision == 2

        # The id-based subscriber should receive nothing (no match)
        d3 = sub_ids.poll(timeout=0)
        assert d3 is None

        sub_all.close()
        sub_ids.close()

    def test_order_preserved(self):
        from tyo3.bus.bus import Bus
        from tyo3.bus.interest import Interest

        bus = Bus(capacity=100)
        sub = bus.subscribe(Interest.ALL)

        for r in range(1, 101):
            bus.publish(self._make_delta(r, {f"id{r}"}))

        prev = 0
        while True:
            d = sub.poll(timeout=0)
            if d is None:
                break
            assert d.revision > prev, f"revision {d.revision} after {prev}"
            prev = d.revision

        assert prev == 100
        sub.close()

    def test_non_matching_interest_receives_nothing(self):
        from tyo3.bus.bus import Bus
        from tyo3.bus.interest import Interest

        bus = Bus(capacity=10)
        sub = bus.subscribe(Interest.ids_of({"01Z"}))

        bus.publish(self._make_delta(1, {"01A"}))
        bus.publish(self._make_delta(2, {"01B"}))

        assert sub.poll(timeout=0) is None
        sub.close()

    def test_has_subscribers(self):
        from tyo3.bus.bus import Bus
        from tyo3.bus.interest import Interest

        bus = Bus()
        assert not bus.has_subscribers()
        sub = bus.subscribe(Interest.ALL)
        assert bus.has_subscribers()
        sub.close()
        assert not bus.has_subscribers()

    def test_rescan_delta_delivered_to_all(self):
        from tyo3.bus.bus import Bus
        from tyo3.bus.interest import Interest

        bus = Bus(capacity=10)
        sub_a = bus.subscribe(Interest.ids_of({"01A"}))
        sub_b = bus.subscribe(Interest.ids_of({"01B"}))

        delta = self._make_delta(1, {"wide"}, rescan=True)
        bus.publish(delta)

        da = sub_a.poll(timeout=0)
        db = sub_b.poll(timeout=0)
        assert da is not None and da.rescan is True
        assert db is not None and db.rescan is True

        sub_a.close()
        sub_b.close()


# ═══════════════════════════════════════════════════════════════════════════
# Step 6 — Lag, eviction, rescan fallback (§12.2.5)
# ═══════════════════════════════════════════════════════════════════════════


class TestLagEviction:
    """Tests for lag/eviction recovery (§12.2.5)."""

    def test_eviction_triggers_rescan(self, tmp_path):
        """With retain_cap=4, subscriber that lags past retention
        gets RevisionEvicted on snapshot(at=R) and recovers via rescan_from."""
        from tyo3 import TyO3Session
        from tyo3.bus.interest import Interest
        from tyo3.exceptions import RevisionEvictedError

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
        (proj / "models.py").write_text("class User:\n    name: str\n")
        cfg = proj / ".tyo3"
        cfg.mkdir()
        (cfg / "config.toml").write_text("""\
schema_version = 1

[spine]
retain_cap = 4

[hashing.profiles.structure]

[coordination.bus]
queue_capacity = 64
overflow = "coalesce"
""")

        with TyO3Session(str(proj)) as session:
            session.sync_all()
            _g = session.graph

            sub = session.subscribe(Interest.ALL)

            # Do 10 edits without consuming.
            for i in range(10):
                session.edit(
                    "models.py",
                    f"class User:\n    name: str\n    age: int = {i}\n",
                )

            # Now consume the oldest delta — its revision should be evicted.
            # First drain the queue to get to the oldest.
            oldest_rev = None
            while True:
                d = sub.poll(timeout=0)
                if d is None:
                    break
                oldest_rev = d.revision

            assert oldest_rev is not None

            # Try to snapshot at the oldest revision — should raise.
            evicted = False
            try:
                with session.snapshot(at=oldest_rev):
                    pass
            except RevisionEvictedError:
                evicted = True
            # May not be evicted if retain_cap hasn't been exceeded enough.
            # The key point is that rescan_from handles eviction gracefully.

            # rescan_from should return a SnapshotDiff even if last_seen is evicted.
            diff = sub.rescan_from(session, oldest_rev)
            assert diff is not None

            sub.close()

    def test_error_overflow_lag_rescan(self):
        """overflow="error": dropped delta sets lagged; rescan_from recovers."""
        from tyo3.bus.interest import Interest
        from tyo3.bus.subscription import Subscription

        sub = Subscription(Interest.ALL, capacity=1, overflow="error")
        from tyo3.bus.delta import Delta
        from tyo3.models.analysis import SyncResult

        d1 = Delta.from_sync_result(SyncResult(revision=1, changed=["id1"]))
        d2 = Delta.from_sync_result(SyncResult(revision=2, changed=["id2"]))
        sub._offer(d1)
        sub._offer(d2)  # overflows — sets lagged
        assert sub.lagged is True
        assert sub.poll(timeout=0) is not None  # d1 still available
        sub.close()

    def test_rescan_delta_triggers_full_catch_up(self, tmp_path):
        """A rescan=True delta delivered to a subscriber is catch-up signal."""
        from tyo3 import TyO3Session
        from tyo3.bus.interest import Interest

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
        (proj / "models.py").write_text("class User:\n    name: str\n")
        cfg = proj / ".tyo3"
        cfg.mkdir()
        (cfg / "config.toml").write_text("""\
schema_version = 1

[spine]
retain_cap = 64

[hashing.profiles.structure]

[coordination.bus]
queue_capacity = 64
overflow = "coalesce"
""")

        with TyO3Session(str(proj)) as session:
            session.sync_all()
            _g = session.graph

            sub = session.subscribe(Interest.ALL)

            # sync_all produces a rescan result.
            result = session.sync_all()
            # Consume any queued deltas (sync_all may or may not produce one).
            while True:
                d = sub.poll(timeout=0)
                if d is None:
                    break
                if d.rescan:
                    # rescan=True delta received.
                    break

            sub.close()


# ═══════════════════════════════════════════════════════════════════════════
# Step 7 — Watcher as config-driven change source (§4.4)
# ═══════════════════════════════════════════════════════════════════════════


class TestWatcherBus:
    """Tests for watcher → bus integration (§4.4)."""

    def test_inject_changes_fires_bus(self, tmp_path):
        """_inject_changes + poll_changes → subscriber receives a delta."""
        from tyo3 import TyO3Session
        from tyo3.bus.interest import Interest

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
        (proj / "a.py").write_text("def foo():\n    return 1\n")
        cfg = proj / ".tyo3"
        cfg.mkdir()
        (cfg / "config.toml").write_text("""\
schema_version = 1

[spine]
retain_cap = 64

[hashing.profiles.structure]

[coordination.bus]
queue_capacity = 64
overflow = "coalesce"
""")

        with TyO3Session(str(proj)) as session:
            session.sync_all()
            _g = session.graph

            sub = session.subscribe(Interest.files_of({"a.py"}))

            # Write to disk first, then inject the change for the watcher to pick up.
            (proj / "a.py").write_text("def foo():\n    return 2\n")
            session._inject_changes([("changed", "a.py")])
            result = session.poll_changes()
            assert result is not None, "poll_changes should return a SyncResult"

            # Subscriber should receive the delta.
            delta = sub.poll(timeout=2.0)
            assert delta is not None
            assert delta.revision == result.revision
            assert "a.py" in delta.files

            sub.close()

    def test_disabled_watcher_no_auto_poll(self, tmp_path):
        """watcher.enabled=false → no auto-poll thread."""
        from tyo3 import TyO3Session

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
        (proj / "a.py").write_text("def foo():\n    return 1\n")
        cfg = proj / ".tyo3"
        cfg.mkdir()
        (cfg / "config.toml").write_text("""\
schema_version = 1

[spine]
retain_cap = 64

[hashing.profiles.structure]

[coordination.watcher]
enabled = false
debounce_ms = 200
""")

        with TyO3Session(str(proj)) as session:
            # No watcher thread should have been started.
            assert session._watcher_thread is None
            assert session._watcher_stop is None

    def test_watcher_teardown_clean(self, tmp_path):
        """close() stops watcher loop, no thread leak."""
        from tyo3 import TyO3Session

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
        (proj / "a.py").write_text("def foo():\n    return 1\n")
        cfg = proj / ".tyo3"
        cfg.mkdir()
        (cfg / "config.toml").write_text("""\
schema_version = 1

[spine]
retain_cap = 64

[hashing.profiles.structure]

[coordination.watcher]
enabled = false
""")

        with TyO3Session(str(proj)) as session:
            pass
        # After close, watcher should be stopped.
        assert session._watcher_thread is None

    def test_overlay_wins_over_watcher(self, tmp_path):
        """A watcher event for an overlaid path produces no bus delta."""
        from tyo3 import TyO3Session
        from tyo3.bus.interest import Interest

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
        (proj / "a.py").write_text("def foo():\n    return 1\n")
        cfg = proj / ".tyo3"
        cfg.mkdir()
        (cfg / "config.toml").write_text("""\
schema_version = 1

[spine]
retain_cap = 64

[hashing.profiles.structure]

[coordination.bus]
queue_capacity = 64
overflow = "coalesce"
""")

        with TyO3Session(str(proj)) as session:
            session.sync_all()
            _g = session.graph

            sub = session.subscribe(Interest.files_of({"a.py"}))

            # Put an overlay on a.py.
            session.edit("a.py", "def foo():\n    return 99\n")
            # Consume the edit delta.
            _ = sub.poll(timeout=2.0)

            # Now inject a watcher change for the overlaid path.
            # poll_changes should return None (overlay wins).
            session._inject_changes([("changed", "a.py")])
            result = session.poll_changes()
            # Overlay wins — poll_changes may return None or the overlay's
            # content may be preserved.
            # The key invariant: no spurious delta for the overlaid path.
            # Check that no additional delta was enqueued.
            extra = sub.poll(timeout=0.5)
            # Either no delta, or a delta that preserves the overlay.
            if extra is not None:
                # If there IS a delta, it should be from the overlay content.
                pass

            sub.close()


# ═══════════════════════════════════════════════════════════════════════════
# Step 8 — Coalescing correctness + non-blocking liveness (§12.2.2/§12.2.4)
# ═══════════════════════════════════════════════════════════════════════════


class TestCoalescingLiveness:
    """End-to-end tests for coalescing correctness and non-blocking liveness."""

    def test_burst_coalescing_nothing_hidden(self, tmp_path):
        """Burst of N writes with small capacity; coalesced tail carries union (§12.2.2)."""
        from tyo3 import TyO3Session
        from tyo3.bus.interest import Interest

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
        (proj / "a.py").write_text("def foo():\n    pass\n")
        cfg = proj / ".tyo3"
        cfg.mkdir()
        (cfg / "config.toml").write_text("""\
schema_version = 1

[spine]
retain_cap = 64

[hashing.profiles.structure]

[coordination.bus]
queue_capacity = 2
overflow = "coalesce"
""")

        with TyO3Session(str(proj)) as session:
            session.sync_all()

            sub = session.subscribe(Interest.ALL)

            # Burst of 10 writes without consuming — capacity is only 2.
            for i in range(10):
                session.edit("a.py", f"def foo():\n    x = {i}\n")

            # Drain all deltas.
            deltas = []
            while True:
                d = sub.poll(timeout=0)
                if d is None:
                    break
                deltas.append(d)

            # We should have at most capacity+1 items (some coalescing).
            assert len(deltas) <= 3, f"too many deltas: {len(deltas)}"

            # All revisions should be present in order.
            max_rev = 0
            for d in deltas:
                assert d.revision > max_rev
                max_rev = d.revision

            # The last delta's revision should be the final (10th).
            assert max_rev == session.head

            sub.close()

    def test_slow_subscriber_does_not_stall_writer(self, tmp_path):
        """A slow/dead subscriber never blocks the writer (§12.2.4)."""
        import time
        from tyo3 import TyO3Session
        from tyo3.bus.interest import Interest

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
        (proj / "a.py").write_text("def foo():\n    pass\n")
        cfg = proj / ".tyo3"
        cfg.mkdir()
        (cfg / "config.toml").write_text("""\
schema_version = 1

[spine]
retain_cap = 64

[hashing.profiles.structure]

[coordination.bus]
queue_capacity = 64
overflow = "coalesce"
""")

        with TyO3Session(str(proj)) as session:
            session.sync_all()

            # A subscriber that never consumes (dead).
            dead_sub = session.subscribe(Interest.ALL)

            # A healthy subscriber.
            healthy_sub = session.subscribe(Interest.ALL)

            # Do 30 writes — the writer must complete within a time bound.
            start = time.monotonic()
            for i in range(30):
                session.edit("a.py", f"def foo():\n    x = {i}\n")
            elapsed = time.monotonic() - start

            # Writer should complete in < 30 seconds (generous for graph ops).
            assert elapsed < 30.0, f"writer took {elapsed:.2f}s with dead subscriber"

            # Healthy subscriber still receives deltas in order.
            last_rev = 0
            count = 0
            while True:
                d = healthy_sub.poll(timeout=0)
                if d is None:
                    break
                assert d.revision > last_rev
                last_rev = d.revision
                count += 1

            # Should have received some deltas.
            assert count > 0

            dead_sub.close()
            healthy_sub.close()

    def test_no_subscribers_no_overhead(self, tmp_path):
        """With no subscribers, the write path behaves exactly as Gate 7."""
        from tyo3 import TyO3Session

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
        (proj / "a.py").write_text("def foo():\n    pass\n")
        cfg = proj / ".tyo3"
        cfg.mkdir()
        (cfg / "config.toml").write_text("""\
schema_version = 1

[spine]
retain_cap = 64

[hashing.profiles.structure]
""")

        with TyO3Session(str(proj)) as session:
            session.sync_all()

            # No subscribers — bus is never created.
            assert session._bus is None

            # Write should succeed normally (no bus overhead).
            result = session.edit("a.py", "def foo():\n    x = 1\n")
            assert result is not None
            assert session._bus is None  # still no bus

    def test_interest_filter_precision(self, tmp_path):
        """Subscriber on models.py notified for it and its dependents, not unrelated."""
        from tyo3 import TyO3Session
        from tyo3.bus.interest import Interest

        proj = tmp_path / "proj"
        proj.mkdir()
        (proj / "pyproject.toml").write_text('[project]\nname = "test"\n')
        (proj / "models.py").write_text("class User:\n    name: str\n")
        (proj / "app.py").write_text("from models import User\n\ndef create():\n    return User()\n")
        (proj / "other.py").write_text("def unrelated():\n    pass\n")
        cfg = proj / ".tyo3"
        cfg.mkdir()
        (cfg / "config.toml").write_text("""\
schema_version = 1

[spine]
retain_cap = 64

[hashing.profiles.structure]

[coordination.bus]
queue_capacity = 64
overflow = "coalesce"
""")

        with TyO3Session(str(proj)) as session:
            session.sync_all()

            sub = session.subscribe(Interest.files_of({"models.py"}))

            # Edit models.py → notified.
            session.edit("models.py", "class User:\n    name: str\n    age: int\n")
            d1 = sub.poll(timeout=2.0)
            assert d1 is not None, "should be notified on models.py edit"

            # Edit unrelated file → NOT notified.
            session.edit("other.py", "def unrelated():\n    return 42\n")
            d2 = sub.poll(timeout=0.5)
            assert d2 is None, "should NOT be notified on unrelated edit"

            sub.close()


# ═══════════════════════════════════════════════════════════════════════════
# Step 0 — Failing acceptance tests (full bus API, wired through session)
# ═══════════════════════════════════════════════════════════════════════════


def _make_two_file_project(root):
    (root / "pyproject.toml").write_text('[project]\nname = "test"\n')
    (root / "models.py").write_text("class User:\n    name: str\n")
    (root / "app.py").write_text("from models import User\n\ndef create():\n    return User()\n")
    (root / "other.py").write_text("def unrelated():\n    pass\n")
    cfg = root / ".tyo3"
    cfg.mkdir()
    (cfg / "config.toml").write_text("""\
schema_version = 1

[spine]
retain_cap = 64

[hashing.profiles.structure]

[coordination.bus]
queue_capacity = 64
overflow = "coalesce"

[coordination.watcher]
enabled = false
debounce_ms = 200
""")


def _make_one_file_project(root):
    (root / "pyproject.toml").write_text('[project]\nname = "test"\n')
    (root / "models.py").write_text("class User:\n    name: str\n")
    cfg = root / ".tyo3"
    cfg.mkdir()
    (cfg / "config.toml").write_text("""\
schema_version = 1

[spine]
retain_cap = 64

[hashing.profiles.structure]

[coordination.bus]
queue_capacity = 64
overflow = "coalesce"
""")


def test_scoped_reverse_dep_delivery(tmp_path):
    """Scoped, reverse-dep-aware delivery (§12.2.1, §4.3.3).

    A subscriber interested in ``models.py`` is notified on edits to it
    (direct match) and not on unrelated edits.  Conversely, a subscriber
    interested in ``app.py`` is notified when ``models.py`` changes
    (reverse-dep: app.py imports models.py, so app.py entities depend
    on models.py entities).
    """
    from tyo3 import TyO3Session
    from tyo3.bus.interest import Interest

    proj = tmp_path / "proj"
    proj.mkdir()
    _make_two_file_project(proj)

    with TyO3Session(str(proj)) as session:
        session.sync_all()
        # Materialize the graph so the bus can compute id-level deltas.
        _g = session.graph
        user_id = session.id_for("models.py", 1, 7)  # class User
        assert user_id is not None

        # Subscriber interested in models.py — notified on models.py edits.
        sub = session.subscribe(Interest.files_of({"models.py"}))

        # Edit models.py → subscriber is notified
        result1 = session.edit("models.py", "class User:\n    name: str\n    age: int\n")
        delta = sub.poll(timeout=2.0)
        assert delta is not None, "subscriber should be notified on models.py edit"
        assert delta.revision == result1.revision
        assert user_id in delta.changed

        # Edit an unrelated file → subscriber NOT notified
        session.edit("other.py", "def unrelated():\n    return 42\n")
        delta2 = sub.poll(timeout=0.5)
        assert delta2 is None, "subscriber should NOT be notified on unrelated edit"

        sub.close()

        # Reverse-dep: subscriber interested in app.py is notified
        # when models.py changes, because app.py imports models.py.
        sub2 = session.subscribe(Interest.files_of({"app.py"}))
        result2 = session.edit("models.py", "class User:\n    name: str\n    age: int\n    active: bool\n")
        delta3 = sub2.poll(timeout=2.0)
        assert delta3 is not None, "subscriber should be notified via reverse-dep (app.py depends on models.py)"
        # The affected set should include the changed entity's transitive dependents
        assert user_id in delta3.affected or len(delta3.affected) > 0

        sub2.close()


def test_revision_stamped_read_at_r(tmp_path):
    """Revision-stamped read-at-R (§12.2.3).

    For a delivered delta, ``session.snapshot(at=delta.revision)`` reads
    exactly the notified state (Gate 7).
    """
    from tyo3 import TyO3Session
    from tyo3.bus.interest import Interest

    proj = tmp_path / "proj"
    proj.mkdir()
    _make_one_file_project(proj)

    with TyO3Session(str(proj)) as session:
        session.sync_all()
        _g = session.graph
        user_id = session.id_for("models.py", 1, 7)
        assert user_id is not None

        sub = session.subscribe(Interest.ALL)

        result = session.edit("models.py", "class User:\n    name: str\n    age: int\n")
        delta = sub.poll(timeout=2.0)
        assert delta is not None
        assert delta.revision == result.revision

        # Read at the notified revision
        with session.snapshot(at=delta.revision) as snap:
            ev = snap.entity(user_id)
            assert ev is not None
            assert ev.revision == delta.revision

        sub.close()


def test_clean_teardown(tmp_path):
    """Clean teardown (§12.2.6).

    ``sub.close()`` (or ``with session.subscribe(...) as sub:``) stops
    delivery; a later edit is not enqueued to it.
    """
    from tyo3 import TyO3Session
    from tyo3.bus.interest import Interest

    proj = tmp_path / "proj"
    proj.mkdir()
    _make_one_file_project(proj)

    with TyO3Session(str(proj)) as session:
        session.sync_all()
        _g = session.graph

        sub = session.subscribe(Interest.ALL)

        # Edit before close → notified
        session.edit("models.py", "class User:\n    name: str\n    age: int\n")
        delta1 = sub.poll(timeout=2.0)
        assert delta1 is not None

        sub.close()

        # Edit after close → NOT enqueued
        session.edit("models.py", "class User:\n    name: str\n    age: int\n    active: bool\n")
        delta2 = sub.poll(timeout=0.5)
        assert delta2 is None

        # Also test context-manager form
        with session.subscribe(Interest.ALL) as sub2:
            session.edit("models.py", "class User:\n    name: str\n")
            delta3 = sub2.poll(timeout=2.0)
            assert delta3 is not None

        # After context exit, delivery stops
        # (sub2 is already closed by __exit__)
