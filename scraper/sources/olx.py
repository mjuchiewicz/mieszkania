"""Scraper OLX — mieszkania na sprzedaż we Wrocławiu.

Korzysta z publicznego JSON API frontendu OLX:
    https://www.olx.pl/api/v1/offers/
category_id=14  -> Nieruchomości / Mieszkania / Sprzedaż
region_id=3     -> Dolnośląskie
city_id=19701   -> Wrocław
(wartości potwierdzone w window.__PRERENDERED_STATE__ strony
 https://www.olx.pl/nieruchomosci/mieszkania/sprzedaz/wroclaw/)

API ogranicza offset do 0..1000 (max ~1040 ofert na zapytanie), więc zakres
cen dzielimy adaptacyjnie na pasma mieszczące się w tym limicie. Oferty zmirrorowane z otodom (external_url / partner otodom_pl)
są pomijane — otodom scrapujemy osobno.
"""
from __future__ import annotations

import html
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from scraper.http import make_session, polite_sleep
from scraper.models import Listing

log = logging.getLogger(__name__)

SOURCE = "olx"
API_URL = "https://www.olx.pl/api/v1/offers/"
CATEGORY_ID = 14      # mieszkania / sprzedaż
REGION_ID = 3         # dolnośląskie
CITY_ID = 19701       # Wrocław
PAGE_SIZE = 40
MAX_OFFSET = 1000     # limit walidacji API
BAND_TARGET = 950     # pasmo cenowe z większą liczbą ofert dzielimy na pół
MIN_BAND_WIDTH = 2000  # zł — węższych pasm już nie dzielimy
DEFAULT_PRICE_MAX = 20_000_000
MAX_REQUESTS = 90     # globalny limit zapytań na przebieg (~3 min z polite_sleep)

ROOMS_MAP = {"one": 1, "two": 2, "three": 3, "four": 4}


def _build_params(config: dict, offset: int, price_from: Optional[int] = None,
                  price_to: Optional[int] = None) -> Dict[str, Any]:
    params: Dict[str, Any] = {
        "offset": offset,
        "limit": PAGE_SIZE,
        "category_id": CATEGORY_ID,
        "region_id": REGION_ID,
        "city_id": CITY_ID,
        "sort_by": "created_at:desc",
    }
    pre = config.get("prefilter") or {}
    if price_from is not None:
        params["filter_float_price:from"] = int(price_from)
    if price_to is not None:
        params["filter_float_price:to"] = int(price_to)
    if pre.get("area_min"):
        params["filter_float_m:from"] = pre["area_min"]
    return params


def _is_otodom(item: dict) -> bool:
    ext = item.get("external_url") or ""
    partner = (item.get("partner") or {}).get("code") or ""
    return "otodom" in ext or "otodom" in partner or "otodom.pl" in (item.get("url") or "")


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        return None


