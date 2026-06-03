"""Minimal file with an unsuppressed type error for graph testing."""


def bad_add(a: int, b: int) -> str:
    return a + b  # intentional type error: int returned where str expected
