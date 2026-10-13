"""Scraper morizon.pl — mieszkania na sprzedaż.

Morizon i gratka to ten sam backend (Grupa Morizon-Gratka, Nuxt 3, identyczny
format __NUXT_DATA__), więc korzystamy ze wspólnego silnika z gratka.py.
Różnią się tylko adresy, parametry filtrów i ID ofert ("mzn...").
Uwaga: oferty w obu portalach w dużej mierze się pokrywają (te same ogłoszenia).
"""
from __future__ import annotations

from typing import List
from urllib.parse import urlencode

from scraper.models import Listing
from scraper.sources.gratka import _demo, scrape_portal

SOURCE = "morizon"
MORIZON_BASE = "https://www.morizon.pl"
MORIZON_THUMBS = "https://img1.staticmorizon.com.pl"

_MARKET_TYPE = {"pierwotny": 1, "wtorny": 2}


def _morizon_url(config: dict, market: str, page: int) -> str:
    city = config.get("city") or "wroclaw"
    pf = config.get("prefilter") or {}
    params = [("ps[market_type]", _MARKET_TYPE[market])]
    if pf.get("price_min"):
        params.append(("ps[price_from]", int(pf["price_min"])))
    if pf.get("price_max"):
        params.append(("ps[price_to]", int(pf["price_max"])))
    if pf.get("area_min"):
        params.append(("ps[living_area_from]", int(pf["area_min"])))
    if page > 1:
        params.append(("page", page))
    return f"{MORIZON_BASE}/mieszkania/{city}/?{urlencode(params)}"


def scrape(config: dict) -> List[Listing]:
    return scrape_portal(config, source=SOURCE, base_url=MORIZON_BASE,
                         thumb_host=MORIZON_THUMBS, build_url=_morizon_url)


if __name__ == "__main__":
    _demo(scrape)
