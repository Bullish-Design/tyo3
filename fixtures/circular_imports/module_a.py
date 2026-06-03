"""Module A — imports from Module B, creating a circular dependency."""

from module_b import get_greeting


def get_name() -> str:
    return "World"


def greet() -> str:
    return get_greeting() + ", " + get_name() + "!"
