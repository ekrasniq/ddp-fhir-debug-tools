#!/usr/bin/env python3
"""Generate infl.cumulative.gender without Observation/LOINC retrieval."""

from __future__ import annotations

import ddp_infl_cumulative_items as infl


# This keeps the item name and all other influenza cumulative settings, but skips
# the expensive Observation search completely.
infl.USE_OBSERVATIONS = False
infl.USE_CONDITIONS = True


if __name__ == "__main__":
    infl.main()
