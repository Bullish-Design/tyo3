# Phase 5 Implementation Guide - Concurrency proof

> Audience: an engineer who has finished **Phase 1**
> (`PHASE_1_IMPLEMENTATION_GUIDE.md`, `ContentStore` + `OverlaySystem`),
> **Phase 2** (`PHASE_2_IMPLEMENTATION_GUIDE.md`, the HEAD db built over the
> overlay via real discovery), **Phase 3**
> (`PHASE_3_IMPLEMENTATION_GUIDE.md`, the write path), and **Phase 4**
> (`PHASE_4_IMPLEMENTATION_GUIDE.md`, independent MVCC snapshots), and is now
> implementing **Phase 5** of `MVCC_SUBSTRATE_ARCHITECTURE.md`.
>
> Goal of Phase 5: prove the concurrency claim the whole architecture exists to
> satisfy. With many reader snapshots held open and actively querying, a hot
> writer must continue to apply edits to HEAD, snapshots must never surface
> `salsa::Cancelled`, and every snapshot must keep reading the revision it pinned.
>
> **Still out of scope:** the graph (`apply_delta`, reverse-dep index,
> `Snapshot.graph()` - **Phases 6-7**), the file watcher (**Phase 8**), the
> floating warm cancel-retry `session.check()` fast path (**Phase 9**), benchmarks
> intended to compare implementation strategies (**Phase 10**). Phase 5 may add
> test helpers and narrowly scoped hardening if a stress test exposes a real race,
> but it should not add a new public feature.
>
> When you finish: the project compiles, the full existing suite still passes, and
> new Rust + Python stress tests prove:
>
> 1. Holding K snapshots open does not block `edit` / `apply_changes`.
> 2. Snapshot reads continue successfully while the writer hammers HEAD.
> 3. Snapshot results remain pinned to their revision while HEAD advances.
> 4. Read-once disk capture is race-safe under concurrent snapshot reads.
> 5. Hang-prone Python liveness tests run in a subprocess with an external timeout
>    so a regression fails CI instead of freezing it.

---

## 0. Mental model (read this first)

Phase 4 built the mechanism:

```text
HEAD db                 snapshot db @ R
ProjectDatabase         ProjectDatabase
over live OverlaySystem over frozen OverlaySystem
Zalsa A                 Zalsa B
mutated by apply_changes never mutated
```

Phase 5 is the proof that the mechanism stayed intact under real concurrency.
The tests should fail if any future refactor accidentally reintroduces one of the
old shapes:

- `snapshot()` returns `head.db.clone()` again.
- `snapshot()` builds a fresh db but still shares the live overlay content cell.
- `snapshot()` or a session read holds the head mutex while doing cold discovery.
- frozen disk capture assigns inconsistent file revisions under racing reads.
- session-level cache invalidation closes a snapshot while another thread is using
  it.

The first failure mode is the most important. If a snapshot shares HEAD's `Zalsa`,
then a writer calling `head.db.apply_changes(...)` enters salsa's
`cancel_others`, waits for the shared clone count to drop to 1, and blocks while
the snapshot is alive. A normal unit test that calls `edit()` directly would hang
forever. Phase 5 tests must therefore be **bounded**: run the writer in a worker
and observe completion through a timeout; for Python, run the whole scenario in a
child process that the parent can kill.

### What "unaffected throughput" means in CI

Do not write a brittle microbenchmark that fails because the CI machine is slow.
The load-bearing assertion is liveness: with many snapshots held open, a bounded
number of warm edits completes before a generous deadline. Record elapsed times in
the test output / PR notes, but keep pass/fail thresholds coarse enough to detect
"blocked forever" and pathological serialization, not normal machine variance.

Recommended defaults:

- Rust liveness: 32 snapshots, 100 edits, 5 second deadline.
- Rust active-reader stress: 8 reader threads, 100 writer edits, 10 second
  deadline.
- Python subprocess liveness: 8 snapshots, 50 edits, 15 second subprocess timeout.

Make the counts configurable through environment variables if local debugging
needs a larger run:

```text
TYO3_MVCC_STRESS_SNAPSHOTS
TYO3_MVCC_STRESS_READERS
TYO3_MVCC_STRESS_EDITS
```

