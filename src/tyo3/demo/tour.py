"""Guided tour of TyO3's incremental, transactional engine.

A deterministic, scripted walkthrough rendered as a stepped CLI session: it
builds a tiny synthetic project in a temp dir, then plays five scenes —
durable identity, authored memory that survives refactoring, the id-level
delta bus + transitive affected closure, derived-layer recompute (local vs
semantic), and a time-travel snapshot diff — printing the *actual* API calls
and their real results at each step. It ends by dropping into the live REPL so
you can poke the edited session yourself.

Run it:

    tyo3-demo tour            # interactive: press Enter between scenes
    tyo3-demo tour --auto     # run straight through (CI / asciinema)
    tyo3-demo tour --no-repl  # skip the drop-to-REPL at the end

Aimed at agent/tool builders: every scene shows the literal `session.edit(...)`,
`session.subscribe(...)`, `snap.diff(...)` calls you would make.
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

from tyo3.demo.cli import bold, cyan, dim, green, red
from tyo3.sidecar import Sidecar

# ── Deterministic in-process derived generators ─────────────────────────────
#
# Batched python generators (the derived-layer contract: take a list of
# GenInput, return a list of artifact strings). Each records the ids it
# processed so the tour can show recompute-vs-reuse by counting invocations.

SUMMARY_CALLS: list[str] = []
EMBED_CALLS: list[str] = []


def summary_generator(inputs):
    SUMMARY_CALLS.extend(i.durable_id for i in inputs)
    return [f"summary<{(i.source or '').splitlines()[0].strip()}>" for i in inputs]


def embedding_generator(inputs):
    EMBED_CALLS.extend(i.durable_id for i in inputs)
    return [f"vec[dim={len(i.source or '')}]" for i in inputs]


# ── The fixed synthetic project ─────────────────────────────────────────────

MONEY_SRC = """\
def usd(cents: int) -> str:
    return f"${cents / 100:.2f}"
"""

CATALOG_SRC = """\
class Item:
    def price(self) -> int:
        return 100

    def label(self) -> str:
        return "item"
"""

BOOK_SRC = """\
from catalog import Item


class Book(Item):
    def isbn(self) -> str:
        return "0000000000"
"""

STORE_SRC = """\
from book import Book
from catalog import Item
from money import usd


def checkout() -> str:
    return usd(Book().price())


def show_label() -> str:
    return Item().label()
"""

LEGACY_SRC = """\
def legacy_helper(x: int) -> int:
    return x + 1
"""

# Scene 3 edit: Item.price returns 250 instead of 100 (a meaningful method edit).
CATALOG_SRC_PRICE_250 = """\
class Item:
    def price(self) -> int:
        return 250

    def label(self) -> str:
        return "item"
"""

CONFIG_TOML = """\
schema_version = 1

[hashing.profiles.structure]

[code_graph]
precision = "method"
refinement = "async"

[coordination.bus]
queue_capacity = 256
overflow = "coalesce"

[layers.intent]
origin = "authored"
history = true
review_on_change = true

[layers.docs]
origin = "authored"
history = true

[layers.summary]
origin = "derived"
depends_on = ["code"]
generator = "summary_gen"
generator_version = "v1"
hash_profile = "structure"
store = "kv_summary"
serving = "block"
key_locality = "local"
entity_kinds = ["function"]

[layers.embed]
origin = "derived"
depends_on = ["code"]
generator = "embed_gen"
generator_version = "v1"
hash_profile = "structure"
store = "kv_embed"
serving = "block"
key_locality = "semantic"
entity_kinds = ["function"]

[generators.summary_gen]
type = "python"
callable = "tyo3.demo.tour:summary_generator"

[generators.embed_gen]
type = "python"
callable = "tyo3.demo.tour:embedding_generator"

[stores.kv_summary]
backend = "fs"
path = "cache/summary"

