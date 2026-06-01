"""Module with Unicode identifiers and positions to test edge cases.

Includes:
- Non-ASCII identifiers
- Multi-byte UTF-8 characters
- Emoji in comments
"""

# Unicode identifier with Greek letters
α: float = 3.14159
β: float = 2.71828

def καλημέρα(name: str) -> str:
    """Greek greeting function."""
    return f"Καλημέρα, {name}!"


# Variable with accented characters
café_price: int = 5

# Japanese identifier
挨拶: str = "こんにちは"

def 挨拶する(name: str) -> str:
    """Japanese greeting function."""
    return f"こんにちは、{name}さん"

# Multi-byte character positions — emoji in strings and comments
emoji_greeting: str = "Hello 👋 World 🌍"  # 👋 is at offset, 🌍 is at offset

def describe_emoji() -> str:
    """✨ Returns a description with emoji. 🎉"""
    return "This function ✨ has emoji everywhere! 🎉🚀"