Keep the defaults modest. This is a correctness gate, not a benchmark suite.

---

## 1. Prerequisite check

Phase 5 assumes Phases 1-4 are merged/working. Run the baseline before adding
stress tests:

```bash
devenv shell -- check-rust
devenv shell -- test-rust
devenv shell -- tests
```

You rely on:

- `build_head(root, ContentStore::new()) -> HeadState` from Phase 2.
- Phase-3 helpers such as `classify_overlay_edit`, `commit_head` or the equivalent
  private write-path helpers in `project.rs`.
- Phase-4 `build_frozen(root, generation, rev) -> TyProjectState`.
- `PyTyProject.snapshot(at=None)` returning a native `PySnapshot` with an
  independent db and a `revision` getter.
- Python `TyO3Session.snapshot(at=None)` returning a `Snapshot` wrapper, and
  session writes invalidating the cached head snapshot.

If any Phase-4 test still contains "snapshot must be closed before edit", remove
or invert it before adding this phase. After Phase 4, that rule is false.

---

## 2. Test utility pattern: bounded workers

### 2.1 Rust: channel + timeout

Any Rust test that could expose the old `cancel_others` deadlock must run the
writer in a thread and wait on a channel:

```rust
use std::sync::mpsc;
use std::time::Duration;

let (tx, rx) = mpsc::channel();
std::thread::spawn(move || {
    // Do the potentially-blocking writes here.
    let result = run_writer();
    let _ = tx.send(result);
});

let result = rx.recv_timeout(Duration::from_secs(5)).expect(
    "writer did not finish while snapshots were held open; \
     this usually means snapshot() shares the HEAD Zalsa again",
);
result.expect("writer failed");
```

If the regression is present, the spawned thread may remain blocked after the
test panics. That is acceptable for a libtest process: the important part is that
the test fails instead of hanging the whole suite. Do not `join()` a thread after
`recv_timeout` has already failed.

### 2.2 Python: subprocess timeout, not in-process thread timeout

Python liveness tests must use `subprocess.run(..., timeout=...)`.

Reason: Phase-3 writes deliberately hold the GIL while calling `apply_changes`.
If a buggy writer blocks inside Rust while holding the GIL, the main Python test
thread may not regain the GIL to notice a `thread.join(timeout=...)` has expired.
An external process timeout is the reliable kill switch.

The parent test should:

1. Write a small Python stress script into `tmp_path`.
2. Run it with `sys.executable` and `timeout=15` (or slightly higher on slow CI).
3. On failure, include the child process stdout/stderr in the assertion message.

Do not put a possible writer-deadlock scenario directly in the pytest process.

---

## 3. Rust stress tests (`rust/src/project.rs`)

Put these under a new `#[cfg(test)] mod phase5_concurrency_tests` in
`rust/src/project.rs`. Keeping them in `project.rs` lets the tests use private
helpers (`build_head`, `build_frozen`, write-path classification helpers) without
making test-only APIs public.

### 3.1 Shared fixture and edit helper

Start with a small project and a helper that performs the same write-path ordering
as production: classify -> mutate store -> publish -> apply.

```rust
#[cfg(test)]
mod phase5_concurrency_tests {
    use super::*;
    use ruff_db::source::source_text;
    use std::io::Write;
    use std::panic::{catch_unwind, AssertUnwindSafe};
    use std::sync::atomic::{AtomicBool, Ordering};
    use std::sync::{mpsc, Arc, Barrier};
    use std::time::{Duration, Instant};

    fn stress_count(name: &str, default: usize) -> usize {
        std::env::var(name)
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(default)
    }

    fn project(a_py: &str) -> (tempfile::TempDir, SystemPathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let mut f = std::fs::File::create(dir.path().join("a.py")).unwrap();
        f.write_all(a_py.as_bytes()).unwrap();
        let root =
            SystemPathBuf::from_path_buf(dir.path().canonicalize().unwrap()).unwrap();
        (dir, root)
    }

    fn read_source(state: &TyProjectState, path: &SystemPathBuf) -> String {
        let file = ruff_db::files::system_path_to_file(&state.db, path).unwrap();
        source_text(&state.db, file).as_str().to_string()
    }

    fn apply_overlay_edit(head: &mut HeadState, path: &SystemPathBuf, text: String) {
        let event = classify_overlay_edit(&head.db, path);
        head.store.insert_text(path.clone(), text);
        head.system.publish(head.store.capture());
        head.db.apply_changes(std::slice::from_ref(&event), None);
    }
}
```

