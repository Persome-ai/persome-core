"""Module entry point for `python -m persome` and PyInstaller builds.

PyInstaller treats whatever script it's pointed at as ``__main__``. If we
point it at ``cli.py``, the ``from . import paths`` style relative imports
inside that file fail because ``__main__`` is not a package. This shim is
the public entry: it imports the Typer ``app`` by absolute path and invokes
it, which lets ``cli.py`` be loaded as ``persome.cli`` so its
relative imports resolve correctly.
"""

from __future__ import annotations

from persome.cli import app


def main() -> None:
    app()


if __name__ == "__main__":
    main()
