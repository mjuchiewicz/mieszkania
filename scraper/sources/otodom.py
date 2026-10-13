"""Scraper Otodom — mieszkania na sprzedaż we Wrocławiu.

Strony wyników to Next.js; dane ofert siedzą w <script id="__NEXT_DATA__">
pod props.pageProps.data.searchAds.items. Lista nie zawiera pola "rynek",
więc pobieramy osobno rynek wtórny i pierwotny (filtr market=...) i rynek
przypisujemy z zapytania. Strony obu rynków są przeplatane, a łączny limit
żądań to config["max_pages"].
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from scraper.http import make_session, polite_sleep
from scraper.models import Listing

log = logging.getLogger(__name__)

SOURCE = "otodom"
BASE = "https://www.otodom.pl"
SEARCH_URL = BASE + "/pl/wyniki/sprzedaz/mieszkanie/dolnoslaskie/wroclaw/wroclaw/wroclaw"
LIMIT = 72

MARKETS = (("SECONDARY", "wtorny"), ("PRIMARY", "pierwotny"))

_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S
)

_NUM_WORDS = {
    "ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5, "SIX": 6,
    "SEVEN": 7, "EIGHT": 8, "NINE": 9, "TEN": 10,
}
_FLOORS = {
    "CELLAR": "suterena", "GROUND": "parter", "FIRST": "1", "SECOND": "2",
    "THIRD": "3", "FOURTH": "4", "FIFTH": "5", "SIXTH": "6", "SEVENTH": "7",
    "EIGHTH": "8", "NINTH": "9", "TENTH": "10", "ABOVE_TENTH": ">10",
    "HIGHER_THAN_TENTH": ">10", "GARRET": "poddasze",
}


class OtodomError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Pobieranie
# --------------------------------------------------------------------------- #

def _build_params(config: dict, market: str, page: int) -> Dict[str, Any]:
    pre = config.get("prefilter") or {}
    params: Dict[str, Any] = {
        "limit": LIMIT,
        "market": market,
        "by": "LATEST",
        "direction": "DESC",
        "page": page,
    }
    if pre.get("price_min"):
        params["priceMin"] = int(pre["price_min"])
    if pre.get("price_max"):
        params["priceMax"] = int(pre["price_max"])
    if pre.get("area_min"):
        params["areaMin"] = pre["area_min"]
    return params


def _fetch_search(session, params: Dict[str, Any]) -> Tuple[List[dict], dict]:
    """Zwraca (items, pagination). Rzuca wyjątek przy błędzie."""
    resp = session.get(SEARCH_URL, params=params, timeout=30)
    resp.raise_for_status()
    m = _NEXT_DATA_RE.search(resp.text)
    if not m:
        raise OtodomError(
            "brak __NEXT_DATA__ w odpowiedzi (HTTP %s, %d B) — możliwa blokada/captcha"
            % (resp.status_code, len(resp.text))
        )
    data = json.loads(m.group(1))
    search_ads = (((data.get("props") or {}).get("pageProps") or {})
                  .get("data") or {}).get("searchAds")
    if not isinstance(search_ads, dict):
        raise OtodomError("nieoczekiwana struktura __NEXT_DATA__ (brak searchAds)")
    items = search_ads.get("items") or []
    pagination = search_ads.get("pagination") or {}
    return items, pagination


# --------------------------------------------------------------------------- #
# Parsowanie
# --------------------------------------------------------------------------- #

def _money(obj: Any) -> Optional[int]:
    if isinstance(obj, dict) and obj.get("value") is not None:
        try:
            return int(round(float(obj["value"])))
        except (TypeError, ValueError):
            return None
    return None


def _rooms(val: Any) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, int):
        return val
    s = str(val).upper()
    if s in _NUM_WORDS:
        return _NUM_WORDS[s]
    if s.startswith("MORE"):
        return 10
    try:
        return int(s)
    except ValueError:
        return None


def _floor(val: Any) -> Optional[str]:
    if not val:
        return None
    s = str(val).upper()
    if s in _FLOORS:
        return _FLOORS[s]
    m = re.search(r"(\d+)", s)
    return m.group(1) if m else s.lower()


def _district(location: dict) -> Optional[str]:
    """Najbardziej szczegółowa lokalizacja poniżej miasta (osiedle > dzielnica)."""
    locs = ((location or {}).get("reverseGeocoding") or {}).get("locations") or []
    by_level = {l.get("locationLevel"): l.get("name") for l in locs if isinstance(l, dict)}
    for level in ("residential", "district"):
        if by_level.get(level):
            return by_level[level]
    return None


def _street(location: dict) -> Optional[str]:
    st = (((location or {}).get("address") or {}).get("street") or {})
    name = st.get("name")
    if not name:
        return None
    if st.get("number"):
        name = "%s %s" % (name, st["number"])
    return name


def _seller(item: dict) -> Optional[str]:
    agency_type = ((item.get("agency") or {}).get("type") or "").upper()
    if agency_type == "DEVELOPER":
        return "deweloper"
    if agency_type == "AGENCY":
        return "agencja"
    if item.get("isPrivateOwner"):
        return "prywatny"
    ext = (item.get("extendedAdvertiserType") or "").lower()
    return {"private_person": "prywatny", "agency": "agencja",
            "developer": "deweloper"}.get(ext)


def _images(item: dict) -> List[str]:
    out = []
    for img in item.get("images") or []:
        if not isinstance(img, dict):
            continue
        url = img.get("large") or img.get("medium") or img.get("small")
        if url:
            out.append(url)
    return out


def _url(item: dict) -> str:
    slug = item.get("slug")
    if slug:
        return "%s/pl/oferta/%s" % (BASE, slug)
    href = (item.get("href") or "").replace("[lang]/ad/", "pl/oferta/").replace("[lang]", "pl")
    return "%s/%s" % (BASE, href.lstrip("/"))


def _parse_item(item: dict, market: str) -> Optional[Listing]:
    if item.get("estate") != "FLAT":
        return None
    if item.get("transaction") not in (None, "SELL"):
        return None
    price = None if item.get("hidePrice") else _money(item.get("totalPrice"))
    area = item.get("areaInSquareMeters")
    location = item.get("location") or {}
    desc = item.get("shortDescription")
    if isinstance(desc, str):
        desc = desc.strip()[:500] or None
    else:
        desc = None
    return Listing(
        source=SOURCE,
        id=str(item["id"]),
        url=_url(item),
        title=(item.get("title") or "").strip(),
        price=price,
        area=float(area) if area is not None else None,
        rooms=_rooms(item.get("roomsNumber")),
        floor=_floor(item.get("floorNumber")),
        district=_district(location),
        street=_street(location),
        market=market,
        seller=_seller(item),
        images=_images(item),
        description=desc,
        created_at=item.get("createdAtFirst") or item.get("dateCreated"),
    )


def _within_prefilter(l: Listing, config: dict) -> bool:
    """Dla ofert z 'relatedAds' inwestycji (portal ich nie filtruje)."""
    pre = config.get("prefilter") or {}
    if l.price is None:
        return False
    if pre.get("price_min") and l.price < pre["price_min"]:
        return False
    if pre.get("price_max") and l.price > pre["price_max"]:
        return False
    if pre.get("area_min") and (l.area is None or l.area < pre["area_min"]):
        return False
    return True


def _iter_units(items: Iterable[dict]) -> Iterable[Tuple[dict, bool]]:
    """(item, is_related) — rozwija inwestycje do pojedynczych lokali."""
    for it in items:
        if not isinstance(it, dict):
            continue
        if it.get("estate") == "INVESTMENT":
            for rel in it.get("relatedAds") or []:
                if isinstance(rel, dict):
                    yield rel, True
        else:
            yield it, False


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

def scrape(config: dict) -> List[Listing]:
    max_pages = int(config.get("max_pages") or 1)
    session = make_session()
    results: List[Listing] = []
    seen: Set[str] = set()

    # stan per rynek: następna strona lub None gdy skończone
    next_page: Dict[str, Optional[int]] = {m: 1 for m, _ in MARKETS}
    market_label = dict(MARKETS)
    requests_done = 0
    any_page_ok = False
    first_errors: List[str] = []

    while requests_done < max_pages and any(p is not None for p in next_page.values()):
        for market, _label in MARKETS:
            page = next_page[market]
            if page is None or requests_done >= max_pages:
                continue
            if requests_done:
                polite_sleep()
            requests_done += 1
            params = _build_params(config, market, page)
            try:
                items, pagination = _fetch_search(session, params)
            except Exception as e:  # noqa: BLE001
                log.warning("otodom %s strona %d: błąd pobierania: %s", market, page, e)
                if page == 1:
                    first_errors.append("%s: %s" % (market, e))
                next_page[market] = None
                continue

            current = pagination.get("currentPage")
            total_pages = pagination.get("totalPages")
            # Otodom przy stronie poza zakresem przekierowuje na ostatnią/zmienioną
            if current is not None and int(current) != page:
                log.info("otodom %s: strona %d poza zakresem (currentPage=%s) — koniec",
                         market, page, current)
                next_page[market] = None
                continue
            any_page_ok = True

            added = 0
            for raw, is_related in _iter_units(items):
                try:
                    listing = _parse_item(raw, market_label[market])
                except Exception as e:  # noqa: BLE001
                    log.warning("otodom: pominięto błędną ofertę id=%s: %s",
                                raw.get("id") if isinstance(raw, dict) else "?", e)
                    continue
                if listing is None or listing.id in seen:
                    continue
                if listing.price is None:
                    continue
                if is_related and not _within_prefilter(listing, config):
                    continue
                seen.add(listing.id)
                results.append(listing)
                added += 1
            log.info("otodom %s strona %d/%s: %d ofert (%d nowych)",
                     market, page, total_pages, len(items), added)

            if not items or (total_pages is not None and page >= int(total_pages)):
                next_page[market] = None
            else:
                next_page[market] = page + 1

    if not any_page_ok:
        raise OtodomError(
            "Otodom: nie udało się pobrać pierwszej strony wyników (%s)"
            % "; ".join(first_errors or ["brak wyników"])
        )
    log.info("otodom: łącznie %d ofert z %d żądań", len(results), requests_done)
    return results


if __name__ == "__main__":
    import os
    import yaml

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(root, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["max_pages"] = 2
    out = scrape(cfg)
    print("count:", len(out))
    print(json.dumps([l.to_dict() for l in out[:3]], ensure_ascii=False, indent=2))
