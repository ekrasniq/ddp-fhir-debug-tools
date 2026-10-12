#!/usr/bin/env python3
"""COVID ICU debug with DDP identifier/VN mapping only."""

import sqlite3
import sys

from ddp_covid_icu_debug import main

if __name__ == "__main__":
    try:
        main(default_mode="without_partof")
    except (RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"Debug-Lauf abgebrochen: {exc}", file=sys.stderr)
        sys.exit(1)
