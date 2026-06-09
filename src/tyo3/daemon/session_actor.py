"""SessionActor — single-threaded owner of one ``TyO3Session``.

The session must never be mutated from two threads (the salsa/MVCC constraint —
see ``test_concurrency.py`` / ``test_mvcc_concurrency.py``). So the daemon owns
exactly one session on **one** dedicated thread and funnels *every* session call
— reads and writes alike — through it via :meth:`submit`. Client-handler threads
and RPC handlers never touch the session directly; they hand it a closure and
block for the result.

The bus pump (a separate worker) does *not* go through the actor for delivery:
it calls :meth:`subscribe` once (through the actor, so the lazy bus build is
serialised) and then polls the returned :class:`Subscription`, whose queue is
already thread-safe. Only the subscribe call is a session mutation.

Lifecycle: :meth:`start` opens the session and runs the initial ``sync_all`` on
the actor thread, then blocks until the project is indexed (or surfaces the open
error). :meth:`stop` drains the queue and closes the session on the same thread.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tyo3 import TyO3Session

# Sentinel enqueued to stop the actor loop.
_STOP = object()


class SessionActor:
    """Owns a :class:`TyO3Session` on one thread; serialises all access.

    Usage::

        actor = SessionActor(root)
        actor.start()                       # opens + sync_all (blocks until ready)
        rev = actor.submit(lambda s: s.head)
        actor.stop()
    """

    def __init__(self, root: str) -> None:
        self._root = root
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._session: TyO3Session | None = None
        self._ready = threading.Event()
        self._open_error: BaseException | None = None
        self._started = False
        self._stopped = False

    # ── Lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        """Start the actor thread, open the session, and run the initial
        ``sync_all``. Blocks until the project is indexed.

        Raises whatever :class:`TyO3Session` raised on open (e.g.
        ``ProjectOpenError``) — surfaced from the actor thread to the caller.
        """
        if self._started:
            return
        self._started = True
        self._thread = threading.Thread(target=self._run, name="tyo3-session-actor", daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._open_error is not None:
            raise self._open_error

    def _run(self) -> None:
        from tyo3 import TyO3Session

        try:
            self._session = TyO3Session(self._root)
            # Index the project once, up front, so the head graph is populated
            # before the first read. Mirrors tour.py / test_final_acceptance.py.
            self._session.sync_all()
        except BaseException as e:  # noqa: BLE001 — surface any open failure to start()
            self._open_error = e
            self._ready.set()
            return
        self._ready.set()

        while True:
            item = self._queue.get()
            if item is _STOP:
                break
            fut, fn = item
            if fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn(self._session))
                except BaseException as e:  # noqa: BLE001 — propagate to the waiter
                    fut.set_exception(e)

        # Drain any remaining work as cancelled, then close on this thread.
        try:
            while True:
                item = self._queue.get_nowait()
                if item is _STOP:
                    continue
                fut, _fn = item
                fut.cancel()
        except queue.Empty:
            pass
        try:
            if self._session is not None:
                self._session.close()
        except Exception:
            pass

    def stop(self) -> None:
        """Stop the actor loop and close the session (idempotent)."""
        if self._stopped or not self._started:
            return
        self._stopped = True
        self._queue.put(_STOP)
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=5.0)

    # ── Work submission ────────────────────────────────────────────

    def submit(self, fn: Callable[[TyO3Session], Any]) -> Any:
        """Run *fn(session)* on the actor thread and return its result.

        Blocks the calling thread until the closure completes. Exceptions raised
        inside *fn* propagate to the caller unchanged.
        """
        if self._stopped:
            raise RuntimeError("SessionActor is stopped")
        fut: Future = Future()
        self._queue.put((fut, fn))
        return fut.result()

    @property
    def root(self) -> str:
        return self._root