[stores.kv_embed]
backend = "fs"
path = "cache/embed"
"""


def _build_project(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pyproject.toml").write_text('[project]\nname = "shop"\n')
    (root / "money.py").write_text(MONEY_SRC)
    (root / "catalog.py").write_text(CATALOG_SRC)
    (root / "book.py").write_text(BOOK_SRC)
    (root / "store.py").write_text(STORE_SRC)
    (root / "legacy.py").write_text(LEGACY_SRC)
    (root / "legacy_moved.py").write_text("")  # known path for the atomic move
    # Construct the sidecar layout through its sole owner (Sidecar), never an
    # inline sidecar-dir literal — the single-path-owner policy enforced by
    # test_sidecar_is_sole_path_owner_in_source.
    sidecar = Sidecar(root)
    sidecar.root.mkdir()
    sidecar.config_path().write_text(CONFIG_TOML)


# ── Presentation helpers ────────────────────────────────────────────────────


class _Tour:
    def __init__(self, auto: bool) -> None:
        self.auto = auto
        self._scene = 0

    def scene(self, title: str, why: str) -> None:
        self._scene += 1
        print(f"\n{bold('━' * 70)}")
        print(f"{bold(f'  Scene {self._scene}:  {title}')}")
        print(f"  {dim(why)}")
        print(bold("━" * 70))

    def say(self, text: str) -> None:
        print(f"\n{text}")

    def code(self, line: str) -> None:
        print(f"    {dim('>>>')} {cyan(line)}")

    def result(self, label: str, value: object) -> None:
        print(f"    {green('→')} {label}: {bold(value)}")

    def pause(self) -> None:
        if self.auto:
            return
        try:
            input(f"\n  {dim('[Enter] to continue')}")
        except (EOFError, KeyboardInterrupt):
            raise SystemExit(0) from None


def _ids(graph) -> dict[str, str]:
    out: dict[str, str] = {}
    g = graph._graph
    for idx in g.node_indices():
        node = g[idx]
        out[node.qualified_name] = node.durable_id
        out.setdefault(node.name, node.durable_id)
    return out


def _node(graph, durable_id: str):
    g = graph._graph
    for idx in g.node_indices():
        if g[idx].durable_id == durable_id:
            return g[idx]
    return None


def _short(did: str) -> str:
    return did[-6:]


# ── The tour ────────────────────────────────────────────────────────────────


def run_tour(*, auto: bool = False, repl: bool = True) -> None:
    from tyo3 import TyO3Session
    from tyo3.bus.interest import Interest

    t = _Tour(auto)
    tmp = Path(tempfile.mkdtemp(prefix="tyo3_tour_"))
    proj = tmp / "shop"
    _build_project(proj)

    print(f"\n{bold('TyO3 — guided tour')}")
    print(dim(f"  A tiny synthetic 'shop' project in {proj}"))
    print(dim("  Every step shows the real API call and its real result."))

    try:
        _run_scenes(t, proj, TyO3Session, Interest, repl)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_scenes(t: _Tour, proj: Path, TyO3Session, Interest, repl: bool) -> None:
    with TyO3Session(str(proj)) as session:
        session.sync_all()
        t.code("session.sync_all()")
        ids = _ids(session.graph)
        price_id = ids["Item.price"]
        legacy_id = ids["legacy_helper"]

        # Pin the original world now, before any edits — used in Scene 5.
        snap0 = session.snapshot()

        # ── Scene 1: durable identity ────────────────────────────────────
        t.scene(
            "Durable identity",
            "An agent needs a stable handle on a code entity — one that outlives edits.",
        )
        t.say("Every entity has a DurableId, resolvable by position or carried in deltas.")
        t.code('session.id_for("legacy.py", 1, 5)')
        t.result("legacy_helper id", legacy_id)
        t.result("Item.price id", price_id)
        t.pause()

        # ── Scene 2: authored memory that survives refactoring ───────────
        t.scene(
            "Authored memory survives refactoring",
            "Attach durable knowledge to an entity; it stays glued through renames, moves, reformats.",
        )
        t.say("Author a note on legacy_helper, then refactor the code around it.")
        t.code('session.author("intent", legacy_id, {"note": "load-bearing: do not inline"})')
        session.author("intent", legacy_id, {"note": "load-bearing: do not inline"})
        h0 = _node(session.graph, legacy_id).content_hash
        t.result("authored note", session.authored("intent", legacy_id).value)
        t.result("content hash", _short(h0))

        t.say(dim("(a) Reformat — add whitespace (cosmetic):"))
        t.code('session.edit("legacy.py", "def legacy_helper(x: int) -> int:\\n\\n    return x + 1\\n")')
        session.edit("legacy.py", "def legacy_helper(x: int) -> int:\n\n    return x + 1\n")
        h1 = _node(session.graph, legacy_id).content_hash
        t.result("id stable", _ids(session.graph)["legacy_helper"] == legacy_id)
        t.result("hash unchanged (cosmetic)", _short(h1) + f"  == {_short(h0)}: {h1 == h0}")

        t.say(dim("(b) Move it to another file, body unchanged (one atomic commit):"))
        t.code('session.edit_many({"legacy.py": "", "legacy_moved.py": <same body>})')
        session.edit_many(
            {"legacy.py": "", "legacy_moved.py": "def legacy_helper(x: int) -> int:\n\n    return x + 1\n"}
        )
        t.result("id stable across move", _ids(session.graph)["legacy_helper"] == legacy_id)
        t.result("locate() now points to", session.locate(legacy_id))
        t.result("note still attached", session.authored("intent", legacy_id).value)

        t.say(dim("(c) A *meaningful* edit — change the body:"))
        t.code('session.edit("legacy_moved.py", "def legacy_helper(x: int) -> int:\\n    return x + 100\\n")')
        session.edit("legacy_moved.py", "def legacy_helper(x: int) -> int:\n    return x + 100\n")
        h2 = _node(session.graph, legacy_id).content_hash
        t.result("id STILL stable", _ids(session.graph)["legacy_helper"] == legacy_id)
        t.result("hash changed (real edit)", f"{_short(h0)} → {_short(h2)}: {h2 != h0}")
        t.result("note flagged for review", session.authored("intent", legacy_id).status)
        t.pause()

        # ── Scene 3: the delta bus + transitive affected closure ─────────
        t.scene(
            "Id-level bus + transitive affected closure",
            "Edit one method; get the exact set of entities to re-analyze — computed natively.",
        )
        t.say("Subscribe, then edit the base class Item.price (a method body).")
        t.code("sub = session.subscribe(Interest.ALL)")
        sub = session.subscribe(Interest.ALL)
        t.code('result = session.edit("catalog.py", <Item.price returns 250>)')
        result = session.edit("catalog.py", CATALOG_SRC_PRICE_250)
        ids2 = _ids(session.graph)
        names = {v: k for k, v in ids2.items()}
        t.result("changed ids (the seeds)", [names.get(i, _short(i)) for i in result.changed_ids])
        t.result(
            "AFFECTED closure (subclass + importers)",
            sorted(names.get(i, _short(i)) for i in result.affected_ids),
        )
        primary = sub.poll(timeout=5.0)
        if primary is not None:
            t.result("bus delta revision", primary.revision)
            t.result("bus affected (id-level)", sorted(names.get(i, _short(i)) for i in primary.affected))
        ref = sub.poll_refinement(timeout=10.0)
        if ref is not None:
            t.result(
                "precision=method REFINEMENT narrows to",
                sorted(names.get(i, _short(i)) for i in ref.narrowed),
            )
            t.say(dim("  (show_label drops out — it uses .label(), not the changed .price())"))
        sub.close()
        t.pause()

        # ── Scene 4: derived recompute — local vs semantic ───────────────
        t.scene(
            "Derived layers: local vs semantic recompute",
            "Content-hash-keyed artifacts that recompute only when they must.",
        )
        t.say("Two layers over functions: 'summary' (local) and 'embed' (semantic).")
        warm = session.snapshot()
        t.code('warm.derived("summary", checkout_id) / warm.derived("embed", checkout_id)')
        checkout_id = ids2["checkout"]
        warm.derived("summary", checkout_id)
        warm.derived("embed", checkout_id)
        warm.close()
        sm, em = len(SUMMARY_CALLS), len(EMBED_CALLS)
        t.say("Now edit money.usd — a dependency of checkout (checkout calls usd).")
        t.code('session.edit("money.py", <usd body changed>)')
        session.edit("money.py", 'def usd(cents: int) -> str:\n    return f"$ {cents / 100:.2f}"\n')
        after = session.snapshot()
        after.derived("summary", checkout_id)
        after.derived("embed", checkout_id)
        after.close()
        t.result("local 'summary' recomputed checkout?", len(SUMMARY_CALLS) > sm)
        t.result("semantic 'embed' recomputed checkout?", len(EMBED_CALLS) > em)
        t.say(dim("  Local keys on the entity's own hash → reused. Semantic folds in the"))
        t.say(dim("  dependency fingerprint → recomputes when a dependency changes."))
        t.pause()

        # ── Scene 5: time-travel diff ────────────────────────────────────
        t.scene(
            "Time-travel snapshot diff",
            "Pin the world, keep editing, then diff old vs new at the entity level.",
        )
        t.say("snap0 was pinned at the very start; head has moved on through every edit.")
        now = session.snapshot()
        t.code("diff = now.diff(snap0)")
        diff = now.diff(snap0)
        t.result("entities changed", sorted(names.get(i, _short(i)) for i in diff.code.changed))
        t.result("entities moved", sorted(names.get(i, _short(i)) for i in diff.code.moved))
        t.result("snap0 still reads the ORIGINAL revision", snap0.revision)
        t.result("head revision now", now.revision)
        now.close()
        snap0.close()

        print(f"\n{green(bold('That is the whole model:'))} durable identity · authored memory ·")
        print(f"{green('id-level deltas')} · derived caching · MVCC time-travel — one session.\n")

        if repl:
            t.pause()
            from tyo3.demo.cli import run_interactive

            print(dim("Dropping into the live explorer against this edited session."))
            print(dim("Try: files · symbols store.py · search checkout · help · quit\n"))
            run_interactive(session, "shop (tour)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="tyo3-demo tour",
        description="Guided tour of TyO3's incremental engine on a synthetic project.",
    )
    parser.add_argument("--auto", action="store_true", help="Run straight through (no pauses).")
    parser.add_argument("--no-repl", action="store_true", help="Skip the drop-to-REPL at the end.")
    args = parser.parse_args(argv)
    try:
        run_tour(auto=args.auto, repl=not args.no_repl)
    except KeyboardInterrupt:
        print(red("\nInterrupted."))


if __name__ == "__main__":
    main()
