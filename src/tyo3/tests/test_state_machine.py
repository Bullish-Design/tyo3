"""State machine / transition graph tests — TyProject status lifecycle.

Transition graph derived from tyo3-core.allium:

  (initial) ──OpenProject──→ [open] ──CloseProject──→ [closed]
                                   ──ReloadProject──→ [open]

Obligation groups:
- Valid transitions: closed→open (via OpenProject), open→closed (CloseProject),
  open→open (ReloadProject)
- Invalid transitions: closed→closed (can't close closed), open→open (can't reopen open root)
- Terminal states: closed has no outbound rules in current spec
"""

from pathlib import PurePosixPath

import pytest

from tyo3.models.core import (
    ProjectStatus,
)


class TestTransitionGraph:
    """Transition graph edge tests."""

    def test_initial_to_open_via_open_project(self, project_service) -> None:
        """closed → open: OpenProject creates a project in 'open' status."""
        root = PurePosixPath("new")
        project, _ = project_service.open_project(root)
        assert project.status == ProjectStatus.OPEN

    def test_open_to_closed_via_close_project(self, project_service) -> None:
        """open → closed: CloseProject transitions from open to closed."""
        root = PurePosixPath("toclose")
        project, _ = project_service.open_project(root)
        project_service.close_project(project)
        assert project.status == ProjectStatus.CLOSED

    def test_open_to_open_via_reload_project(self, project_service) -> None:
        """open → open: ReloadProject keeps project open."""
        root = PurePosixPath("toreload")
        project, _ = project_service.open_project(root)
        project_service.reload_project(project)
        assert project.status == ProjectStatus.OPEN

    def test_declared_edge_is_reachable(self, project_service) -> None:
        """Every declared transition edge is reachable via its witnessing rule."""
        # open ──CloseProject──→ closed
        root = PurePosixPath("e1")
        p, _ = project_service.open_project(root)
        project_service.close_project(p)
        assert p.status == ProjectStatus.CLOSED

        # open ──ReloadProject──→ open
        root2 = PurePosixPath("e2")
        p2, _ = project_service.open_project(root2)
        project_service.reload_project(p2)
        assert p2.status == ProjectStatus.OPEN


class TestInvalidTransitions:
    """Tests that undeclared transitions are rejected."""

    def test_cannot_close_closed_project(self, project_service) -> None:
        root = PurePosixPath("cc")
        p, _ = project_service.open_project(root)
        project_service.close_project(p)
        with pytest.raises(ValueError, match="not open"):
            project_service.close_project(p)

    def test_cannot_reload_closed_project(self, project_service) -> None:
        root = PurePosixPath("rc")
        p, _ = project_service.open_project(root)
        project_service.close_project(p)
        with pytest.raises(ValueError, match="not open"):
            project_service.reload_project(p)


class TestReachability:
    """Reachability tests: walk complete lifecycle paths."""

    def test_create_to_terminal(self, project_service) -> None:
        """Walk from initial state through to terminal state (closed)."""
        root = PurePosixPath("lifecycle")
        p, _ = project_service.open_project(root)  # created → open
        assert p.is_open
        project_service.close_project(p)  # open → closed
        assert not p.is_open

    def test_create_reload_close(self, project_service) -> None:
        """Walk: open → reload → close."""
        root = PurePosixPath("lcr")
        p, _ = project_service.open_project(root)
        project_service.reload_project(p)
        assert p.is_open
        project_service.close_project(p)
        assert not p.is_open

    def test_full_lifecycle(self, project_service) -> None:
        """Walk: open → reload → reload → close."""
        root = PurePosixPath("full")
        p, _ = project_service.open_project(root)
        project_service.reload_project(p)
        project_service.reload_project(p)
        project_service.close_project(p)
        assert p.status == ProjectStatus.CLOSED
