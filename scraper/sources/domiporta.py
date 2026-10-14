"""Scraper domiporta.pl — mieszkania na sprzedaż i na wynajem (domyślnie we Wrocławiu;
miejscowość z config["city"] = {path, name}, np. path "dolnoslaskie/wroclawski/radwanice",
patrz scraper/cities.py).

Strona wyników jest renderowana po stronie serwera. Każda strona zawiera
JSON-LD (ItemList z RealEstateListing: cena, metraż, pokoje, ulica, data,
sprzedawca, zdjęcie) oraz kafelki HTML (`article.sneakpeak`), z których bierzemy
lokalizację (dzielnica/osiedle). Rynek ustalamy, robiąc dwa przebiegi:
Rynek=Wtorny i Rynek=Pierwotny (każdy do `max_pages` stron).

Wynajem (/mieszkanie/wynajme/...): jeden przebieg bez podziału na rynki; cena to
czynsz najmu zł/mies., a czynsz administracyjny bierzemy z kafelka ("+ czynsz: X zł").
Kaucji ani daty dostępności lista nie podaje. Pokoi (/pokoj/wynajme/...) domiporta
dla Wrocławia nie ma ("Brak wyników"), więc "pokoj" nie jest obsługiwany.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional, Tuple

from bs4 import BeautifulSoup

from scraper.http import make_session, polite_sleep
from scraper.models import Listing

log = logging.getLogger(__name__)

SOURCE = "domiporta"
BASE = "https://www.domiporta.pl"
DEFAULT_PATH = "dolnoslaskie/wroclaw"
SEARCH_URL = BASE + "/mieszkanie/sprzedam/" + DEFAULT_PATH   # Wrocław
RENT_URL = BASE + "/mieszkanie/wynajme/" + DEFAULT_PATH
DEALS = ("sprzedaz", "wynajem")
MARKETS = (("Wtorny", "wtorny"), ("Pierwotny", "pierwotny"))
# Heurystyka: nazwa firmy wskazuje na dewelopera (a nie biuro nieruchomości).
_DEV_RE = re.compile(r"develop|deweloper|budownictw|budowlan|inwestycj|invest|robyg|archicom|"
                     r"murapol|atal\b|vantage|ronson|lokum|\bsp\. ?z o\.? ?o|\bs\.a\.")


def _location(config: dict) -> Tuple[str, str]:
    """(ścieżka lokalizacji, nazwa miasta) z config["city"]; bez niego — Wrocław."""
    city = config.get("city")
    if isinstance(city, dict) and city.get("path"):
        return str(city["path"]).strip("/"), str(city.get("name") or "")
    return DEFAULT_PATH, "Wrocław"


def _params(config: dict, rynek: Optional[str], page: int) -> Dict[str, str]:
    pre = config.get("prefilter") or {}
    p: Dict[str, str] = {
        "SortingOrderDirection": "InsertionDateDescending",
        "GroupToInvestments": "False",
    }
    if rynek:
        p["Rynek"] = rynek
    if pre.get("price_min"):
        p["Price.From"] = str(int(pre["price_min"]))
    if pre.get("price_max"):
        p["Price.To"] = str(int(pre["price_max"]))
    if pre.get("area_min"):
        p["Surface.From"] = str(int(pre["area_min"]))
    if page > 1:
        p["PageNumber"] = str(page)
    return p


def _num(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ".").replace("\xa0", "").replace(" ", ""))
    except ValueError:
        return None


def _listing_id(url: str) -> Optional[str]:
    m = re.search(r"/nieruchomosci/[^/]+/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/nowe/ogloszenie/[^/?#]*?_(\d+)(?:[/?#]|$)", url)
    if m:
        return "nowe-" + m.group(1)
    return None


def _ld_items(soup: BeautifulSoup) -> List[dict]:
    items: List[dict] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        graph = data.get("@graph", [data]) if isinstance(data, dict) else []
        for g in graph:
            if isinstance(g, dict) and g.get("@type") == "ItemList":
                for el in g.get("itemListElement") or []:
                    if isinstance(el, dict) and isinstance(el.get("item"), dict):
                        items.append(el["item"])
    return items


def _tile_locations(soup: BeautifulSoup, city_name: str = "Wrocław") -> Dict[str, List[str]]:
    """Mapuje ścieżkę URL ogłoszenia -> części lokalizacji po nazwie miasta ('Wrocław')."""
    out: Dict[str, List[str]] = {}
    for art in soup.select("article.sneakpeak"):
        a = art.select_one("a.sneakpeak__title[href]")
        loc = art.select_one(".sneakpeak__title--inblock")
        if not a or not loc:
            continue
        text = loc.get_text(" ", strip=True)
        text = re.sub(r"^mieszkanie\s+", "", text, flags=re.I)
        parts = [p.strip() for p in text.split(",") if p.strip()]
        if parts and parts[0].lower().startswith(city_name.lower()):
            parts = parts[1:]
        path = a["href"].split("?")[0].replace(BASE, "")
        out[path] = parts
    return out


def _tile_fees(soup: BeautifulSoup) -> Dict[str, int]:
    """Mapuje ścieżkę URL ogłoszenia -> czynsz administracyjny ("+ czynsz: 1 500 zł")."""
    out: Dict[str, int] = {}
    for art in soup.select("article.sneakpeak"):
        a = art.select_one("a.sneakpeak__title[href]")
        fee = art.select_one(".sneakpeak__price_fee")
        if not a or not fee:
            continue
        m = re.search(r"czynsz:\s*([\d\s\xa0]+)", fee.get_text(" ", strip=True))
        v = _num(re.sub(r"\D", "", m.group(1))) if m else None
        if v:
            out[a["href"].split("?")[0].replace(BASE, "")] = int(v)
    return out


def _district(parts: List[str], street: Optional[str]) -> Optional[str]:
    cands = [p for p in parts if not street or p.lower() != street.lower()]
    return cands[0] if cands else None


def _parse_item(item: dict, locs: Dict[str, List[str]], market: Optional[str],
                deal: str = "sprzedaz", fees: Optional[Dict[str, int]] = None) -> Optional[Listing]:
    url = item.get("url") or ""
    lid = _listing_id(url)
    if not lid:
        log.debug("domiporta: pomijam element bez ID: %s", url)
        return None
    offers = item.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = _num(offers.get("price"))
    if not price:
        return None  # zbiorcze wpisy inwestycji bez ceny jednostkowej
    offered = offers.get("itemOffered") or {}
    area = _num((offered.get("floorSize") or {}).get("value"))
    rooms = offered.get("numberOfRooms")
    try:
        rooms = int(rooms) if rooms not in (None, "") else None
    except (TypeError, ValueError):
        rooms = None
    addr = offered.get("address") or {}
    street = (addr.get("streetAddress") or "").strip() or None

    path = url.split("?")[0].replace(BASE, "")
    parts = locs.get(path, [])
    district = _district(parts, street)

    seller_obj = offers.get("seller") or {}
    if "/nowe/" in url:
        seller = "deweloper"
    elif seller_obj.get("@type") == "Person":
        seller = "prywatny"
    elif seller_obj.get("@type") == "Organization":
        name = (seller_obj.get("name") or "").lower()
        if deal == "sprzedaz" and "nieruchomo" not in name and _DEV_RE.search(name):
            seller = "deweloper"
        else:
            seller = "agencja"
    else:
        seller = None

    title = item.get("name") or ""
    title = title.split(":")[0].strip() or title
    img = item.get("image")
    images = [img] if isinstance(img, str) and img else (list(img) if isinstance(img, list) else [])
    desc = (item.get("description") or "").strip() or None
    if desc:
        desc = re.sub(r"\s+", " ", desc)[:500]

    return Listing(
        source=SOURCE,
        id=lid,
        url=url,
        title=title,
        deal=deal,
        price=int(round(price)),
        area=area,
        rooms=rooms,
        district=district,
        street=street,
        market=market,
        seller=seller,
        images=images,
        description=desc,
        created_at=item.get("datePosted") or None,
        rent_extra=(fees or {}).get(path) if deal != "sprzedaz" else None,
    )


def _no_results(soup: BeautifulSoup) -> bool:
    """Komunikat "Brak wyników spełniających Twoje kryteria wyszukiwania"."""
    return any("brak wyników" in el.get_text(" ", strip=True).lower() for el in soup.select(".alert__title"))


def _last_page(soup: BeautifulSoup) -> Optional[int]:
    nums = [int(m) for a in soup.select(".pagination a[href]")
            for m in re.findall(r"PageNumber=(\d+)", a["href"])]
    return max(nums) if nums else None


def _scrape_market(session, config: dict, rynek: Optional[str], market: Optional[str],
                   max_pages: int, seen: set, deal: str = "sprzedaz") -> Tuple[List[Listing], bool]:
    path, city_name = _location(config)
    search_url = BASE + ("/mieszkanie/sprzedam/" if deal == "sprzedaz" else "/mieszkanie/wynajme/") + path
    city_name = city_name or "Wrocław"
    label = rynek or deal
    results: List[Listing] = []
    first_ok = False
    last_page: Optional[int] = None
    for page in range(1, max_pages + 1):
        if page > 1:
            polite_sleep()
        try:
            resp = session.get(search_url, params=_params(config, rynek, page), timeout=30)
            resp.raise_for_status()
        except Exception as e:  # noqa: BLE001
            if page == 1:
                raise RuntimeError(f"domiporta: nie udało się pobrać 1. strony ({label}): {e}") from e
            log.warning("domiporta: błąd strony %d (%s): %s — przerywam", page, label, e)
            break
        soup = BeautifulSoup(resp.text, "lxml")
        items = _ld_items(soup)
        if page == 1:
            if not items and not soup.select("article.sneakpeak") and _no_results(soup):
                # mała miejscowość: portal po prostu nie ma takich ofert
                log.info("domiporta %s: brak ofert (%s)", label, resp.url)
                return results, True
            if not items and not soup.select("article.sneakpeak"):
                raise RuntimeError(
                    f"domiporta: brak ofert na 1. stronie ({label}) — zmiana struktury strony lub blokada? "
                    f"URL: {resp.url}")
            first_ok = True
            last_page = _last_page(soup)
        if not items:
            break
        locs = _tile_locations(soup, city_name)
        fees = _tile_fees(soup) if deal != "sprzedaz" else {}
        new = 0
        for it in items:
            try:
                lst = _parse_item(it, locs, market, deal, fees)
            except Exception as e:  # noqa: BLE001
                log.warning("domiporta: błąd parsowania elementu: %s", e)
                continue
            if lst is None or lst.key in seen:
                continue
            seen.add(lst.key)
            results.append(lst)
            new += 1
        log.info("domiporta %s: strona %d — %d nowych", label, page, new)
        if new == 0 or (last_page is not None and page >= last_page):
            break
    return results, first_ok


def scrape(config: dict, deal: str = "sprzedaz") -> List[Listing]:
    if deal not in DEALS:
        raise ValueError(f"domiporta: nieobsługiwany typ ogłoszenia {deal!r}")
    max_pages = int(config.get("max_pages") or 30)
    session = make_session()
    seen: set = set()
    if deal != "sprzedaz":
        res, _ = _scrape_market(session, config, None, None, max_pages, seen, deal)
        return res
    out: List[Listing] = []
    for i, (rynek, market) in enumerate(MARKETS):
        if i > 0:
            polite_sleep()
        res, _ = _scrape_market(session, config, rynek, market, max_pages, seen)
        out.extend(res)
    return out


if __name__ == "__main__":
    import sys

    import yaml

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    with open("config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    from scraper.cities import location_for

    # użycie: python -m scraper.sources.domiporta [sprzedaz|wynajem] [miejscowość] [max_pages]
    deal_arg = sys.argv[1] if len(sys.argv) > 1 else "sprzedaz"
    cfg["city"] = location_for(sys.argv[2] if len(sys.argv) > 2 else "wroclaw", SOURCE)
    cfg["max_pages"] = int(sys.argv[3]) if len(sys.argv) > 3 else 2
    deal_cfg = (cfg.get("deals") or {}).get(deal_arg) or {}
    if "prefilter" in deal_cfg:
        cfg["prefilter"] = deal_cfg.get("prefilter") or {}
    res = scrape(cfg, deal_arg)
    print(f"{len(res)} ofert")
    for r in res[:3]:
        print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
