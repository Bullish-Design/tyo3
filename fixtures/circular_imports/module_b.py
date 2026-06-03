"""Module B — imports from Module A, completing the circular dependency."""

from module_a import get_name


def get_greeting() -> str:
    return "Hello"


def greet_reversed() -> str:
    return get_name() + " says " + get_greeting()
