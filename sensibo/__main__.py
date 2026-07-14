"""Entry point for ``python -m sensibo``."""

from __future__ import annotations

import sys

from sensibo.cli import main

if __name__ == "__main__":
    sys.exit(main())
