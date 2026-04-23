"""greeter — minimal Polylith component example.

Interface: re-export public API only.
Bases import from here, never from core.py directly.
"""

from .core import greet, greet_all

__all__ = ["greet", "greet_all"]
