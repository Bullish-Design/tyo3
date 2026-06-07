"""Authored-layer façade (Gate 6).

Provides `AuthoredLayer`, a read-only view of an authored layer's config,
built from `session.config`.  Authored layers are sinks (§9.2.4) — they
have no upstream and are absent from the derivation DAG.
"""

from tyo3.authored.layer import AuthoredLayer

__all__ = ["AuthoredLayer"]
