"""Main module that imports from math_ops."""

from math_ops import add, multiply, PI


def calculate(a: int, b: int) -> int:
    """Calculate using imported functions."""
    result: int = add(a, b)
    doubled: int = multiply(result, 2)
    return doubled


circumference: float = 2.0 * PI * 10.0
