"""Phase 9: Floating warm read fast path — correctness tests."""

from __future__ import annotations

import subprocess
import sys
import textwrap

from tyo3 import TyO3Session


def test_latest_reflects_newest_head(tmp_path):
    """Floating reads see the newest HEAD, while held snapshots stay pinned."""
    (tmp_path / "a.py").write_text("x: int = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        snap = s.snapshot()  # pinned at r0
        try:
            r0_syms = {sym.name for sym in snap.document_symbols("a.py")}
            assert "x" in r0_syms

            s.edit("a.py", "x: int = 1\ny: int = 2\n")  # HEAD advances

            latest_syms = {sym.name for sym in s.latest.document_symbols("a.py")}
            assert "y" in latest_syms  # floating sees the edit

            # the held snapshot is STILL pinned:
            assert {sym.name for sym in snap.document_symbols("a.py")} == r0_syms
            assert "y" not in r0_syms
        finally:
            snap.close()


def test_latest_matches_pinned_when_quiescent(tmp_path):
    """When no concurrent writes, floating and pinned describe the same state."""
    (tmp_path / "a.py").write_text("x: int = 'bad'\n")  # a type error
    with TyO3Session(str(tmp_path)) as s:
        latest = s.latest.check()
        with s.snapshot() as snap:
            pinned = snap.check()
        assert len(latest.diagnostics) == len(pinned.diagnostics)


def test_latest_view_pins_nothing(tmp_path):
    """Holding a LatestView must not block edits (it pins no revision/clone)."""
    (tmp_path / "a.py").write_text("x = 0\n")
    with TyO3Session(str(tmp_path)) as s:
        view = s.latest
        view.check()  # warm it
        for i in range(20):
            s.edit("a.py", f"x = {i}\n")  # must not hang/deadlock
        assert "x" in {sym.name for sym in view.document_symbols("a.py")}


def test_latest_basic_reads_work(tmp_path):
    """Basic smoke test: latest.check() and latest.document_symbols() work."""
    (tmp_path / "a.py").write_text("def hello(): return 42\n")
    with TyO3Session(str(tmp_path)) as s:
        result = s.latest.check()
        assert result is not None
        syms = s.latest.document_symbols("a.py")
        names = {sym.name for sym in syms}
        assert "hello" in names


def test_latest_workspace_symbols(tmp_path):
    (tmp_path / "a.py").write_text("class FooBar: pass\n")
    with TyO3Session(str(tmp_path)) as s:
        results = s.latest.workspace_symbols("FooBar")
        assert len(results) >= 1
        assert results[0].name == "FooBar"


def test_latest_goto_definition(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\ny = x\n")
    with TyO3Session(str(tmp_path)) as s:
        # goto_definition from y=x on position of x
        targets = s.latest.goto_definition("a.py", 2, 5)
        assert len(targets) >= 1


def test_latest_files(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    with TyO3Session(str(tmp_path)) as s:
        files = s.latest.files()
        assert any("a.py" in str(f) for f in files)


# ── Subprocess hot-writer test ───────────────────────────────────────────


def _run_child(tmp_path, code, *, timeout=20.0):
    """Run code in a subprocess with tmp_path as cwd. Returns CompletedProcess."""
    script = tmp_path / "_test_script.py"
    script.write_text(textwrap.dedent(code))
    return subprocess.run(
        [sys.executable, str(script)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _assert_child_ok(result):
    assert result.returncode == 0, f"Child failed:\nstdout={result.stdout}\nstderr={result.stderr}"


def test_latest_reads_survive_hot_writer_subprocess(tmp_path):
    """Floating reads survive concurrent writes — no salsa::Cancelled leaks."""
    result = _run_child(
        tmp_path,
        """
        import threading
        from tyo3 import TyO3Session

        open("a.py", "w").write("x: int = 0\\n")
        errors = []
        stop = threading.Event()

        with TyO3Session(".") as s:
            latest = s.latest

            def reader():
                try:
                    while not stop.is_set():
                        latest.check()                  # floating, warm, retried
                        latest.document_symbols("a.py")
                except BaseException as exc:
                    errors.append(repr(exc))
                    stop.set()

            threads = [threading.Thread(target=reader, daemon=True) for _ in range(2)]
            for t in threads:
                t.start()

            for i in range(30):
                s.edit("a.py", f"x: int = {i}\\n" if i % 2 else "x: int = 'bad'\\n")

            stop.set()
            for t in threads:
                t.join(timeout=3)
            assert not errors, errors
        """,
        timeout=20.0,
    )
    _assert_child_ok(result)
