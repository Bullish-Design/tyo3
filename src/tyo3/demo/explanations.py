"""Curated, offline "LLM" explanations for the tyo3.nvim demo recording.

Deterministic prose for the synthetic *shop* entities so the `explain` code
action / panel action shows a believable, model-quality summary **with no
network call** — the recording stays reproducible. The demo selects this backend
with::

    TYO3_LLM=callable TYO3_LLM_CALLABLE=tyo3.demo.explanations:explain

A real deployment instead sets ``TYO3_LLM=anthropic`` and a current model (see
``src/tyo3/daemon/llm.py``); this module is demo scaffolding, not the product.

``explain(prompt, *, system=None)`` matches the ``_llm`` seam signature. It reads
the entity name/kind off the prompt's first ``Entity: <name> (<kind>)`` line and
the mode off the system prompt, then returns curated text (with a graceful
generic fallback for any entity not in the table).
"""

from __future__ import annotations

import re

# explain-mode prose, keyed by (bare) entity name.
_EXPLAIN = {
    "checkout": (
        "Prices a single book for sale — the load-bearing purchase path. `checkout` "
        "constructs a `Book`, reads its inherited `price()`, and renders the result "
        "as a USD string via `usd()`. It depends on both `Book` and `usd`, so a "
        "change to either ripples straight through here."
    ),
    "usd": (
        "Formats an integer number of cents as a human-readable USD amount "
        "(`1099 → \"$10.99\"`). A pure, dependency-free helper; `checkout` relies on "
        "it to render the final price a customer sees."
    ),
    "show_label": (
        "Returns the display label for a catalogue item. `show_label` instantiates "
        "`Item` and delegates to its `label()` method — a thin presentation wrapper "
        "with no business logic of its own."
    ),
    "Item": (
        "The base catalogue type. `Item` defines the default `price()` (100) and "
        "`label()` (\"item\") that every product inherits; `Book` subclasses it. "
        "Editing either method changes the default for every item that doesn't "
        "override it."
    ),
    "Product": (
        "The base catalogue type (renamed from `Item`). It defines the default "
        "`price()` and `label()` that every product inherits; `Book` subclasses it."
    ),
    "price": (
        "Returns the item's base price in whole units (default 100). Subclasses "
        "inherit it unless they override; `checkout` reads it through "
        "`Book().price()`, so this value flows directly into the USD total."
    ),
    "label": (
        "Returns the item's display label (default \"item\"). A presentation detail "
        "inherited by every subclass; `show_label` surfaces it to the UI."
    ),
    "Book": (
        "A concrete product type. `Book` extends `Item`, inheriting its "
        "`price()`/`label()` and adding an `isbn()`. `checkout` builds one to "
        "compute a sale price."
    ),
    "isbn": (
        "Returns the book's ISBN as a string. A `Book`-specific accessor not present "
        "on the base `Item`."
    ),
    "legacy_helper": (
        "A tiny arithmetic helper that returns its argument plus one. Self-contained, "
        "with no callers in the project — a clean candidate to move or retire."
    ),
}

# simplify-mode prose, keyed by name (sparse — generic fallback covers the rest).
_SIMPLIFY = {
    "checkout": (
        "`checkout` constructs both of its dependencies inline (`Book()`, `usd(...)`). "
        "To price many items, lift the construction out and pass the value in, so the "
        "function is pure and trivially testable:\n\n"
        "    def checkout(cents: int) -> str:\n"
        "        return usd(cents)\n\n"
        "Behaviour for a single book is unchanged; you've just removed the hard "
        "dependency on `Book` from the pricing step."
    ),
    "show_label": (
        "`show_label` builds an `Item` only to call `label()`. If the caller already "
        "has an item, accept it as a parameter (`def show_label(item: Item) -> str: "
        "return item.label()`) so the function stops constructing global state and "
        "becomes reusable across subclasses."
    ),
}

_GENERIC_EXPLAIN = (
    "`{name}` is a {kind} in this module. It has no separate authored intent yet — "
    "running this action stores a summary on the durable `explain` layer, keyed by "
    "its identity, so the explanation rides edits and moves and flags itself when "
    "the body later drifts."
)

_GENERIC_SIMPLIFY = (
    "Consider narrowing what `{name}` constructs itself: lift inline dependencies "
    "into parameters so the {kind} stays pure and testable, and give it a precise "
    "return type. No behaviour change required — just reduce what it reaches for."
)


def _parse_entity(prompt: str) -> tuple[str, str]:
    m = re.search(r"^Entity: (\S+) \(([^)]+)\)", prompt, re.MULTILINE)
    if not m:
        return ("this entity", "entity")
    qualified, kind = m.group(1), m.group(2)
    name = qualified.rsplit(".", 1)[-1]  # Item.price → price
    return name, kind


def explain(prompt: str, *, system: str | None = None) -> str:
    """The ``_llm``-shaped seam: curated demo explanation for *prompt*."""
    name, kind = _parse_entity(prompt)
    simplify = bool(system and "refactor" in system.lower())
    table = _SIMPLIFY if simplify else _EXPLAIN
    text = table.get(name)
    if text is not None:
        return text
    template = _GENERIC_SIMPLIFY if simplify else _GENERIC_EXPLAIN
    return template.format(name=name, kind=kind)
