"""`AuthoredLayer` — a read-only view of an authored layer's config.

Created from `session.config` via `AuthoredLayer.from_config(...)` for
callers that want to enumerate authored layers without touching the native
store directly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AuthoredLayer:
    """A read-only view of an authored layer's configuration.

    Created by `from_config` from a `TyConfig` + layer name.  Carries
    **no generator/store** — authored layers have no upstream and are
    not registered in the Gate-5 `DerivationDAG` (they are sinks, §9.2.4).
    """

    name: str
    """The layer name as declared in ``config.toml`` (e.g. ``"intent"``)."""

    history: bool
    """Whether prior versions are retained (``history = true`` in config)."""

    review_on_change: bool
    """Whether the record is flagged ``needs_review`` when its entity changes."""

    @classmethod
    def from_config(cls, config: object, name: str) -> AuthoredLayer:
        """Build an `AuthoredLayer` view from a `TyConfig` and layer name.

        Args:
            config: A `TyConfig` instance (from `session.config`).
            name: The declared layer name.

        Returns:
            An `AuthoredLayer` with the layer's settings.

        Raises:
            KeyError: if ``name`` is not a declared layer.
            ValueError: if the layer's origin is not ``"authored"``.
        """
        layer_cfg = config.layers[name]  # type: ignore[attr-defined]
        if layer_cfg.origin != "authored":  # type: ignore[attr-defined]
            raise ValueError(
                f"Layer '{name}' has origin '{layer_cfg.origin}', not 'authored'"
            )
        return cls(
            name=name,
            history=layer_cfg.history,
            review_on_change=layer_cfg.review_on_change,
        )


__all__ = ["AuthoredLayer"]
