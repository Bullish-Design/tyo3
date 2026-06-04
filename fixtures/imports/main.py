"""Main module that imports from math_ops."""

from math_ops import PI, add, multiply


def calculate(a: int, b: int) -> int:
    """Calculate using imported functions."""
    result: int = add(a, b)
    doubled: int = multiply(result, 2)
    return doubled


circumference: float = 2.0 * PI * 10.0