Adjust `apply_overlay_edit` if Phase 3 factored the production commit helper
differently. The invariant is the ordering, not the exact helper names.

### 3.2 Held snapshots do not block the writer

This is the direct proof of architecture §0. Build many frozen snapshots, keep
them alive, move the head into a writer thread, and assert the writer finishes.
If `build_frozen` accidentally becomes `head.db.clone()`, this test times out.

```rust
#[test]
fn held_snapshots_do_not_block_writer() {
    let (_dir, root) = project("x: int = 0\n");
    let mut head = build_head(root.clone(), ContentStore::new());
    let a = root.join("a.py");

    let snapshot_count = stress_count("TYO3_MVCC_STRESS_SNAPSHOTS", 32);
    let edit_count = stress_count("TYO3_MVCC_STRESS_EDITS", 100);

    let snapshots: Vec<TyProjectState> = (0..snapshot_count)
        .map(|_| build_frozen(root.clone(), head.store.capture(), head.store.revision()))
        .collect();

    // Force each snapshot to do real work before the writer starts. This makes
    // the test catch both "held but idle clone" and "active snapshot db" bugs.
    for snap in &snapshots {
        assert!(read_source(snap, &a).contains("x: int = 0"));
    }

    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let start = Instant::now();
        for i in 1..=edit_count {
            apply_overlay_edit(&mut head, &a, format!("x: int = {i}\n"));
        }
        let _ = tx.send((head.store.revision().0, start.elapsed()));
    });

    let (rev, elapsed) = rx.recv_timeout(Duration::from_secs(5)).expect(
        "writer did not finish while snapshots were held open; \
         snapshot() likely shares the HEAD Zalsa or holds the head lock too long",
    );

    assert!(rev >= edit_count as u64);
    eprintln!(
        "held_snapshots_do_not_block_writer: snapshots={snapshot_count}, \
         edits={edit_count}, elapsed={elapsed:?}"
    );

    // Keep the snapshots alive until after the writer has completed.
    assert_eq!(snapshots.len(), snapshot_count);
}
```

Do not make the elapsed time assertion tight. The timeout is the assertion.

### 3.3 Snapshot readers never cancel during hot writes

This test runs active snapshot queries while the writer mutates HEAD. Any panic or
error from a snapshot read is a failure. If a snapshot somehow shares a mutable
Zalsa again, readers may see `salsa::Cancelled` under concurrent writes.

```rust
#[test]
fn snapshot_reads_never_cancel_while_writer_hammers_head() {
    let (_dir, root) = project("x: int = 0\n");
    let mut head = build_head(root.clone(), ContentStore::new());
    let a = root.join("a.py");

    let reader_count = stress_count("TYO3_MVCC_STRESS_READERS", 8);
    let edit_count = stress_count("TYO3_MVCC_STRESS_EDITS", 100);

    let snapshots: Vec<TyProjectState> = (0..reader_count)
        .map(|_| build_frozen(root.clone(), head.store.capture(), head.store.revision()))
        .collect();

    let barrier = Arc::new(Barrier::new(reader_count + 1));
    let stop = Arc::new(AtomicBool::new(false));
    let (err_tx, err_rx) = mpsc::channel::<String>();
    let (iter_tx, iter_rx) = mpsc::channel::<usize>();

    for (idx, snap) in snapshots.into_iter().enumerate() {
        let barrier = Arc::clone(&barrier);
        let stop = Arc::clone(&stop);
        let err_tx = err_tx.clone();
        let iter_tx = iter_tx.clone();
        let a = a.clone();
        std::thread::spawn(move || {
            barrier.wait();
            let mut iterations = 0usize;
            while !stop.load(Ordering::Relaxed) {
                let result = catch_unwind(AssertUnwindSafe(|| {
                    let text = read_source(&snap, &a);
                    assert!(
                        text.contains("x: int = 0"),
                        "snapshot reader {idx} observed unpinned content: {text:?}"
                    );
                    // Exercise semantic queries too; source_text alone does not
                    // cover as much salsa state as a real read surface.
                    let _ = snap.db.check();
                }));

                if result.is_err() {
                    let _ = err_tx.send(format!(
                        "snapshot reader {idx} panicked; possible cancellation"
                    ));
                    break;
                }
                iterations += 1;
            }
            let _ = iter_tx.send(iterations);
        });
    }
    drop(err_tx);
    drop(iter_tx);

    let (done_tx, done_rx) = mpsc::channel();
    barrier.wait();
    std::thread::spawn(move || {
        for i in 1..=edit_count {
            let text = if i % 2 == 0 {
                format!("x: int = {i}\n")
            } else {
                "x: int = 'bad'\n".to_string()
            };
            apply_overlay_edit(&mut head, &a, text);
        }
        let _ = done_tx.send(head.store.revision().0);
    });

    let final_rev = done_rx.recv_timeout(Duration::from_secs(10)).expect(
        "writer did not finish during active snapshot reads",
    );
    stop.store(true, Ordering::Relaxed);

    let mut total_iterations = 0usize;
    for _ in 0..reader_count {
        total_iterations += iter_rx
            .recv_timeout(Duration::from_secs(5))
            .expect("snapshot reader did not stop after writer completed");
    }

    let errors: Vec<String> = err_rx.try_iter().collect();
    assert!(errors.is_empty(), "snapshot read errors: {errors:?}");

    assert!(final_rev >= edit_count as u64);
    assert!(
        total_iterations > 0,
        "reader threads did not perform any snapshot reads"
    );
}
```

