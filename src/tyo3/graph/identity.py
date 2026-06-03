"""Symbol identity construction for the code graph."""

from __future__ import annotations

from tyo3.models.symbols import Symbol


def make_symbol_id(file: str, qualified_name: str) -> str:
    """Create a canonical symbol ID from a file path and qualified name."""
    return f"{file}::{qualified_name}"


def make_external_id(package: str, qualified_name: str) -> str:
    """Create a canonical symbol ID for an external (dependency) symbol."""
    return f"{package}::{qualified_name}"


def symbol_id_from_symbol(file: str, symbol: Symbol) -> str:
    """Derive a symbol_id from a TyO3 Symbol model.

    Uses qualified_name if available, otherwise falls back to
    name@line for uniqueness.
    """
    if symbol.qualified_name:
        return make_symbol_id(file, symbol.qualified_name)
    return make_symbol_id(file, f"{symbol.name}@{symbol.location.range.start.line}")


def file_from_symbol_id(symbol_id: str) -> str:
    """Extract the file path (or package name) from a symbol_id."""
    return symbol_id.split("::", 1)[0]


def qualified_name_from_symbol_id(symbol_id: str) -> str:
    """Extract the qualified name from a symbol_id."""
    return symbol_id.split("::", 1)[1]
