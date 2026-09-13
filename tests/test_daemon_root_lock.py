"""Exactly one daemon may own a project root.

``_bind`` used to unlink any socket file it found before binding, on the
assumption that one could only be a leftover from a crash. A *live* second
daemon therefore replaced the first one's socket silently: the first kept
running, unreachable, while both held writable sessions over the same
``.tyo3/`` sidecar and interleaved writes to one identity registry.

The lock is an ``flock`` on a sibling ``.lock`` file. The kernel drops it when
the holder exits — cleanly, by signal, or by crash — so a stale lock cannot
exist and no recovery path is needed.
"""

import os
import tempfile
from pathlib import Path

import pytest

from tyo3.daemon.server import DaemonServer
from tyo3.exceptions import DaemonAlreadyRunning


def _project(root: Path) -> str:
    (root / "pyproject.toml").write_text("[project]\nname = 'test'\nversion = '0.1.0'\n")
    (root / "a.py").write_text("X = 1\n")
    return str(root)


def test_second_daemon_on_the_same_root_is_refused():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        proj = _project(root)
        sock = root / "run" / "t.sock"
        first = DaemonServer(proj, socket_path=sock)
        second = DaemonServer(proj, socket_path=sock)

        first._acquire_lock()
        try:
            with pytest.raises(DaemonAlreadyRunning) as excinfo:
                second._acquire_lock()
            assert excinfo.value.pid == os.getpid(), "the error names the holder"
        finally:
            first._release_lock()


def test_the_root_is_claimable_again_once_released():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        proj = _project(root)
        sock = root / "run" / "t.sock"

        first = DaemonServer(proj, socket_path=sock)
        first._acquire_lock()
        first._release_lock()

        second = DaemonServer(proj, socket_path=sock)
        second._acquire_lock()  # must not raise
        second._release_lock()


def test_distinct_roots_do_not_contend():
    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        a = DaemonServer(_project(Path(d1)), socket_path=Path(d1) / "a.sock")
        b = DaemonServer(_project(Path(d2)), socket_path=Path(d2) / "b.sock")
        a._acquire_lock()
        try:
            b._acquire_lock()  # different root, different lock
            b._release_lock()
        finally:
            a._release_lock()


def test_lock_file_is_owner_only():
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        server = DaemonServer(_project(root), socket_path=root / "run" / "t.sock")
        server._acquire_lock()
        try:
            assert server._lock_path.stat().st_mode & 0o777 == 0o600
        finally:
            server._release_lock()