If `ProjectDatabase::check()` has a different public method name in this pin, use
the same semantic read helper that Phase-2/Phase-4 tests used. The important part
is that readers exercise real salsa queries while the writer mutates a different
db.

### 3.4 Concurrent first read of a frozen disk file is race-safe

Phase 4's `capture_disk_file` uses `ArcSwap::rcu` and a shared
`capture_version`. This test makes many clones of one snapshot read the same
uncaptured disk-backed file at once. The expected result is boring: every reader
gets the same content, and no reader panics.

```rust
#[test]
fn frozen_read_once_capture_is_race_safe() {
    let (_dir, root) = project("CAPTURED = 1\n");
    let head = build_head(root.clone(), ContentStore::new());
    let a = root.join("a.py");
    let snap = build_frozen(root.clone(), head.store.capture(), head.store.revision());

    let reader_count = stress_count("TYO3_MVCC_STRESS_READERS", 16);
    let barrier = Arc::new(Barrier::new(reader_count));
    let (tx, rx) = mpsc::channel();

    for _ in 0..reader_count {
        let snap_clone = snap.read_clone();
        let barrier = Arc::clone(&barrier);
        let tx = tx.clone();
        let a = a.clone();
        std::thread::spawn(move || {
            barrier.wait();
            let result = catch_unwind(AssertUnwindSafe(|| read_source(&snap_clone, &a)));
            let _ = tx.send(result.map_err(|_| "panic during frozen capture".to_string()));
        });
    }
    drop(tx);

    let mut texts = Vec::new();
    for _ in 0..reader_count {
        let result = rx
            .recv_timeout(Duration::from_secs(5))
            .expect("frozen capture reader did not finish");
        texts.push(result.expect("frozen capture reader panicked"));
    }

    assert!(texts.iter().all(|t| t.contains("CAPTURED = 1")));
    assert!(
        texts.windows(2).all(|w| w[0] == w[1]),
        "all concurrent first reads should observe identical captured content"
    );
}
```

If `TyProjectState` does not implement `ReadCloneSource` publicly enough for the
test module, clone the fields directly in the test:

```rust
let snap_clone = TyProjectState { db: snap.db.clone(), root: snap.root.clone() };
```

That clone shares the frozen snapshot's Zalsa, which is allowed because it is
never mutated.

---

## 4. Python subprocess stress tests

Add `src/tyo3/tests/test_mvcc_concurrency.py`. These tests exercise the public
Python API and protect against GIL-level deadlocks that Rust-only tests cannot see.

### 4.1 Subprocess helper

