"""Entry point: ``python -m runner``.

Thin wrapper so the standalone poster can be invoked as a module with
``PYTHONPATH=src`` (same convention as ``python -m scheduler``).
"""

from src.runner.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
