"""``python -m tyo3.daemon`` / ``tyo3-daemon`` — serve one project over a socket.

    tyo3-daemon --root /path/to/project [--socket PATH] [--autostop]

Owns one :class:`~tyo3.TyO3Session` for ``--root`` and serves it over a
unix-domain socket (default: ``$XDG_RUNTIME_DIR/tyo3/<hash(root)>.sock``).
Logs to **stderr** so a supervisor (overseer.nvim) can surface them. The socket
is removed on exit; ``SIGINT`` / ``SIGTERM`` trigger a clean shutdown. With
``--autostop`` the daemon exits when the last client disconnects; otherwise it
stays resident.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

from tyo3.daemon.server import DaemonServer, default_socket_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tyo3-daemon", description="Serve a TyO3 project over a unix socket.")
    parser.add_argument("--root", required=True, help="Project root directory to open.")
    parser.add_argument("--socket", default=None, help="Socket path (default: $XDG_RUNTIME_DIR/tyo3/<hash>.sock).")
    parser.add_argument(
        "--autostop",
        action="store_true",
        help="Exit when the last client disconnects (default: stay resident).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (to stderr).",
    )
    parser.add_argument(
        "--print-socket",
        action="store_true",
        help="Print the resolved socket path to stdout once listening (for spawners).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, args.log_level),
        format="%(asctime)s tyo3d %(levelname)s %(message)s",
    )

    root = Path(args.root)
    if not root.exists():
        print(f"tyo3-daemon: root does not exist: {root}", file=sys.stderr)
        return 2

    socket_path = Path(args.socket) if args.socket else default_socket_path(root)
    server = DaemonServer(root, socket_path=socket_path, autostop=args.autostop)

    def _handle_signal(signum, _frame):
        logging.getLogger("tyo3.daemon").info("received signal %s; shutting down", signum)
        server.shutdown()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    if args.print_socket:
        # The spawner waits for this line, then connects.
        print(str(socket_path), flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
    except Exception as e:  # noqa: BLE001 — top-level: report and exit non-zero
        logging.getLogger("tyo3.daemon").exception("fatal: %s", e)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
