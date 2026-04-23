"""Implementation details — bases import from __init__.py, not here."""

from __future__ import annotations


def greet(name: str) -> str:
    """Generate a greeting for the given name."""
    return f"Hello, {name}!"


def greet_all(names: list[str]) -> list[str]:
    """Generate greetings for a list of names."""
    return [greet(n) for n in names]
