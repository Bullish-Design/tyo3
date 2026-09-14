"""Tests for the native extension build-profile guard."""

from __future__ import annotations

import pytest

import tyo3


def test_build_profile_reports_loaded_profile() -> None:
    """The runtime probe reports only a supported profile value."""
    assert tyo3.build_profile() in {"debug", "release", "unknown"}


def test_require_release_build_rejects_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    """A debug extension must never be accepted for performance evidence."""
    monkeypatch.setattr(tyo3, "build_profile", lambda: "debug")

    with pytest.raises(RuntimeError, match="loaded profile is 'debug'"):
        tyo3.require_release_build()


def test_require_release_build_accepts_release(monkeypatch: pytest.MonkeyPatch) -> None:
    """A release extension passes the performance evidence guard."""
    monkeypatch.setattr(tyo3, "build_profile", lambda: "release")

    assert tyo3.require_release_build() is None
