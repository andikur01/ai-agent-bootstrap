#!/usr/bin/env python3
"""Convenience entrypoint. Same as `python -m app.main`."""
import sys

from app.main import main

if __name__ == "__main__":
    sys.exit(main())
