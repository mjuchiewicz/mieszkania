"""Scraper domiporta.pl — mieszkania na sprzedaż we Wrocławiu.

Strona wyników jest renderowana po stronie serwera. Każda strona zawiera
JSON-LD (ItemList z RealEstateListing: cena, metraż, pokoje, ulica, data,
sprzedawca, zdjęcie) oraz kafelki HTML (`article.sneakpeak`), z których bierzemy
lokalizację (dzielnica/osiedle). Rynek ustalamy, robiąc dwa przebiegi:
Rynek=Wtorny i Rynek=Pierwotny (każdy do `max_pages` stron).
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
SEARCH_URL = BASE + "/mieszkanie/sprzedam/dolnoslaskie/wroclaw"
MARKETS = (("Wtorny", "wtorny"), ("Pierwotny", "pierwotny"))
# Heurystyka: nazwa firmy wskazuje na dewelopera (a nie biuro nieruchomości).
_DEV_RE = re.compile(r"develop|deweloper|budownictw|budowlan|inwestycj|invest|robyg|archicom|"
                     r"murapol|atal\b|vantage|ronson|lokum|\bsp\. ?z o\.? ?o|\bs\.a\.")


def _params(config: dict, rynek: str, page: int) -> Dict[str, str]:
    pre = config.get("prefilter") or {}
    p: Dict[str, str] = {
        "Rynek": rynek,
        "SortingOrderDirection": "InsertionDateDescending",
        "GroupToInvestments": "False",
    }
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


def _tile_locations(soup: BeautifulSoup) -> Dict[str, List[str]]:
    """Mapuje ścieżkę URL ogłoszenia -> części lokalizacji po 'Wrocław'."""
    out: Dict[str, List[str]] = {}
    for art in soup.select("article.sneakpeak"):
        a = art.select_one("a.sneakpeak__title[href]")
        loc = art.select_one(".sneakpeak__title--inblock")
        if not a or not loc:
            continue
        text = loc.get_text(" ", strip=True)
        text = re.sub(r"^mieszkanie\s+", "", text, flags=re.I)
        parts = [p.strip() for p in text.split(",") if p.strip()]
        if parts and parts[0].lower().startswith("wrocław"):
            parts = parts[1:]
        path = a["href"].split("?")[0].replace(BASE, "")
        out[path] = parts
    return out


def _district(parts: List[str], street: Optional[str]) -> Optional[str]:
    cands = [p for p in parts if not street or p.lower() != street.lower()]
    return cands[0] if cands else None


def _parse_item(item: dict, locs: Dict[str, List[str]], market: str) -> Optional[Listing]:
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
        if "nieruchomo" not in name and _DEV_RE.search(name):
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
    )


def _last_page(soup: BeautifulSoup) -> Optional[int]:
    nums = [int(m) for a in soup.select(".pagination a[href]")
            for m in re.findall(r"PageNumber=(\d+)", a["href"])]
    return max(nums) if nums else None


def _scrape_market(session, config: dict, rynek: str, market: str,
                   max_pages: int, seen: set) -> Tuple[List[Listing], bool]:
    results: List[Listing] = []
    first_ok = False
    last_page: Optional[int] = None
    for page in range(1, max_pages + 1):
        if page > 1:
            polite_sleep()
        try:
            resp = session.get(SEARCH_URL, params=_params(config, rynek, page), timeout=30)
            resp.raise_for_status()
        except Exception as e:  # noqa: BLE001
            if page == 1:
                raise RuntimeError(f"domiporta: nie udało się pobrać 1. strony ({rynek}): {e}") from e
            log.warning("domiporta: błąd strony %d (%s): %s — przerywam", page, rynek, e)
            break
        soup = BeautifulSoup(resp.text, "lxml")
        items = _ld_items(soup)
        if page == 1:
            if not items and not soup.select("article.sneakpeak"):
                raise RuntimeError(
                    f"domiporta: brak ofert na 1. stronie ({rynek}) — zmiana struktury strony lub blokada? "
                    f"URL: {resp.url}")
            first_ok = True
            last_page = _last_page(soup)
        if not items:
            break
        locs = _tile_locations(soup)
        new = 0
        for it in items:
            try:
                lst = _parse_item(it, locs, market)
            except Exception as e:  # noqa: BLE001
                log.warning("domiporta: błąd parsowania elementu: %s", e)
                continue
            if lst is None or lst.key in seen:
                continue
            seen.add(lst.key)
            results.append(lst)
            new += 1
        log.info("domiporta %s: strona %d — %d nowych", rynek, page, new)
        if new == 0 or (last_page is not None and page >= last_page):
            break
    return results, first_ok


def scrape(config: dict) -> List[Listing]:
    max_pages = int(config.get("max_pages") or 30)
    session = make_session()
    seen: set = set()
    out: List[Listing] = []
    for i, (rynek, market) in enumerate(MARKETS):
        if i > 0:
            polite_sleep()
        res, _ = _scrape_market(session, config, rynek, market, max_pages, seen)
        out.extend(res)
    return out


if __name__ == "__main__":
    import yaml

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    with open("config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["max_pages"] = 2
    res = scrape(cfg)
    print(f"{len(res)} ofert")
    for r in res[:3]:
        print(json.dumps(r.to_dict(), ensure_ascii=False, indent=2))
