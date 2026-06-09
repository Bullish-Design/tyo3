#!/usr/bin/env python3
"""Record the tyo3.nvim demo as an asciinema v2 ``.cast``.

vhs renders the GIF (the canonical visual artifact); this produces the secondary
asciinema cast for asciinema.org embedding. It is dependency-free — no asciinema
binary required — driving the *same* scripted scenes as ``tour.tape`` in a real
PTY and writing an asciinema v2 cast (a header line + ``[t, "o", data]`` events).

Deterministic by construction: explicit sleeps past every async beat (daemon
open, debounced commit, precision refinement), a fixed window size, and the same
synthetic shop project ``setup.sh`` builds.

Run (inside the devenv shell, from the repo root)::

    python editors/tyo3.nvim/demo/record_cast.py [out.cast]
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import struct
import sys
import termios
import threading
import time
from pathlib import Path

WIDTH = 120
HEIGHT = 34

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parent
REPO = PLUGIN.parent.parent

# The scripted scenes, mirroring tour.tape. (delay_before_seconds, keys_bytes).
# "\r" = Enter, "\x1b" = Esc.
ACTIONS: list[tuple[float, str]] = [
    (8.0, "/def checkout\r"),       # Scene 2: find checkout (after open + daemon)
    (0.8, ":TyO3Inspect\r"),        # floating inspector
    (3.0, "\x1b"),                  # dismiss
    (0.6, ":TyO3Note load-bearing checkout path\r"),  # Scene 3: author a note
    (2.5, ":TyO3Move checkout checkout.py\r"),        # Scene 4: the money shot
    (3.0, ":e checkout.py\r"),      # note rides along
    (3.0, ":e catalog.py\r"),       # Scene 5: edit a base method
    (1.0, "/100\r"),                # land on the literal, not `return`
    (0.5, "ciw250\x1b"),            # change the value → return 250
    (0.7, ":w\r"),                  # debounced commit fires
    (5.0, ":TyO3Affected\r"),       # Scene 6: navigate the affected set
    (2.0, "1\r"),
    (2.5, ":qa!\r"),
    (1.5, ""),                      # let it exit
]


def main() -> int:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else (HERE / "tour.cast")

    demo_cmd = (
        "source editors/tyo3.nvim/demo/setup.sh && "
        'cd "$TYO3_DEMO_DIR" && '
        # $TYO3_NVIM is the pristine neovim-unwrapped binary setup.sh resolves
        # (the home-manager wrapper injects a user after/ftplugin even under
        # --clean); --clean + -u then loads only $VIMRUNTIME + the demo init.
        'exec "$TYO3_NVIM" --clean -u "$TYO3_PLUGIN_DIR/demo/init.lua" store.py'
    )

    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    env["TYO3_PLUGIN_DIR"] = str(PLUGIN)

    pid, master = pty.fork()
    if pid == 0:
        # Child: become the demo. cwd = repo root so the relative source path works.
        os.chdir(str(REPO))
        os.execvpe("bash", ["bash", "-c", demo_cmd], env)
        os._exit(127)

    # Parent: set the PTY window size so nvim lays out at WIDTH×HEIGHT.
    winsize = struct.pack("HHHH", HEIGHT, WIDTH, 0, 0)
    fcntl.ioctl(master, termios.TIOCSWINSZ, winsize)

    start = time.monotonic()
    events: list[tuple[float, str, str]] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            try:
                r, _, _ = select.select([master], [], [], 0.2)
            except OSError:
                break
            if master in r:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                events.append((time.monotonic() - start, "o", data.decode("utf-8", errors="replace")))

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    try:
        for delay, keys in ACTIONS:
            time.sleep(delay)
            if keys:
                os.write(master, keys.encode("utf-8"))
    except OSError:
        pass

    # Give the child a moment to exit, then tear down.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        wpid, _ = os.waitpid(pid, os.WNOHANG)
        if wpid != 0:
            break
        time.sleep(0.1)
    stop.set()
    t.join(timeout=2.0)
    try:
        os.close(master)
    except OSError:
        pass
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass

    header = {
        "version": 2,
        "width": WIDTH,
        "height": HEIGHT,
        "timestamp": int(time.time()),
        "env": {"SHELL": "/bin/bash", "TERM": "xterm-256color"},
        "title": "tyo3.nvim — durable identity in Neovim",
    }
    with out_path.open("w", encoding="utf-8") as f:
        f.write(json.dumps(header) + "\n")
        for ts, kind, data in events:
            f.write(json.dumps([round(ts, 6), kind, data]) + "\n")

    size = out_path.stat().st_size
    print(f"wrote {out_path} ({len(events)} events, {size} bytes)")
    return 0 if events else 1


if __name__ == "__main__":
    raise SystemExit(main())
