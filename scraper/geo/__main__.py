"""Uzupełnia przybliżone lokalizacje w istniejących docs/data/<typ>.json bez pobierania ofert.

    python -m scraper.geo            # wszystkie typy
    python -m scraper.geo sprzedaz   # wybrane
"""
from __future__ import annotations

import sys

from scraper.geo import geocode_listings
from scraper.main import deal_path, load_state, save_state
from scraper.models import DEALS


def main(argv=None) -> int:
    deals = (argv if argv is not None else sys.argv[1:]) or list(DEALS)
    for deal in deals:
        if not deal_path(deal).exists():
            continue
        listings = list(load_state(deal).values())
        stats = geocode_listings(listings)
        save_state(deal, listings)
        print("%s: %s" % (deal, ", ".join("%s=%d" % kv for kv in sorted(stats.items()))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