def _clean_description(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    t = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    t = re.sub(r"\s+", " ", t).strip()
    if len(t) > 500:
        t = t[:497].rstrip() + "..."
    return t or None


def _parse_item(item: dict) -> Listing:
    params = {p.get("key"): p for p in item.get("params") or [] if isinstance(p, dict)}

    def pval(key: str) -> dict:
        return (params.get(key) or {}).get("value") or {}

    price = None
    pv = pval("price")
    if pv.get("value") is not None and (pv.get("currency") in (None, "PLN")):
        price = int(round(float(pv["value"])))

    area = _to_float(pval("m").get("key"))

    rooms = ROOMS_MAP.get(pval("rooms").get("key"))

    floor = None
    fk = pval("floor_select").get("key") or ""
    flabel = pval("floor_select").get("label")
    if fk == "floor_0" or (flabel and "parter" in flabel.lower()):
        floor = "parter"
    elif flabel:
        floor = str(flabel)

    market = {"primary": "pierwotny", "secondary": "wtorny"}.get(pval("market").get("key"))

    if item.get("business"):
        seller = "deweloper" if market == "pierwotny" else "agencja"
    else:
        seller = "prywatny"

    loc = item.get("location") or {}
    district = (loc.get("district") or {}).get("name")

    images: List[str] = []
    for ph in item.get("photos") or []:
        link = ph.get("link")
        if link:
            images.append(link.replace("{width}", "1024").replace("{height}", "768"))

    m = item.get("map") or {}
    lat = _to_float(m.get("lat"))
    lon = _to_float(m.get("lon"))

    return Listing(
        source=SOURCE,
        id=str(item["id"]),
        url=item["url"],
        title=(item.get("title") or "").strip(),
        price=price,
        area=area,
        rooms=rooms,
        floor=floor,
        district=district,
        market=market,
        seller=seller,
        images=images,
        description=_clean_description(item.get("description")),
        lat=lat,
        lon=lon,
        created_at=item.get("created_time"),
    )


def _price_range(config: dict) -> Tuple[int, int]:
    pre = config.get("prefilter") or {}
    lo = int(pre.get("price_min") or 0)
    hi = int(pre.get("price_max") or DEFAULT_PRICE_MAX)
    return lo, hi


def scrape(config: dict) -> List[Listing]:
    """Pobiera oferty, dzieląc zakres cen na pasma tak, by każde mieściło się
    w limicie offsetu API (adaptacyjnie: pasmo z > BAND_TARGET ofert dzielone na pół)."""
    session = make_session()
    session.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.olx.pl/nieruchomosci/mieszkania/sprzedaz/wroclaw/",
    })
    max_pages = int(config.get("max_pages") or 1)   # limit stron na pasmo

    results: List[Listing] = []
    seen: Set[str] = set()
    stats = {"otodom": 0, "requests": 0}

    def fetch(offset: int, lo: int, hi: int) -> dict:
        if stats["requests"] > 0:
            polite_sleep()
        stats["requests"] += 1
        resp = session.get(API_URL, params=_build_params(config, offset, lo, hi), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data.get("data"), list):
            raise ValueError("brak listy 'data' w odpowiedzi API")
        return data

    def consume(items: list) -> None:
        for item in items:
            try:
                if _is_otodom(item):
                    stats["otodom"] += 1
                    continue
                city = ((item.get("location") or {}).get("city") or {}).get("id")
                if city is not None and city != CITY_ID:
                    continue
                listing = _parse_item(item)
                if listing.id in seen:
                    continue
                seen.add(listing.id)
                results.append(listing)
            except Exception as e:  # jedna zepsuta oferta nie zabija przebiegu
                log.warning("olx: pominięto ofertę %s: %s",
                            item.get("id") if isinstance(item, dict) else "?", e)

    lo0, hi0 = _price_range(config)
    bands: List[Tuple[int, int]] = [(lo0, hi0)]
    first = True

    while bands:
        lo, hi = bands.pop(0)
        if stats["requests"] >= MAX_REQUESTS:
            log.warning("olx: osiągnięto globalny limit %d zapytań — pomijam pasmo %d-%d i dalsze",
                        MAX_REQUESTS, lo, hi)
            break
        try:
            data = fetch(0, lo, hi)
        except Exception as e:
            if first:
                raise RuntimeError(
                    f"olx: nie udało się pobrać pierwszej strony API ({API_URL}): {e}") from e
            log.warning("olx: błąd pasma %d-%d: %s — pomijam", lo, hi, e)
            continue
        first = False

        meta = data.get("metadata") or {}
        total = int(meta.get("visible_total_count") or meta.get("total_elements") or 0)
        if total > BAND_TARGET and hi - lo > MIN_BAND_WIDTH:
            mid = (lo + hi) // 2
            log.info("olx: pasmo %d-%d ma %d ofert — dzielę", lo, hi, total)
            consume(data["data"])  # ta strona i tak już pobrana
            bands[0:0] = [(lo, mid), (mid + 1, hi)]
            continue

        log.info("olx: pasmo %d-%d: %d ofert", lo, hi, total)
        consume(data["data"])
        pages_needed = min(max_pages, (min(total, MAX_OFFSET + PAGE_SIZE) + PAGE_SIZE - 1) // PAGE_SIZE)
        for page in range(1, pages_needed):
            offset = page * PAGE_SIZE
            if offset > MAX_OFFSET:
                break
            if stats["requests"] >= MAX_REQUESTS:
                log.warning("olx: globalny limit zapytań w trakcie pasma %d-%d", lo, hi)
                break
            try:
                data = fetch(offset, lo, hi)
            except Exception as e:
                log.warning("olx: błąd pasma %d-%d offset %d: %s — przerywam pasmo", lo, hi, offset, e)
                break
            if not data["data"]:
                break
            consume(data["data"])
            if not (data.get("links") or {}).get("next"):
                break

    log.info("olx: %d ofert (pominięto %d mirrorów otodom, %d zapytań)",
             len(results), stats["otodom"], stats["requests"])
    return results


if __name__ == "__main__":
    import json
    import sys
    from pathlib import Path

    import yaml

    logging.basicConfig(level=logging.INFO)
    cfg_path = Path(__file__).resolve().parents[2] / "config.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if "--full" not in sys.argv:  # --full: pełny przebieg z max_pages z configu
        cfg["max_pages"] = 2
    listings = scrape(cfg)
    print(f"Pobrano {len(listings)} ofert")
    for l in listings[:3]:
        print(json.dumps(l.to_dict(), ensure_ascii=False, indent=2))
