"""Scraper morizon.pl — mieszkania na sprzedaż, mieszkania na wynajem, pokoje.

Morizon i gratka to ten sam backend (Grupa Morizon-Gratka, Nuxt 3, to samo
API GraphQL — tu pod ``/api-morizon``), więc korzystamy ze wspólnego silnika
z gratka.py. Różnią się tylko adresy, parametry filtrów i ID ofert ("mzn...").
Uwaga: oferty w obu portalach w dużej mierze się pokrywają (te same ogłoszenia).

Adresy (zweryfikowane):
  sprzedaż: /mieszkania/wroclaw/?ps[market_type]=2 (wtórny) / =1 (pierwotny)
  wynajem:  /do-wynajecia/mieszkania/wroclaw/
  pokoje:   /do-wynajecia/pokoje/wroclaw/
"""
from __future__ import annotations

from typing import List, Optional
from urllib.parse import urlencode

from scraper.models import Listing
from scraper.sources.gratka import Api, _demo, city_slug, scrape_portal

SOURCE = "morizon"
DEALS = ("sprzedaz", "wynajem", "pokoj")

MORIZON_BASE = "https://www.morizon.pl"
MORIZON_THUMBS = "https://img1.staticmorizon.com.pl"
MORIZON_API = Api("https://www.morizon.pl/api-morizon", "MORIZON", "WWW_2_0", MORIZON_BASE)

_MARKET_TYPE = {"pierwotny": 1, "wtorny": 2}


def _morizon_url(config: dict, deal: str, market: Optional[str], page: int) -> str:
    city = city_slug(config)  # np. "wroclaw", "wroclawski/miasto-siechnice"
    pf = config.get("prefilter") or {}
    params = []
    if deal == "sprzedaz":
        params.append(("ps[market_type]", _MARKET_TYPE[market or "wtorny"]))
    if pf.get("price_min"):
        params.append(("ps[price_from]", int(pf["price_min"])))
    if pf.get("price_max"):
        params.append(("ps[price_to]", int(pf["price_max"])))
    if pf.get("area_min"):
        params.append(("ps[living_area_from]", int(pf["area_min"])))
    if page > 1:
        params.append(("page", page))
    if deal == "pokoj":
        path = f"/do-wynajecia/pokoje/{city}/"
    elif deal == "wynajem":
        path = f"/do-wynajecia/mieszkania/{city}/"
    else:
        path = f"/mieszkania/{city}/"
    return f"{MORIZON_BASE}{path}" + (f"?{urlencode(params)}" if params else "")


def scrape(config: dict, deal: str = "sprzedaz") -> List[Listing]:
    if deal not in DEALS:
        raise ValueError(f"{SOURCE}: nieobsługiwany typ ogłoszenia {deal!r}")
    return scrape_portal(config, source=SOURCE, base_url=MORIZON_BASE,
                         thumb_host=MORIZON_THUMBS, build_url=_morizon_url,
                         api=MORIZON_API, deal=deal)


if __name__ == "__main__":
    _demo(scrape, SOURCE)
