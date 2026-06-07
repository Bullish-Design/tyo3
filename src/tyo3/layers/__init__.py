"""TyO3 Layer Views — uniform read surface (§5.1).

Each layer (code, derived, authored) implements the same ``LayerView``
protocol, bound to one pinned ``Snapshot``. This lets the cross-layer
join (Step 2) and combined diff (Step 6) iterate layers uniformly.
"""

from __future__ import annotations

from tyo3.layers.base import LayerView, LayerDiff
from tyo3.layers.code import CodeLayerView
from tyo3.layers.derived import DerivedLayerView
from tyo3.layers.authored import AuthoredLayerView

__all__ = [
    "LayerView",
    "LayerDiff",
    "CodeLayerView",
    "DerivedLayerView",
    "AuthoredLayerView",
]
