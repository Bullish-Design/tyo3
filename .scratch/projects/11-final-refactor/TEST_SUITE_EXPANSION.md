# TyO3 — Concurrency Test-Suite Expansion Guide

**Audience:** an engineer (intern) hardening the test suite for the Option C
concurrency refactor (`OPTION_C_SNAPSHOT_IMPLEMENTATION.md`, same folder).
**Goal:** close the coverage gaps in `src/tyo3/tests/test_concurrency.py` so the
suite actually exercises the load-bearing safety claims of the refactor — not just
the happy path.
**Prerequisite reading:** `OPTION_C_SNAPSHOT_IMPLEMENTATION.md` §0 (the
swap-don't-mutate invariant) and §1.3 (why the `Mutex` is load-bearing). This guide
assumes the implementation is complete, built, and green.

This guide is **prescriptive**. Follow the phases in order. Each phase is
independently committable and independently green. You are only **adding** tests —
do not modify `rust/src/` or `src/tyo3/session.py`. If a new test fails, that is a
finding: stop and report it, do not "fix" it by weakening the assertion.

> ## 🔴 GOLDEN RULE — run EVERYTHING through the devenv shell
> Same rule as the implementation guide. **Every** pytest/python/ruff invocation
> must be prefixed with `devenv shell -- …` (or run from inside an interactive
> `devenv shell`). Running `pytest`/`python` bare gives the wrong toolchain and a
> stale extension.
>
> - ✅ `devenv shell -- pytest src/tyo3/tests/test_concurrency.py -v`
> - ✅ `devenv shell -- tests`
> - ❌ `pytest …`, `python …` (bare — wrong env)
>
> The only commands safe to run bare are read-only repo greps (`grep`, `git status`)
> used in the verification steps.

---

## 0. Why these tests (the gaps, ranked)

The current suite is strong on lifecycle and breadth but thin on the two things that
justify the refactor's safety. Each phase below targets a specific gap:

| Phase | Gap closed | Risk if untested |
|---|---|---|
| 1 | **Concurrent `reload()` overlapping in-flight reads** | The central invariant (swap-don't-mutate prevents `salsa::Cancelled`) is completely unverified. **Highest risk.** |
| 1 | **A single `TyO3Session` shared across threads** | Session uses the same clone+detach path as the snapshot but is only ever tested one-session-per-thread. |
| 2 | **Error-path propagation on snapshots** | The snapshot's hand-duplicated `#[pymethods]` bodies build `PyErr` after the GIL re-acquire; never tested → silent drift. |
| 3 | **Full session↔snapshot equivalence** (all 13 reads) + the `Some` branch of `hover`/`type_hierarchy` | Only 3/13 methods have parity tests; the `None` branch is the only one truly exercised for hover/hierarchy. |
| 4 | **`close()` racing in-flight reads** | The benign-by-design close race is asserted nowhere. |
| 5 | **Multiple snapshots pinned across multiple reloads** | Isolation is proven for one snapshot and one reload only. |
| 6 | (optional) deeper cross-thread equality, second GIL-proof method | Hardening only. |

---

## 1. Environment & entrypoints

| Task | Command |
|---|---|
| Run ONLY the concurrency file | `devenv shell -- pytest src/tyo3/tests/test_concurrency.py -v` |
| Run ONE new test | `devenv shell -- pytest src/tyo3/tests/test_concurrency.py::TestReloadConcurrency::test_reload_during_concurrent_reads -v` |
| Run the new tests repeatedly (flake check) | `devenv shell -- pytest src/tyo3/tests/test_concurrency.py -v --count=5` *(if `pytest-repeat` is available; otherwise loop the command)* |
| Full suite (end of work) | `devenv shell -- tests` |
| Lint / format | `devenv shell -- ruff check src && devenv shell -- ruff format src` |

> **Timeouts.** Threaded tests use `f.result(timeout=…)` / `thread.join(timeout=…)`.
> Never use a bare unbounded join — a real deadlock must fail the test, not hang the
> suite. Use **30 s** for pooled reads, **60 s** for the reload-under-load test.
> Do not give pytest itself a short global timeout that would mask a slow CI box.

All new tests go in the **existing** `src/tyo3/tests/test_concurrency.py`. Keep the
existing classes; append the new ones. Reuse the module-level helpers already there
(`_fixture_path`, `_serial`, `_parallel`, `FIXTURES_DIR`).

---

## 2. Phase 0 — shared helpers (add once, used by later phases)

Add these helpers near the top of `test_concurrency.py`, right after the existing
timing helpers (after `_parallel`, ~line 60). They are imported by nothing else —
keep them module-private.

```python
import threading  # add to the imports block at the top of the file


# ── Equivalence / error helpers ─────────────────────────────────────────


def _dump(obj):
    """Normalize a read result to a plain comparable value.

    Pydantic models → dicts; lists recurse; None and scalars pass through.
    Lets us assert session.<read>(...) == snapshot.<read>(...) for every method
    without caring whether it returned a model, a list, or None.
    """
    if obj is None:
        return None
    if isinstance(obj, list):
        return [_dump(x) for x in obj]
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return obj


def _capture_exc(fn):
    """Run *fn* and return the type of any exception it raised, else None."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001 — we want the type of whatever it is
        return type(e)
    return None
```

**Verify:** `devenv shell -- ruff check src/tyo3/tests/test_concurrency.py` is clean.
(No test runs yet — these are just helpers.)

---

## 3. Phase 1 — concurrency invariants (HIGHEST PRIORITY)

Two tests. The first is the most important test in the whole suite.

### 3.1 Reload racing in-flight reads (the invariant)

This proves the §0 claim: because `reload` **swaps** the db under the lock and reads
**clone** under the lock, a read that is mid-flight (lock + GIL already released) on
its own cloned db is never invalidated by a concurrent reload — no `salsa::Cancelled`,
no panic, no error. Reads never observe a closed state because `close` is never called.

> **Why a copied fixture:** `reload()` re-reads the project from disk. Use a `tmp_path`
> copy so the test is hermetic and you *could* mutate it between reloads (we don't need
> to here — we only need reloads to overlap reads).

Append this class:

```python
# ── Reload / read concurrency ────────────────────────────────────────────


@needs_native
class TestReloadConcurrency:
    """The load-bearing invariant: reload() swaps the db; in-flight reads on a
    cloned db are never invalidated (no salsa::Cancelled, no panic, no error)."""

    def test_reload_during_concurrent_reads(self, tmp_path: StdPath) -> None:
        """Hammer reload() on one thread while N threads read — zero errors."""
        project_root = tmp_path / "project"
        shutil.copytree(FIXTURES_DIR / "simple_package", project_root)

        session = TyO3Session(project_root)
        errors: list[tuple[str, BaseException]] = []
        stop = threading.Event()

        def reader() -> None:
            try:
                while not stop.is_set():
                    # Mix of full-project (rayon) and cursor reads to maximize
                    # the chance a read is mid-flight when the swap lands.
                    session.check()
                    session.document_symbols("main.py")
                    session.files()
            except Exception as e:  # noqa: BLE001
                errors.append(("reader", e))

        def reloader() -> None:
            try:
                for _ in range(25):
                    session.reload()
            except Exception as e:  # noqa: BLE001
                errors.append(("reloader", e))
            finally:
                stop.set()

        threads = [threading.Thread(target=reader) for _ in range(4)]
        threads.append(threading.Thread(target=reloader))
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)
            assert not any(t.is_alive() for t in threads), "deadlock: a thread did not finish"
            # A read must NEVER raise here — close() is never called, so there is no
            # closed window, and the swap-don't-mutate invariant means no Cancelled.
            assert not errors, f"Concurrent reload/read raised: {errors}"
        finally:
            stop.set()
            session.close()
```

**Run it — and run it several times** (races are probabilistic):

```bash
for i in 1 2 3 4 5; do
  devenv shell -- pytest \
    src/tyo3/tests/test_concurrency.py::TestReloadConcurrency::test_reload_during_concurrent_reads \
    -v || break
done
```

**Expected:** 5/5 green. If it *ever* fails with a `salsa::Cancelled` /
`PyRuntimeError` / panic, **stop — that is a real concurrency bug in the
implementation**, not a test problem. Report it with the captured `errors` list; do
not loosen the assertion.

### 3.2 A single session shared across threads

Mirrors `TestSnapshotThreadSafety.test_snapshot_shared_across_threads`, but for the
session — closing the gap that every concurrency test currently opens a fresh session
per thread.

```python
@needs_native
class TestSessionThreadSafety:
    """One TyO3Session, shared across threads — same clone+detach path as Snapshot."""

    ROOT = _fixture_path("simple_package")

    def test_session_shared_across_threads(self) -> None:
        session = TyO3Session(self.ROOT)
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
                futures = [ex.submit(session.check) for _ in range(4)]
                results = [f.result(timeout=30) for f in futures]
            counts = [len(r.diagnostics) for r in results]
            assert all(isinstance(c, int) and c >= 0 for c in counts)
            assert len(set(counts)) == 1, f"Shared session returned differing counts: {counts}"
        finally:
            session.close()

    def test_session_mixed_reads_no_deadlock(self) -> None:
        session = TyO3Session(self.ROOT)
        try:
            def _mixed() -> bool:
                session.check()
                session.document_symbols("main.py")
                session.files()
                session.workspace_symbols("a")
                return True

            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                futures = [ex.submit(_mixed) for _ in range(8)]
                for f in futures:
                    assert f.result(timeout=30) is True
        finally:
            session.close()
```

**Verify Phase 1:**

```bash
devenv shell -- pytest src/tyo3/tests/test_concurrency.py \
  -k "TestReloadConcurrency or TestSessionThreadSafety" -v
# Expect: all green.
```

**Commit:**

```bash
git commit -am "test(concurrency): reload-under-load invariant + shared-session safety"
```

---

## 4. Phase 2 — error-path parity on snapshots

The snapshot read methods are a hand-duplicated copy of the session methods, and the
refactor moved `PyErr` construction out of the `detach` closure into
`AnalysisError::into_pyerr`. Nothing tests that a snapshot raises the *right* typed
exception. We assert **parity**: for the same bad input, the snapshot raises the *same*
exception type the session does. This is self-calibrating — it stays correct even if
the exact mapping is refined later.

```python
@needs_native
class TestSnapshotErrorParity:
    """A snapshot raises the SAME typed exception the session does for bad input."""

    ROOT = _fixture_path("simple_package")

    # Each case is a callable taking a handle (session or snapshot). The inputs are
    # chosen to trip a specific error path:
    #   - missing file        → PathResolutionError (resolved before any position work)
    #   - overflowing column  → OverflowError → PositionError (session.py maps it)
    ERROR_CASES = [
        ("check_file_missing", lambda h: h.check_file("definitely_missing_file.py")),
        ("document_symbols_missing", lambda h: h.document_symbols("definitely_missing_file.py")),
        ("semantic_tokens_missing", lambda h: h.semantic_tokens("definitely_missing_file.py")),
        ("file_occurrences_missing", lambda h: h.file_occurrences("definitely_missing_file.py")),
        ("goto_definition_missing", lambda h: h.goto_definition("definitely_missing_file.py", 1, 1)),
        ("goto_definition_overflow", lambda h: h.goto_definition("main.py", 1, 2**63)),
        ("find_references_overflow", lambda h: h.find_references("main.py", 1, 2**63)),
        ("hover_overflow", lambda h: h.hover("main.py", 1, 2**63)),
        ("type_hierarchy_overflow", lambda h: h.type_hierarchy("main.py", 1, 2**63)),
    ]

    @pytest.mark.parametrize("name,call", ERROR_CASES, ids=[c[0] for c in ERROR_CASES])
    def test_error_parity(self, name, call) -> None:
        session = TyO3Session(self.ROOT)
        try:
            snap = session.snapshot()
            try:
                session_exc = _capture_exc(lambda: call(session))
                snapshot_exc = _capture_exc(lambda: call(snap))
                assert session_exc is not None, f"{name}: session did not raise — fix the test input"
                assert snapshot_exc == session_exc, (
                    f"{name}: snapshot raised {snapshot_exc}, session raised {session_exc}"
                )
            finally:
                snap.close()
        finally:
            session.close()

    def test_read_on_closed_snapshot_parity(self) -> None:
        """Every read method on a closed snapshot raises ProjectClosedError."""
        session = TyO3Session(self.ROOT)
        try:
            snap = session.snapshot()
        finally:
            session.close()
        snap.close()
        for call in (
            lambda: snap.files(),
            lambda: snap.check(),
            lambda: snap.check_file("main.py"),
            lambda: snap.document_symbols("main.py"),
            lambda: snap.workspace_symbols("a"),
            lambda: snap.goto_definition("main.py", 1, 1),
            lambda: snap.find_references("main.py", 1, 1),
            lambda: snap.semantic_tokens("main.py"),
            lambda: snap.file_occurrences("main.py"),
            lambda: snap.hover("main.py", 1, 1),
            lambda: snap.type_hierarchy("main.py", 1, 1),
        ):
            with pytest.raises(ProjectClosedError):
                call()
```

> **If `test_error_parity` reports "session did not raise"** for a case, the input
> didn't trip the path you expected (e.g. ty clamped an out-of-range line instead of
> erroring). That's a *test-input* problem, not an implementation bug — adjust the
> input until the session raises, then confirm the snapshot matches. The `2**63`
> column cases are the reliable ones (they overflow the `u32` FFI boundary →
> `OverflowError`, which `session.py` maps to `PositionError`); keep at least one.

**Verify:**

```bash
devenv shell -- pytest src/tyo3/tests/test_concurrency.py::TestSnapshotErrorParity -v
# Expect: all parametrized cases green.
```

**Commit:**

```bash
git commit -am "test(concurrency): snapshot error-path parity with session"
```

---

## 5. Phase 3 — full session↔snapshot equivalence

Today only `check`, `document_symbols`, and `files` have parity assertions. Extend to
**all 13 reads** via one parametrized matrix, comparing normalized results with
`_dump`. Because the comparison is value-level, it catches DTO/conversion drift in the
duplicated snapshot wrappers regardless of return shape (model, list, or `None`).

```python
@needs_native
class TestSnapshotEquivalenceFull:
    """snapshot.<read>(...) == session.<read>(...) at the same revision, all methods."""

    ROOT = _fixture_path("simple_package")

    # (id, callable(handle)). Positions are safe defaults; (1,1) returns a (possibly
    # empty) list for navigation and None for hover/hierarchy — _dump compares either.
    READ_CALLS = [
        ("files", lambda h: h.files()),
        ("check", lambda h: h.check()),
        ("check_file", lambda h: h.check_file("main.py")),
        ("document_symbols", lambda h: h.document_symbols("main.py")),
        ("workspace_symbols", lambda h: h.workspace_symbols("a")),
        ("goto_definition", lambda h: h.goto_definition("main.py", 1, 1)),
        ("goto_declaration", lambda h: h.goto_declaration("main.py", 1, 1)),
        ("goto_type_definition", lambda h: h.goto_type_definition("main.py", 1, 1)),
        ("find_references", lambda h: h.find_references("main.py", 1, 1)),
        ("semantic_tokens", lambda h: h.semantic_tokens("main.py")),
        ("file_occurrences", lambda h: h.file_occurrences("main.py")),
        ("type_hierarchy", lambda h: h.type_hierarchy("main.py", 9, 7)),
        ("hover", lambda h: h.hover("main.py", 3, 10)),
    ]

    @pytest.mark.parametrize("name,call", READ_CALLS, ids=[c[0] for c in READ_CALLS])
    def test_read_parity(self, name, call) -> None:
        session = TyO3Session(self.ROOT)
        try:
            snap = session.snapshot()
            try:
                # No reload between the two calls → identical revision → identical result.
                assert _dump(call(session)) == _dump(call(snap)), f"{name}: session/snapshot mismatch"
            finally:
                snap.close()
        finally:
            session.close()
```

### 5.1 Exercise the `Some` branch of hover / type_hierarchy

The full-surface tests accept `None`, so the `Some → pythonize` branch is never
asserted. Find a position in `main.py` that yields a real hover and assert
session/snapshot agree on a non-`None` result. The probe makes the test robust to the
exact fixture layout.

```python
    def test_hover_some_branch_parity(self) -> None:
        """At a position with real hover info, snapshot matches session and is non-None."""
        session = TyO3Session(self.ROOT)
        try:
            snap = session.snapshot()
            try:
                hit = None
                # Probe a small grid; main.py is tiny. Stop at the first real hover.
                for line in range(1, 30):
                    for col in range(1, 40):
                        if session.hover("main.py", line, col) is not None:
                            hit = (line, col)
                            break
                    if hit:
                        break
                if hit is None:
                    pytest.skip("fixture yielded no hover anywhere in the probed grid")
                line, col = hit
                s = session.hover("main.py", line, col)
                p = snap.hover("main.py", line, col)
                assert p is not None
                assert _dump(s) == _dump(p)
            finally:
                snap.close()
        finally:
            session.close()
```

> If `test_hover_some_branch_parity` skips, widen the probe grid or pick a fixture you
> know has type info (e.g. a file with an annotated function). A skip is acceptable but
> a real assertion is better — prefer hardcoding a known-good `(line, col)` once you've
> found one with a throwaway `devenv shell -- pyrun` probe.

**Verify:**

```bash
devenv shell -- pytest src/tyo3/tests/test_concurrency.py::TestSnapshotEquivalenceFull -v
# Expect: 13 parametrized parity cases + the Some-branch test green (or 1 skip).
```

**Commit:**

```bash
git commit -am "test(concurrency): full session/snapshot read equivalence + hover Some branch"
```

---

## 6. Phase 4 — close racing in-flight reads

`close()` while reads are mid-flight is benign by design: a read that already cloned
its state finishes on the clone; a read that hasn't cloned yet sees `None` and raises
`ProjectClosedError`. The test asserts **no crash, no deadlock, no unexpected
exception** — `ProjectClosedError` is the *only* tolerated error.

```python
@needs_native
class TestCloseRace:
    """close() during concurrent reads: benign — ProjectClosedError only, no crash."""

    ROOT = _fixture_path("simple_package")

    def _race(self, make_handle_and_close):
        """make_handle_and_close() -> (handle, close_fn). Reads on `handle` run on
        threads while close_fn() is called from the main thread."""
        handle, close_fn = make_handle_and_close()
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(100):
                    handle.check()
            except ProjectClosedError:
                pass  # expected once close() lands
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        # Let a few reads start, then close out from under them.
        close_fn()
        for t in threads:
            t.join(timeout=30)
        assert not any(t.is_alive() for t in threads), "deadlock after close()"
        assert not errors, f"close() race produced non-benign errors: {errors}"

    def test_close_session_during_reads(self) -> None:
        def make():
            s = TyO3Session(self.ROOT)
            return s, s.close
        self._race(make)

    def test_close_snapshot_during_reads(self) -> None:
        def make():
            s = TyO3Session(self.ROOT)
            try:
                snap = s.snapshot()
            finally:
                s.close()
            return snap, snap.close
        self._race(make)
```

**Verify (run a few times — it's a race):**

```bash
for i in 1 2 3; do
  devenv shell -- pytest src/tyo3/tests/test_concurrency.py::TestCloseRace -v || break
done
# Expect: green every run. A hang (timeout) or a non-ProjectClosedError is a real bug.
```

**Commit:**

```bash
git commit -am "test(concurrency): close() racing in-flight reads is benign"
```

---

## 7. Phase 5 — multiple snapshots across multiple reloads

`TestSnapshotIsolation` proves one snapshot is pinned across one reload. This extends
it: three snapshots taken at three revisions stay independently pinned across two
reloads. Each snapshot sees exactly the symbols that existed when it was taken.

```python
@needs_native
class TestMultiSnapshotIsolation:
    """Independent snapshots stay pinned to their own revision across multiple reloads."""

    def test_snapshots_pinned_across_reloads(self, tmp_path: StdPath) -> None:
        project_root = tmp_path / "project"
        shutil.copytree(FIXTURES_DIR / "simple_package", project_root)
        main_py = project_root / "main.py"

        sym1 = "added_symbol_one"
        sym2 = "added_symbol_two"

        session = TyO3Session(project_root)
        snaps = []
        try:
            snap_a = session.snapshot()          # revision 0: neither symbol
            snaps.append(snap_a)

            main_py.write_text(main_py.read_text() + f"\n\ndef {sym1}() -> int:\n    return 1\n")
            session.reload()
            snap_b = session.snapshot()          # revision 1: sym1 only
            snaps.append(snap_b)

            main_py.write_text(main_py.read_text() + f"\n\ndef {sym2}() -> int:\n    return 2\n")
            session.reload()
            snap_c = session.snapshot()          # revision 2: sym1 + sym2
            snaps.append(snap_c)

            names_a = {s.name for s in snap_a.document_symbols("main.py")}
            names_b = {s.name for s in snap_b.document_symbols("main.py")}
            names_c = {s.name for s in snap_c.document_symbols("main.py")}

            assert sym1 not in names_a and sym2 not in names_a, "snap_a leaked a later edit"
            assert sym1 in names_b and sym2 not in names_b, "snap_b not pinned to revision 1"
            assert sym1 in names_c and sym2 in names_c, "snap_c missing a current symbol"
        finally:
            for s in snaps:
                s.close()
            session.close()
```

**Verify:**

```bash
devenv shell -- pytest src/tyo3/tests/test_concurrency.py::TestMultiSnapshotIsolation -v
```

**Commit:**

```bash
git commit -am "test(concurrency): multiple snapshots pinned across multiple reloads"
```

---

## 8. Phase 6 — optional hardening (do if time permits)

Lower-value; skip under time pressure. Keep each as its own small test.

### 8.1 Deeper cross-thread equality (not just counts)
`TestSnapshotThreadSafety` asserts equal diagnostic *counts*. Add one test that runs a
deterministic read (`document_symbols`) across threads on one snapshot and asserts the
**full** `_dump` results are byte-identical:

```python
    def test_snapshot_cross_thread_result_identity(self) -> None:
        session = TyO3Session(self.ROOT)
        try:
            snap = session.snapshot()
        finally:
            session.close()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
                dumps = [
                    _dump(f.result(timeout=30))
                    for f in [ex.submit(snap.document_symbols, "main.py") for _ in range(4)]
                ]
            assert all(d == dumps[0] for d in dumps), "snapshot read differs across threads"
        finally:
            snap.close()
```
(Add inside `TestSnapshotThreadSafety`.)

### 8.2 Second GIL-release proof via `check()`
`TestGilRelease` proves release only through `workspace_symbols`. Add a sibling test
using the control-vs-treatment harness from `OPTION_C…md` §8.2 with `check()` on
`demo_repos` as the treatment (one cold session per thread). Keep the **relative**
assertion `rust_speedup > control_speedup * 1.15` — never an absolute threshold. Mark
it `@pytest.mark.slow` if the suite distinguishes slow tests, since `check()` on 37
files is heavier than the symbol path.

**Commit (if done):**

```bash
git commit -am "test(concurrency): cross-thread result identity + second GIL-release proof"
```

---

## 9. Final gates

```bash
devenv shell -- pytest src/tyo3/tests/test_concurrency.py -v   # the whole concurrency file
devenv shell -- tests                                          # full suite + coverage
devenv shell -- ruff check src && devenv shell -- ruff format --check src
```

Then run the two race-y files a few extra times to shake out flakiness:

```bash
for i in 1 2 3 4 5; do
  devenv shell -- pytest src/tyo3/tests/test_concurrency.py \
    -k "TestReloadConcurrency or TestCloseRace" -q || { echo "FLAKE/BUG on run $i"; break; }
done
```

---

## 10. Definition of Done

- [ ] **Phase 0** helpers (`_dump`, `_capture_exc`, `threading` import) added.
- [ ] **Phase 1** `TestReloadConcurrency.test_reload_during_concurrent_reads` passes
      5/5 consecutive runs; `TestSessionThreadSafety` green.
- [ ] **Phase 2** `TestSnapshotErrorParity` green; at least one overflow case present;
      closed-snapshot parity covers all 13 reads.
- [ ] **Phase 3** `TestSnapshotEquivalenceFull` covers all 13 reads; hover `Some`-branch
      test asserts (or documents a justified skip).
- [ ] **Phase 4** `TestCloseRace` green for both session and snapshot across ≥3 runs;
      only `ProjectClosedError` tolerated.
- [ ] **Phase 5** `TestMultiSnapshotIsolation` green.
- [ ] **Phase 6** optional tests done or consciously skipped.
- [ ] Full suite + ruff green. No implementation file (`rust/src/**`, `session.py`)
      modified.
- [ ] **Any new test that fails was reported as a finding, not silenced.**

---

## 11. Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `test_reload_during_concurrent_reads` fails with `salsa::Cancelled` / `PyRuntimeError` / panic | **Real bug**: a read is not running on an isolated clone, or mutation isn't swap-only | **Stop. Report.** Do not weaken the assertion. Capture the `errors` list. |
| Reload test passes every time but you doubt it overlaps | Fixture too small → reads finish between reloads | Switch the readers to `demo_repos` + `check()` (heavier), or raise reader loop count / reloader count |
| `test_error_parity` "session did not raise" | Input didn't trip the error path (ty clamped it) | Adjust the input; the `2**63` column cases are reliable — keep one |
| `test_hover_some_branch_parity` skips | Probe grid missed real hover positions | Widen the grid or hardcode a known-good `(line, col)` found via a `pyrun` probe |
| A threaded test hangs | Deadlock (real) or `join`/`result` has no timeout | Ensure every `join`/`result` has a timeout; a timeout failure is the correct outcome for a deadlock — investigate the lock |
| `AttributeError: ... has no attribute 'snapshot'` | Stale `.so` shadowing | `devenv shell -- clean && devenv shell -- build` (see impl guide §3.3) |
| Equivalence mismatch on `semantic_tokens`/`file_occurrences` | Real DTO drift between the duplicated session and snapshot wrappers | Report — this is exactly the drift this phase exists to catch |
