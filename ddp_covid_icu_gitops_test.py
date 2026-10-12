#!/usr/bin/env python3
"""Next run: audited medic-prod/-test settings plus native/contactart comparison.

Only public configuration values, no internal endpoint or credentials. All defaults
can be overridden by CLI options. See docs/covid-icu-native-next-run.md.
"""

import sqlite3
import sys

from ddp_covid_icu_debug import main

PCR_CODES = "94640-0,94306-8,96763-8,94500-6,94558-4"
DEFAULTS = {
    "preset_name": "gitops_test_2026-10-12",
    "mode": "without_partof",
    "icu_detection": "both",
    "ddp_pcr_loincs": PCR_CODES,
    "ddp_variant_loincs": "96895-8,96741-4," + PCR_CODES,
    "condition_link_mode": "revinclude",
    "filter_patient_retrieval": True,
    "filter_resources_by_date": True,
    "page_size": 150,
    "patient_batch_size": 25,
    "location_batch_size": 150,
    "reference_ddp_count": 65,
}


if __name__ == "__main__":
    try:
        main(defaults=DEFAULTS)
    except (RuntimeError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"Debug-Lauf abgebrochen: {exc}", file=sys.stderr)
        sys.exit(1)
