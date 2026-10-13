"""Scraper nieruchomosci-online.pl — mieszkania na sprzedaż we Wrocławiu.

Wyniki są renderowane po stronie serwera. Dane bierzemy z kafelków HTML
(`div.tile[data-id]`: cena, metraż, piętro, pokoje, rok budowy, dzielnica,
rynek w `data-market-type`), a ulicę uzupełniamy z JSON-LD (CollectionPage ->
AggregateOffer -> offers[].itemOffered.address.streetAddress).

Format filtrów w URL: szukaj.html?3,mieszkanie,sprzedaz,,Wrocław,,,,<cenaOd>-<cenaDo>,<metrażOd>
Paginacja: &p=N.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional
from urllib.parse import quote

from bs4 import BeautifulSoup

from scraper.http import make_session, polite_sleep
from scraper.models import Listing

log = logging.getLogger(__name__)

SOURCE = "nieruchomosci_online"
BASE = "https://wroclaw.nieruchomosci-online.pl/szukaj.html"


def _search_url(config: dict, page: int) -> str:
    pre = config.get("prefilter") or {}
    pmin = int(pre["price_min"]) if pre.get("price_min") else None
    pmax = int(pre["price_max"]) if pre.get("price_max") else None
    amin = int(pre["area_min"]) if pre.get("area_min") else None
    parts = ["3", "mieszkanie", "sprzedaz", "", quote("Wrocław")]
    if pmin or pmax or amin:
        price = ""
        if pmin or pmax:
            price = f"{pmin or ''}-{pmax or ''}"
        parts += ["", "", "", price]
        if amin:
            parts.append(str(amin))
    url = BASE + "?" + ",".join(parts)
    if page > 1:
        url += f"&p={page}"
    return url


def _num(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    t = re.sub(r"[^\d,.]", "", text.replace("\xa0", "").replace(" ", "")).replace(",", ".")
    try:
        return float(t) if t else None
    except ValueError:
        return None


def _ld_streets(soup: BeautifulSoup) -> Dict[str, str]:
    """ID ogłoszenia -> ulica z JSON-LD."""
    out: Dict[str, str] = {}
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict) or data.get("@type") != "CollectionPage":
            continue
        offers = (data.get("mainEntity") or {}).get("offers") or []
        for agg in offers:
            for o in (agg or {}).get("offers") or []:
                try:
                    m = re.search(r"/(\d+)\.html", o.get("url") or "")
                    street = (((o.get("itemOffered") or {}).get("address") or {})
                              .get("streetAddress") or "").strip()
                    if m and street:
                        out[m.group(1)] = re.sub(r"\s+", " ", street)
                except AttributeError:
                    continue
    return out


def _attrs(tile) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for p in tile.select(".attributes__box--item p"):
        label = p.find("span")
        if not label:
            continue
        key = label.get_text(strip=True).rstrip(":").lower()
        val = "".join(s.get_text(strip=True) for s in p.find_all("strong"))
        out[key] = val
    return out


def _parse_tile(tile, streets: Dict[str, str]) -> Optional[Listing]:
    data_id = tile.get("data-id") or ""
    m = re.match(r"^(?:a|i\d+_)(\d+)$", data_id)
    if not m:
        return None
    lid = m.group(1)
    link = tile.select_one("h2.name a[href]")
    if not link:
        return None
    url = link["href"].split("?")[0]
    if url.startswith("//"):
        url = "https:" + url
    title = link.get_text(" ", strip=True)

    disp = tile.select_one("[id^=primary-display]")
    price = area = None
    if disp:
        spans = disp.find_all("span", recursive=False)
        for sp in spans:
            txt = sp.get_text(" ", strip=True)
            if "zł/m" in txt:
                continue
            if "m²" in txt or "m2" in txt:
                area = _num(txt.replace("m²", "").replace("m2", ""))
            elif "zł" in txt:
                v = _num(txt)
                price = int(v) if v else None
    if not price:
        return None  # brak ceny jednostkowej (np. "zapytaj o cenę" / zbiorczy wpis)

    attrs = _attrs(tile)
    rooms = None
    if attrs.get("liczba pokoi", "").isdigit():
        rooms = int(attrs["liczba pokoi"])
    floor = attrs.get("piętro") or None
    build_year = None
    by = re.search(r"\d{4}", attrs.get("rok budowy", ""))
    if by:
        build_year = int(by.group(0))

    district = None
    prov = tile.select_one("[id^=tertiary-province]")
    if prov:
        a = prov.find("a")
        txt = a.get_text(" ", strip=True) if a else prov.get_text(" ", strip=True)
        txt = re.sub(r"\s+", " ", txt).strip().rstrip(",").strip()
        txt = re.sub(r",?\s*Wrocław$", "", txt).strip()
        if txt and txt.lower() != "wrocław":
            district = txt

    street = streets.get(lid)
    if not street:
        sm = re.search(r",\s*((?:ul|al|pl|os)\.\s*.+)$", title)
        if sm:
            street = sm.group(1).strip()
    if street:
        street = re.sub(r"^(ul|al|pl|os)\.\s*", "", street, flags=re.I).strip() or None

    mt = tile.get("data-market-type")
    market = {"primary": "pierwotny", "secondary": "wtorny"}.get(mt or "")
    seller = "deweloper" if data_id.startswith("i") else None

    images: List[str] = []
    for img in tile.select(".thumb-slider img"):
        src = img.get("src") or img.get("data-src")
        if src and src.startswith("http") and src not in images:
            images.append(src)

    desc_el = tile.select_one("p.desc")
    desc = desc_el.get_text(" ", strip=True)[:500] if desc_el else None

    return Listing(
        source=SOURCE,
        id=lid,
        url=url,
        title=title,
        price=price,
        area=area,
        rooms=rooms,
        floor=floor,
        district=district,
        street=street,
        market=market,
        seller=seller,
        build_year=build_year,
        images=images,
        description=desc or None,
    )


def _last_page(soup: BeautifulSoup) -> Optional[int]:
    nums = [int(n) for a in soup.find_all("a", href=True)
            if "szukaj.html" in a["href"]
            for n in re.findall(r"[&?]p=(\d+)", a["href"])]
    return max(nums) if nums else None


def scrape(config: dict) -> List[Listing]:
    max_pages = int(config.get("max_pages") or 30)
    session = make_session()
    seen: set = set()
    results: List[Listing] = []
    last_page: Optional[int] = None
    for page in range(1, max_pages + 1):
        if page > 1:
            polite_sleep()
        url = _search_url(config, page)
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
        except Exception as e:  # noqa: BLE001
            if page == 1:
                raise RuntimeError(f"nieruchomosci_online: nie udało się pobrać 1. strony: {e}") from e
            log.warning("nieruchomosci_online: błąd strony %d: %s — przerywam", page, e)
            break
        soup = BeautifulSoup(resp.text, "lxml")
        tiles = soup.select("div.tile[data-id]")
        if page == 1:
            if not tiles:
                raise RuntimeError(
                    "nieruchomosci_online: brak kafelków ofert na 1. stronie — zmiana struktury "
                    f"strony lub blokada? URL: {resp.url}")
            last_page = _last_page(soup)
        if not tiles:
            break
        streets = _ld_streets(soup)
        new = 0
        for t in tiles:
            try:
                lst = _parse_tile(t, streets)
            except Exception as e:  # noqa: BLE001
                log.warning("nieruchomosci_online: błąd parsowania kafelka %s: %s", t.get("data-id"), e)
                continue
            if lst is None or lst.key in seen:
                continue
            seen.add(lst.key)
            results.append(lst)
            new += 1
        log.info("nieruchomosci_online: strona %d — %d nowych", page, new)
        if new == 0 or (last_page is not None and page >= last_page):
            break
    return results


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
