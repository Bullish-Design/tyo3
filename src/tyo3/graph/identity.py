"""Symbol identity construction for the code graph.

Node identity is the DurableId (SPEC §6.2.1). Top-level entities get their
DurableId from the session's identity system (Gate 2). Nested entities
(methods, inner classes) derive compound ids from the parent DurableId —
stable across parent moves and renames.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tyo3.models.symbols import Symbol

if TYPE_CHECKING:
    from tyo3.session import TyO3Session


def derive_durable_id(
    session: TyO3Session,
    file: str,
    symbol: Symbol,
    parent_durable_id: str | None = None,
) -> str:
    """Derive the DurableId for a symbol entity.

    For top-level entities (no *parent_durable_id*), queries the session's
    identity system via ``session.id_for(path, line, col)``.  The returned
    ULID survives moves, renames, and cosmetic edits (Gate 2 §5).

    For nested entities (methods, inner classes), the session returns the
    enclosing class's DurableId (not a method-level id), so we derive a
    compound id: ``parent_durable_id::qualified_name``.  This is stable
    across parent moves because the parent's DurableId is stable.

    Returns a string that is valid as a graph node key.
    """
    start = (symbol.selection_range or symbol.location.range).start
    durable_id = session.id_for(file, start.line, start.column)
    if durable_id is None:
        # No identity registered yet — fall back to a qualified-name-based
        # key. This happens for freshly-created entities before the
        # reconciliation pass runs.
        if symbol.qualified_name:
            return f"{file}::{symbol.qualified_name}"
        return f"{file}::{symbol.name}@{symbol.location.range.start.line}"

    if parent_durable_id is None:
        # Top-level entity: use the session's DurableId directly.
        return durable_id

    # Nested entity: derive compound id.
    qn = symbol.qualified_name or symbol.name
    return f"{durable_id}::{qn}"


def make_module_durable_id(file: str) -> str:
    """Create a stable synthetic DurableId for a module node.

    Module nodes are synthetic — they are not entities the identity system
    tracks — but must still be stable and not location-derived.
    """
    # The file path within the project is stable; encode it.
    return f"<module>{file}"


def file_from_durable_id(durable_id: str) -> str:
    """Extract the file path from a durable_id.

    For entity nodes, the session's ``locate()`` is the authoritative source.
    This is a fast syntactic fallback for module nodes and external stubs.
    """
    if durable_id.startswith("<module>"):
        return durable_id[len("<module>"):]
    if durable_id.startswith("<external>"):
        return "<external>"
    # For ULID-based ids: locate() is authoritative — callers should use
    # session.locate(). This fallback splits on '::' for compound ids.
    return durable_id.split("::", 1)[0]
