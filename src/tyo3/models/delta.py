"""The id-level commit delta (Phase 3, §5.4 / §5.5).

``CommitDelta`` is the single public per-write value, mirroring the native
``CommitDeltaDto``.  Where the legacy ``SyncResult`` carried file-path strings,
this carries **entity DurableIds**: ``created_ids`` / ``changed_ids`` /
``deleted_ids`` (``changed`` means the content hash changed — never "a path
rebind happened"), a structured ``moved`` (``id`` + old/new location), and the
transitively ``affected_ids``.

The structural code delta is no longer carried here (Project 31, #2): the native
``CodeLayer`` still maintains ``reverse_deps`` in-commit (computing
``affected_ids``), but graph consumers build on demand from ``full_code_delta()``
rather than replaying a per-commit structural diff.

The path-shaped ``created`` / ``changed`` / ``deleted`` / ``touched_files``
fields are **metadata** (the files the write synthesised events for), kept for
file-interest bus matching.  They are never an id substitute (§5.4).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class MovedEntity(BaseModel):
    """A structured move: same ``id``, unchanged body, new location."""

    model_config = ConfigDict(from_attributes=True)

    id: str
    old_qualified_path: str
    new_qualified_path: str
    old_file: str
    new_file: str


class CommitDelta(BaseModel):
    """The id-level delta produced by one committed write (§5.4 / §5.5)."""

    model_config = ConfigDict(from_attributes=True)

    revision: int

    # ── id-level entity delta (the contract) ────────────────────────────
    created_ids: list[str] = Field(default_factory=list)
    changed_ids: list[str] = Field(default_factory=list)
    deleted_ids: list[str] = Field(default_factory=list)
    moved: list[MovedEntity] = Field(default_factory=list)
    authored_ids: list[str] = Field(default_factory=list)
    affected_ids: list[str] = Field(default_factory=list)

    # ── path-shaped metadata (NOT ids) ──────────────────────────────────
    # Directly-edited files (project-relative). The bus's file-interest surface.
    touched_files: list[str] = Field(default_factory=list)
    # Project-relative files of the ``affected_ids`` closure (reverse-dependents
    # included), emitted natively so the bus matches file-interest without a
    # graph walk (§5.11).
    affected_files: list[str] = Field(default_factory=list)
    created: list[str] = Field(default_factory=list)
    changed: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)

    # ── lifecycle / coarse-change signals ───────────────────────────────
    needs_review: list[str] = Field(default_factory=list)
    orphaned: list[str] = Field(default_factory=list)
    authored_needs_review: list[str] = Field(default_factory=list)
    authored_orphaned: list[str] = Field(default_factory=list)
    identity_extracted: int = 0
    identity_scope_files: int = 0
    rescan: bool = False
    project_changed: bool = False
    custom_stdlib_changed: bool = False


__all__ = ["CommitDelta", "MovedEntity"]
