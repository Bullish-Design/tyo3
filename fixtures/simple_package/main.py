"""Simple test package for TyO3 end-to-end testing."""

def greet(name: str) -> str:
    """Greet someone by name."""
    return f"Hello, {name}!"


class MyClass:
    """A test class."""

    def __init__(self, value: int) -> None:
        self._value = value

    def get_val(self) -> int:
        """Return the stored value."""
        return self._value


x: int = 42
result = greet("world")
