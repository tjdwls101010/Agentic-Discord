# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""discord -- read Discord as the user, from the command line."""

from __future__ import annotations

import sys

from discord import __version__


def main(argv: list[str] | None = None) -> int:
    print(__version__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
