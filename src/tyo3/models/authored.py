"""Authored-value model — what snap.authored(layer, id) returns.

Key invariant: *status is derived*, never stored. The `AuthoredValue.status`
is computed from the snapshot-captured identity registry's anchor status for
the durable id, gated by the layer's `review_on_change` setting.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class AuthoredValue(BaseModel):
    """A resolved authored record at a pinned revision.

    ``status`` is honest (§10.2.3):
    - ``present``: value exists and entity is active (or review_on_change=false).
    - ``needs_review``: value exists but entity body changed — human review needed.
    - ``orphaned``: value exists but entity vanished from the codebase.
    - ``absent``: no authored record for this (layer, id) at this revision.
    """

    model_config = ConfigDict(frozen=True)

    layer: str
    durable_id: str
    value: Any | None
    status: Literal["present", "needs_review", "orphaned", "absent"]
    revision: int


class AuthoredVersion(BaseModel):
    """A single revision-stamped version of an authored value (for history)."""

    model_config = ConfigDict(frozen=True)

    value: Any
    revision: int


__all__ = ["AuthoredValue", "AuthoredVersion"]
