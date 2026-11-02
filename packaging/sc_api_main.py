"""PyInstaller entry point for the sc-api server executable.

A dedicated entry script rather than freezing `scapi/server.py` directly: that
module is also a `python -m scapi.server` target, and PyInstaller treats its
entry script as `__main__`, which would make the module's own import of the
`scapi` package ambiguous.
"""
import sys

from scapi.server import main

if __name__ == "__main__":
    sys.exit(main())