```python
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path


def _run_child(tmp_path: Path, script: str, *, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    child = tmp_path / "mvcc_stress_child.py"
    child.write_text(textwrap.dedent(script))

    env = os.environ.copy()
    # Preserve the test runner's import environment. The package is already
    # importable when the parent suite is running; this just avoids cwd surprises.
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in [env.get("PYTHONPATH", ""), str(Path.cwd())] if p
    )

    return subprocess.run(
        [sys.executable, str(child)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _assert_child_ok(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, (
        f"child failed with exit code {result.returncode}\n"
        f"STDOUT:\n{result.stdout}\n"
        f"STDERR:\n{result.stderr}"
    )
```

If the existing suite already has a subprocess helper, reuse it instead. Keep the
external timeout.

### 4.2 Open snapshots do not block edits

```python
def test_open_snapshots_do_not_block_edits_subprocess(tmp_path):
    result = _run_child(
        tmp_path,
        """
        import time
        from tyo3 import TyO3Session

        root = "."
        open("a.py", "w").write("x: int = 0\\n")

        with TyO3Session(root) as s:
            snapshots = [s.snapshot() for _ in range(8)]
            for snap in snapshots:
                snap.check()

            start = time.monotonic()
            for i in range(50):
                s.edit("a.py", f"x: int = {i}\\n")
            elapsed = time.monotonic() - start

            # The subprocess timeout is the real deadlock guard. This assertion
            # catches pathological but non-deadlocked serialization.
            assert elapsed < 10, elapsed

            for snap in snapshots:
                snap.check()
                snap.close()
        """,
        timeout=15.0,
    )
    _assert_child_ok(result)
```

If this test times out, suspect `snapshot()` sharing HEAD's `Zalsa` or a head lock
being held across snapshot construction.

### 4.3 Active snapshot readers survive hot writes

```python
def test_snapshot_readers_survive_hot_writer_subprocess(tmp_path):
    result = _run_child(
        tmp_path,
        """
        import threading
        import time
        from tyo3 import TyO3Session

        open("a.py", "w").write("x: int = 0\\n")
        errors = []
        stop = threading.Event()

        with TyO3Session(".") as s:
            snapshots = [s.snapshot() for _ in range(6)]
            expected_revisions = [snap.revision for snap in snapshots]

            def reader(snap, expected_revision):
                try:
                    while not stop.is_set():
                        assert snap.revision == expected_revision
                        snap.check()
                except BaseException as exc:
                    errors.append(repr(exc))
                    stop.set()

            threads = [
                threading.Thread(target=reader, args=(snap, rev), daemon=True)
                for snap, rev in zip(snapshots, expected_revisions)
            ]
            for thread in threads:
                thread.start()

            for i in range(75):
                if i % 2:
                    s.edit("a.py", "x: int = 'bad'\\n")
                else:
                    s.edit("a.py", f"x: int = {i}\\n")

            stop.set()
            for thread in threads:
                thread.join(timeout=2)

            assert not errors, errors
            for snap in snapshots:
                snap.close()
        """,
        timeout=20.0,
    )
    _assert_child_ok(result)
```

The child process timeout remains the outer safety net. The in-child
`thread.join(timeout=2)` is only a cleanup convenience after the writer has
completed.

### 4.4 Snapshot diagnostics stay pinned while HEAD changes

Use a deterministic semantic assertion, not just "no crash". The exact diagnostic
shape varies across the suite, so adapt `_num_diags` to the existing test helpers.

```python
def test_snapshot_diagnostics_are_pinned_under_concurrent_edits_subprocess(tmp_path):
    result = _run_child(
        tmp_path,
        """
        import threading
        from tyo3 import TyO3Session

        def num_diags(result):
            # Replace this with the same helper used by the existing tests if
            # check() returns a structured model instead of a plain sequence.
            try:
                return len(result)
            except TypeError:
                return len(getattr(result, "diagnostics", []))

        open("a.py", "w").write("x: int = 0\\n")

        with TyO3Session(".") as s:
            snap = s.snapshot()
            expected = num_diags(snap.check())

            def writer():
                for _ in range(30):
                    s.edit("a.py", "x: int = 'bad'\\n")
                    s.edit("a.py", "x: int = 1\\n")

            thread = threading.Thread(target=writer, daemon=True)
            thread.start()

            for _ in range(30):
                assert num_diags(snap.check()) == expected

            thread.join(timeout=5)
            assert not thread.is_alive(), "writer did not finish"
            snap.close()
        """,
        timeout=15.0,
    )
    _assert_child_ok(result)
```

