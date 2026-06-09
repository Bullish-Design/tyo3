"""Shared fixtures for the daemon tests.

Reuses the synthetic *shop* project from ``tyo3.demo.tour`` (the same files and
``.tyo3/config.toml`` the CLI tour and the demo use) so the daemon is exercised
against a real, fully-configured session: ``precision = "method"``, an authored
``intent`` layer, a local ``summary`` derived layer, and a semantic ``embed``
layer.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tyo3.daemon.handlers import Handlers
from tyo3.daemon.session_actor import SessionActor

try:
    from tyo3 import _HAS_NATIVE
except ImportError:
    _HAS_NATIVE = False

needs_native = pytest.mark.skipif(not _HAS_NATIVE, reason="Rust native extension not built")

pytestmark = needs_native


@pytest.fixture
def shop_project(tmp_path: Path) -> Path:
    """Build the synthetic 'shop' project in a temp dir."""
    from tyo3.demo.tour import _build_project

    proj = tmp_path / "shop"
    _build_project(proj)
    return proj


@pytest.fixture
def actor(shop_project: Path) -> Iterator[SessionActor]:
    """A started :class:`SessionActor` owning a session on the shop project."""
    a = SessionActor(str(shop_project))
    a.start()
    try:
        yield a
    finally:
        a.stop()


@pytest.fixture
def handlers(actor: SessionActor) -> Handlers:
    """RPC handlers bound to the started actor."""
    return Handlers(actor)


@pytest.fixture
def ids(actor: SessionActor) -> dict[str, str]:
    """Map qualified_name (and bare name) → durable_id at head."""

    def work(s) -> dict[str, str]:
        out: dict[str, str] = {}
        g = s.graph
        for idx in g._graph.node_indices():
            node = g._graph[idx]
            out[node.qualified_name] = node.durable_id
            out.setdefault(node.name, node.durable_id)
        return out

    return actor.submit(work)
