"""`python -m dipbot` giriş noktası."""
from __future__ import annotations

import sys

from .tg.calistir import main

if __name__ == "__main__":
    sys.exit(main())
