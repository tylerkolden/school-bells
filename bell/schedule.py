"""Compatibility CLI for ``python -m bell.schedule show``."""

from bell.scheduler import main

if __name__ == "__main__":
    raise SystemExit(main())