If sharing a single `TyO3Session` across threads exposes a Python-level cache race
that is not part of the explicit snapshot contract, reduce this test to use an
explicit writer thread plus explicit snapshots only. Do not obscure the core
Phase-5 proof with session-convenience cache behavior unless the product intends
`TyO3Session` itself to be thread-safe.

---

## 5. Optional hardening if tests expose a race

Phase 5 is primarily tests. Only make production changes if the new tests expose a
real concurrency bug. Keep fixes narrow and document them in the PR.

### 5.1 If cached head snapshots can be closed while in use

Phase 4's Python cache invalidation closes the cached native snapshot immediately:

```python
snap, self._head_snap = self._head_snap, None
if snap is not None:
    snap.close()
```

That is fine for single-threaded use. If Phase-5 tests intentionally share one
`TyO3Session` across reader/writer threads and a read can race with invalidation,
prefer this safer pattern:

```python
def _invalidate_head_snap(self) -> None:
    # Drop the cache reference, but do not forcibly close the old native snapshot.
    # Any in-flight read that already grabbed it may finish; Python will drop it
    # when the last reference is gone.
    self._head_snap = None
```

Then keep explicit `Snapshot.close()` behavior unchanged. This trades prompt cache
cleanup for thread safety. Only make this change if the API is meant to support
shared-session concurrent reads.

### 5.2 If snapshot construction still holds the head lock too long

Phase 4's `snapshot(at)` should capture `(root, generation, rev)` under
`Mutex<Head>` and then drop the guard before `build_frozen`. If the writer
liveness test shows long stalls but not a Zalsa deadlock, inspect this first.

The critical shape is:

```rust
let guard = lock_state(&self.inner, "snapshot")?;
let head = guard.as_ref().unwrap();
let root = head.root.clone();
let generation = head.store.capture();
let rev = head.store.revision();
drop(guard);

let state = build_frozen(root, generation, rev);
```

Do not run discovery or `ProjectDatabase::fallible` under the head lock.

### 5.3 If frozen capture panics or produces inconsistent revisions

Re-check Phase 4 `OverlaySystem`:

- `capture_version` must be `Arc<AtomicU64>`, shared by clones.
- `capture_disk_file` must first check `self.document(path)`.
- the `ArcSwap::rcu` closure must not overwrite a path already inserted by a
  racing capture.
- `path_metadata` and `read_to_string` must both call `capture_disk_file` so they
  agree on the `Document::version()`.

Do not solve this by serializing all snapshot reads with a global mutex. That
would hide the race and destroy the parallel-reader property Phase 5 is proving.

---

## 6. Build, test, iterate

Run the targeted stress first:

```bash
devenv shell -- test-rust
devenv shell -- pytest src/tyo3/tests/test_mvcc_concurrency.py -q -s
```

Then run the full gates:

```bash
devenv shell -- check-rust
devenv shell -- tests
```

If a stress test times out, keep the failure artifact useful:

- Rust: the panic message should name the likely invariant (`snapshot()` shares
  HEAD Zalsa, head lock held too long, frozen capture race).
- Python: `_assert_child_ok` should print stdout/stderr from the child process.
- Include observed elapsed timings in the PR notes, but do not tune thresholds
  down to local-machine performance.

---

## 7. Gotchas & decisions (read before you debug)

1. **Do not use HEAD read clones in Phase-5 stress tests.** Phase 9 owns the
   floating warm read path and its cancel-retry behavior. Phase 5 stresses
   independent snapshots. If a test clones HEAD and reads while writing, a
   cancellation is expected, not a Phase-5 failure.

2. **Python deadlock tests need subprocesses.** A blocked PyO3 write can hold the
   GIL, preventing in-process timeout code from running. Use
   `subprocess.run(..., timeout=...)`.

3. **Keep stress sizes bounded.** Larger local runs are useful, but CI defaults
   should be small enough to run on every PR. This phase is a correctness gate.

4. **Do not assert exact diagnostics unless existing tests already do.** ty's
   diagnostic wording and rule defaults may change. Prefer count stability for
   pinned snapshots and "head changed relative to snapshot" assertions.

