"""Standalone Python script — no package/__init__.py.

Tests that TyO3 can handle single-file projects without a package structure.
"""


def standalone_greeting(name: str) -> str:
    """A simple greeting function."""
    return f"Hello from standalone, {name}!"


class StandaloneCounter:
    """A counter class in a standalone script."""

    def __init__(self, start: int = 0) -> None:
        self._count = start

    def increment(self) -> int:
        """Increment and return the counter."""
        self._count += 1
        return self._count

    def reset(self) -> None:
        """Reset the counter to zero."""
        self._count = 0


counter: StandaloneCounter = StandaloneCounter(10)
