"""Symbol identity construction for the code graph.

Node identity is the DurableId (SPEC §6.2.1). Entity nodes get their
DurableId from the session's identity system (Gate 2). Nested entities
(methods, inner classes) use their innermost registry id as the stable
prefix for a compound graph key.
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

    For nested entities (methods, inner classes), ``session.id_for`` returns
    the innermost symbol's DurableId. The graph stores a compound key
    ``durable_id::qualified_name`` so nested nodes remain distinct while the
    stable registry id remains the key prefix.

    Returns a string that is valid as a graph node key.
    """
    start = (symbol.selection_range or symbol.location.range).start
    durable_id = session.id_for(file, start.line, start.column)
    if durable_id is None:
        # Defensive cold-path fallback. CodeGraph.build primes the identity
        # registry for mutable sessions, so this should only be needed for
        # read surfaces that cannot reconcile. Keep it location-free.
        qn = symbol.qualified_name or symbol.name
        if parent_durable_id is not None:
            return f"{parent_durable_id}::{qn}"
        return f"{file}::{qn}"

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


def is_entity_node(durable_id: str, *, external: bool) -> bool:
    """True when this node is a real code entity, not a synthetic.

    *external* comes from the producer (``CodeNodeDto.external`` /
    ``SymbolNode.external``) and is the **only** reliable discriminator for an
    external stub. Stub ids are ordinary ``"package::name"`` strings —
    ``"requests::Session"``, ``"unknown::<module>"`` — and carry no prefix
    (`rust/src/code_layer.rs` ``ensure_external_target`` / ``add_import_edge``).
    Only the node's ``file`` field holds the ``"<external>"`` sentinel, and only
    the producer sets ``external``.

    Classifying by id shape alone misreads every external stub as an entity;
    that was the defect this function replaces. Prefer it over
    :func:`is_entity_durable_id` wherever the node is in hand.
    """
    if external:
        return False
    return not durable_id.startswith("<module>")


def is_entity_durable_id(durable_id: str) -> bool:
    """True when *durable_id* is not a synthetic **module** id.

    Use only where the node itself is unavailable. This **cannot** detect an
    external stub — stub ids carry no distinguishing prefix. Use
    :func:`is_entity_node`, which takes the producer's ``external`` flag, for a
    complete answer.
    """
    return not durable_id.startswith("<module>")


def file_from_durable_id(durable_id: str) -> str:
    """Extract the file path from a durable_id.

    For entity nodes, the session's ``locate()`` is the authoritative source.
    This is a fast syntactic fallback for module nodes.

    It cannot resolve an external stub: stub ids carry no ``"<external>"``
    prefix (only the node's ``file`` field does), so a stub falls through to the
    ``"::"`` split below and yields its package name. Read ``node.file`` instead.
    """
    if durable_id.startswith("<module>"):
        return durable_id[len("<module>") :]
    # For ULID-based ids: locate() is authoritative — callers should use
    # session.locate(). This fallback splits on '::' for compound ids.
    return durable_id.split("::", 1)[0]
