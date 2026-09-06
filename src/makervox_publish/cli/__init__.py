"""Command line entry points.

`pyproject.toml` declares the console script ``makervox-publish``; this package
is what it points at. Keep the import surface here small — the CLI must not drag
platform modules in at import time, so every command imports what it needs
inside the function.
"""

from makervox_publish.cli.main import main

__all__ = ["main"]
