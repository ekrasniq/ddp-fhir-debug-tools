#!/usr/bin/env python3
"""COVID ICU debug with DDP VN plus VK.partOf -> AK.partOf -> EK mapping."""

import sqlite3
import sys

from ddp_covid_icu_debug import main

if __name__ == "__main__":
    try:
        main(default_mode="with_partof")
    except (RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"Debug-Lauf abgebrochen: {exc}", file=sys.stderr)
        sys.exit(1)
