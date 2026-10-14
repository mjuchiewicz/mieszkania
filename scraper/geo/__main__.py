"""Uzupełnia przybliżone lokalizacje w istniejących docs/data/<miejscowość>/<typ>.json bez pobierania ofert.

    python -m scraper.geo                     # wszystkie typy, wszystkie miejscowości z danymi
    python -m scraper.geo sprzedaz            # wybrane typy
    python -m scraper.geo --city krakow       # wybrana miejscowość
"""
from __future__ import annotations

import argparse
import sys

from scraper.cities import DEFAULT_CITY, get_city
from scraper.geo import geocode_listings
from scraper.main import DATA_DIR, city_bbox, deal_path, load_state, save_state
from scraper.models import DEALS


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("deals", nargs="*", default=None)
    ap.add_argument("--city", action="append", default=None)
    args = ap.parse_args(argv)
    deals = args.deals or list(DEALS)
    cities = args.city or sorted(p.name for p in DATA_DIR.iterdir() if p.is_dir())
    for city in cities:
        entry = get_city(city) or {}
        for deal in deals:
            if not deal_path(deal, city).exists():
                continue
            listings = list(load_state(deal, city).values())
            stats = geocode_listings(listings, city=city,
                                     bbox=None if city == DEFAULT_CITY else city_bbox(entry))
            save_state(deal, listings, city)
            print("%s/%s: %s" % (city, deal, ", ".join("%s=%d" % kv for kv in sorted(stats.items()))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
