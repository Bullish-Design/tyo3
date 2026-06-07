"""EntityView — the cross-layer join (§10.2.2 as a unit).

Packages the per-id, all-layer join at one pinned revision into a single
frozen object. Every member describes the same revision R; no head access.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from tyo3.graph.models import SymbolNode
    from tyo3.models.authored import AuthoredValue
    from tyo3.models.derived import DerivedValue
    from tyo3.session import Snapshot


@dataclass(frozen=True)
class EntityView:
    """The cross-layer join for one ``DurableId`` at one pinned revision.

    All members describe the same revision — the join is consistent by
    construction because every sub-read resolves against the snapshot's
    pinned revision.  There is no head access.
    """

    durable_id: str
    revision: int
    code: SymbolNode | None  # None iff the entity is absent at R
    content_hash: str | None
    location: str | None  # file::qualified_name at R
    status: Literal["active", "needs_review", "orphaned", "absent"]
    derived: dict[str, DerivedValue]  # layer name → value
    authored: dict[str, AuthoredValue]  # layer name → value

    @classmethod
    def from_snapshot(cls, snap: Snapshot, durable_id: str) -> EntityView:
        """Build the join from a pinned snapshot and a DurableId.

        Every sub-read resolves against ``snap.revision`` — consistent
        by construction.
        """
        # Code layer
        code_view = snap.code
        code_node = code_view.value(durable_id)

        # Location from the snapshot's captured registry.
        location: str | None = None
        try:
            location = snap.locate(durable_id)
        except Exception:
            pass

        # Status from the captured registry anchor status.
        # Derive from authored layers' status or from the registry.
        status: Literal["active", "needs_review", "orphaned", "absent"]
        if code_node is None:
            status = "absent"
        else:
            # Check if needs_review/orphaned via the snapshot's registry.
            try:
                nr_list = snap.needs_review()
                orph_list = snap.orphaned()
                if durable_id in orph_list:
                    status = "orphaned"
                elif durable_id in nr_list:
                    status = "needs_review"
                else:
                    status = "active"
            except Exception:
                status = "active"

        # Derived layers
        derived: dict[str, DerivedValue] = {}
        if snap._config is not None:
            for lname, lcfg in snap._config.layers.items():
                if lcfg.origin != "derived":
                    continue
                # Only include layers that apply_to the entity's kind.
                if code_node is not None and lcfg.entity_kinds:
                    if code_node.kind.value not in lcfg.entity_kinds:
                        continue
                dv = snap.derived(lname, durable_id)
                derived[lname] = dv  # include all statuses, even absent

        # Authored layers
        authored: dict[str, AuthoredValue] = {}
        if snap._config is not None:
            for lname, lcfg in snap._config.layers.items():
                if lcfg.origin != "authored":
                    continue
                try:
                    av = snap.authored(lname, durable_id)
                    if av.status != "absent":
                        authored[lname] = av
                except Exception:
                    continue

        return cls(
            durable_id=durable_id,
            revision=snap.revision,
            code=code_node,
            content_hash=code_node.content_hash if code_node else None,
            location=location,
            status=status,
            derived=derived,
            authored=authored,
        )
