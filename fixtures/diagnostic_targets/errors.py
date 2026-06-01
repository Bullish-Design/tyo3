"""Module with deliberate type errors for diagnostic tests."""


def type_mismatch(a: int) -> str:
    """Returns a string but we use integer return."""
    return a  # type: ignore — deliberate type error: int returned where str expected


def missing_arg(x: int, y: str) -> int:
    """Requires two arguments."""
    return len(y) + x


# Call with wrong argument count
# result = missing_arg(5)  # too few arguments (commented out to avoid parse error)


def unused_import() -> None:
    """Function that imports but doesn't use (for warning)."""
    import os  # noqa — imported but unused
    return None


# Variable with type annotation mismatch
x: str = 42  # type: ignore — deliberate: int assigned to str variable


def wrong_operation(items: list[int]) -> int:
    """Performs an operation on list items."""
    total: int = 0
    for item in items:
        total = total + item
    return total  # this is fine, but the list[int] contains strs below


# This demonstrates type issues:
str_list: list[str] = ["1", "2", "3"]
# wrong_operation(str_list)  # would fail: list[str] vs list[int]