5. **A timeout is a valid failure.** The old bug is a hang, so the timeout is the
   test signal. Do not mask it with retries.

6. **Dropped snapshot cache references are okay.** A live explicit `Snapshot`
   pins its generation. The session's cached head snapshot is only convenience
   sugar; invalidating it must never affect explicit snapshots.

7. **Do not add sleeps as synchronization.** Use `Barrier`, `AtomicBool`, and
   channels in Rust; use `threading.Event` and subprocess timeouts in Python.
   Sleeps make the tests slower and less deterministic.

8. **Rust `catch_unwind` only guards panics.** If a read returns a normal error,
   assert on that error too. The snippets above use direct assertions because the
   current helpers panic on unexpected failures; adapt if the local helpers return
   `Result`.

9. **One blocked spawned Rust thread may survive a failed test.** That is better
   than hanging libtest. Keep such tests few and targeted.

10. **No graph assertions yet.** `SyncResult.rescan` and path deltas are for
    Phase 6. Phase 5 only proves the db/snapshot substrate under concurrency.

---

## 8. Explicitly OUT of scope for Phase 5

- Implementing `CodeGraph.apply_delta`, reverse-dependency tracking, graph diffing,
  or `Snapshot.graph()` (Phases 6-7).
- Wiring `ProjectWatcher` / `poll_changes()` as a change source (Phase 8).
- Adding the warm floating `session.check()` fast path or cancel-retry loop
  (Phase 9).
- Benchmarking cold snapshot construction, graph update speed, or memory retention
  beyond the coarse liveness timings emitted by these tests (Phase 10).
- Changing the MVCC consistency model or sharing salsa storage across revisions.
- Making notebook overlay content work.
- Surfacing retention-cap configuration.

If you find yourself touching `src/tyo3/graph/`, adding rustworkx assertions,
implementing watcher polling, or adding retry-on-cancel around HEAD reads, stop -
you have left Phase 5.

---

## 9. Definition of Done

- [ ] Rust stress tests added under `project.rs` (or an equivalent Rust test
      module with access to private helpers):
      `held_snapshots_do_not_block_writer`,
      `snapshot_reads_never_cancel_while_writer_hammers_head`,
      `frozen_read_once_capture_is_race_safe`.
- [ ] Rust liveness tests use channel timeouts and do not `join()` a writer after
      a timeout failure.
- [ ] Python stress tests added in `src/tyo3/tests/test_mvcc_concurrency.py` and
      all hang-prone scenarios run in a subprocess with `timeout=...`.
- [ ] Python tests cover open snapshots during edits, active snapshot readers
      during hot writes, and pinned snapshot diagnostics while HEAD changes.
- [ ] Any production hardening required by the stress tests is narrowly scoped and
      documented (for example, safe cached-head-snapshot invalidation).
- [ ] `devenv shell -- test-rust` passes.
- [ ] `devenv shell -- pytest src/tyo3/tests/test_mvcc_concurrency.py -q -s`
      passes.
- [ ] `devenv shell -- tests` shows no regressions.
- [ ] `devenv shell -- check-rust` clean.
- [ ] PR description includes stress parameters used, elapsed timings observed,
      and whether any production code was changed beyond tests.

---

## 10. How this seeds Phase 6+

After Phase 5, the MVCC substrate has been tested under the concurrency pattern it
was designed for: many readers pinned to stable revisions while one writer moves
HEAD forward. That is the safety net Phase 6 needs before making the graph
incremental:

- **Phase 6** can consume `SyncResult` and update the HEAD graph knowing that
  snapshot readers will not block writer-side graph mutation.
- **Phase 7** can add `Snapshot.graph()` as a copy-on-pin graph view paired with
  the already-proven `Snapshot` db revision.
- **Phase 8** can feed watcher events into the same write path with confidence
  that long-lived agent snapshots will not stall disk-ingest writes.
- **Phase 9** can add an explicitly floating warm read path as an optimization,
  without weakening the proven snapshot read surface.

Keep the Phase-5 tests in CI permanently. They are the regression guard for the
single most important architectural invariant: snapshots are independent
per-revision databases, not clones of HEAD.
