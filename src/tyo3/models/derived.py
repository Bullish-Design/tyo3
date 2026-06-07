"""Derived-value model — what snap.derived(layer, id) returns."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class DerivedValue(BaseModel):
    """A resolved derived artifact at a revision.

    ``status`` is honest: ``fresh`` means the artifact matches the entity's
    content hash at this revision; ``stale`` means it is last-good but may
    not reflect the current content; ``failed`` means the generator failed
    and the prior artifact (if any) is served; ``absent`` means no artifact
    exists and none was ever produced.
    """

    model_config = ConfigDict(frozen=True)

    artifact: bytes | None
    status: Literal["fresh", "stale", "failed", "absent"]
    revision: int
    layer: str
