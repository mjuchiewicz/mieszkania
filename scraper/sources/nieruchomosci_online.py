"""Scraper nieruchomosci-online.pl — mieszkania na sprzedaż, mieszkania na wynajem
i pokoje na wynajem (domyślnie we Wrocławiu; miejscowość z config["city"] =
{base, city_param, name}, patrz scraper/cities.py — np. city_param "Siechnice:28774",
czyli nazwa i id miasta z podpowiedzi wyszukiwarki portalu).

Wyniki są renderowane po stronie serwera. Dane bierzemy z kafelków HTML
(`div.tile[data-id]`: cena, metraż, piętro, pokoje, rok budowy, dzielnica,
rynek w `data-market-type`), a ulicę uzupełniamy z JSON-LD (CollectionPage ->
AggregateOffer -> offers[].itemOffered.address.streetAddress).

Format filtrów w URL: szukaj.html?3,<rodzaj>,<transakcja>,,Wrocław,,,,<cenaOd>-<cenaDo>,<metrażOd>
gdzie rodzaj/transakcja to mieszkanie,sprzedaz | mieszkanie,wynajem | pokoj,wynajem.
Paginacja: &p=N.

Wynajem: cena w kafelku to czynsz najmu zł/mies. Kafelki pokoi mają atrybut
"Kaucja"; czynszu administracyjnego ani daty dostępności lista nie podaje.

Cechy mieszkania: piętro "3/8" -> floor_num/floors_total; atrybut
"Umeblowane"/"Umeblowany" (wynajem/pokoje) -> furnished. Typ budynku, stan
i część udogodnień portal koduje w adresie SEO oferty, generowanym z danych
ogłoszenia, np. ``/mieszkanie-w-apartamentowcu,wysoki-standard/123.html``,
``/mieszkanie-w-bloku-mieszkalnym,deweloperski/...``, ``/pokoj-umeblowany/...``
— bierzemy tylko znane tokeny (reszta to m.in. nazwy inwestycji).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

from bs4 import BeautifulSoup

from scraper.http import make_session, polite_sleep
from scraper.models import Listing
from scraper.sources.gratka import floor_numbers

log = logging.getLogger(__name__)

SOURCE = "nieruchomosci_online"
BASE = "https://wroclaw.nieruchomosci-online.pl/szukaj.html"   # Wrocław (domyślnie)
CITY_PARAM = "Wrocław"
DEALS = ("sprzedaz", "wynajem", "pokoj")
# typ ogłoszenia -> (rodzaj nieruchomości, transakcja) w URL wyszukiwarki
_DEAL_PATH = {
    "sprzedaz": ("mieszkanie", "sprzedaz"),
    "wynajem": ("mieszkanie", "wynajem"),
    "pokoj": ("pokoj", "wynajem"),
}


def _location(config: dict) -> Tuple[str, str, str]:
    """(adres wyszukiwarki, parametr miasta, nazwa miasta) z config["city"]."""
    city = config.get("city")
    if isinstance(city, dict) and city.get("city_param"):
        return str(city.get("base") or BASE), str(city["city_param"]), str(city.get("name") or "")
    return BASE, CITY_PARAM, "Wrocław"


def _search_url(config: dict, page: int, deal: str = "sprzedaz") -> str:
    base, city_param, _name = _location(config)
    pre = config.get("prefilter") or {}
    pmin = int(pre["price_min"]) if pre.get("price_min") else None
    pmax = int(pre["price_max"]) if pre.get("price_max") else None
    amin = int(pre["area_min"]) if pre.get("area_min") else None
    kind, trans = _DEAL_PATH[deal]
    parts = ["3", kind, trans, "", quote(city_param, safe=":")]
    if pmin or pmax or amin:
        price = ""
        if pmin or pmax:
            price = f"{pmin or ''}-{pmax or ''}"
        parts += ["", "", "", price]
        if amin:
            parts.append(str(amin))
    url = base + "?" + ",".join(parts)
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


# 1. segment adresu SEO: "mieszkanie-w-<typ budynku>"
_SLUG_BUILDING = (
    ("apartamentow", "apartamentowiec"),
    ("kamieni", "kamienica"),
    ("loft", "loft"),
    ("szeregow", "szeregowiec"),
    ("wielorodzin", "blok"),
    ("blok", "blok"),
    ("wiezow", "blok"),
    ("domu", "dom"),
)
# pozostałe segmenty adresu SEO (dokładne dopasowanie tokenu)
_SLUG_CONDITION = {
    "do-remontu": "do_remontu",
    "do-kapitalnego-remontu": "do_remontu",
    "deweloperski": "stan_deweloperski",
    "stan-deweloperski": "stan_deweloperski",
    "do-wykonczenia": "do_wykonczenia",
    "stan-surowy": "do_wykonczenia",
    "surowy": "do_wykonczenia",
    "wysoki-standard": "do_zamieszkania",
    "nowe-wykonczone": "do_zamieszkania",
    "po-remoncie": "do_zamieszkania",
    "po-generalnym-remoncie": "do_zamieszkania",
    "do-zamieszkania": "do_zamieszkania",
    "dobry-standard": "do_zamieszkania",
    "bardzo-dobry-standard": "do_zamieszkania",
    "do-odswiezenia": "do_zamieszkania",
}
_SLUG_FEATURES = {
    "z-balkonem": "balkon",
    "z-tarasem": "taras",
    "z-loggia": "loggia",
    "z-ogrodkiem": "ogrodek",
    "z-ogrodem": "ogrodek",
    "z-winda": "winda",
    "z-garazem": "garaz",
    "z-miejscem-parkingowym": "parking",
    "z-piwnica": "piwnica",
    "z-komorka-lokatorska": "komorka",
    "z-klimatyzacja": "klimatyzacja",
}


def _slug_info(url: str) -> Dict[str, object]:
    """Typ budynku / stan / cechy / umeblowanie z adresu SEO oferty."""
    out: Dict[str, object] = {"features": []}
    m = re.search(r"\.pl/([^/?#]+)/\d+\.html", url)
    if not m:
        return out
    tokens = m.group(1).lower().split(",")
    first = tokens[0]
    if first.startswith("mieszkanie-w-"):
        for needle, bt in _SLUG_BUILDING:
            if needle in first:
                out["building_type"] = bt
                break
    if "umeblowan" in first:
        out["furnished"] = True
    for t in tokens[1:]:
        if t in _SLUG_CONDITION and "condition" not in out:
            out["condition"] = _SLUG_CONDITION[t]
        elif t in _SLUG_FEATURES and _SLUG_FEATURES[t] not in out["features"]:
            out["features"].append(_SLUG_FEATURES[t])  # type: ignore[union-attr]
        elif t in ("umeblowane", "umeblowany"):
            out["furnished"] = True
    return out


def _yes_no(v: Optional[str]) -> Optional[bool]:
    v = (v or "").strip().lower()
    return True if v == "tak" else False if v == "nie" else None


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


def _parse_tile(tile, streets: Dict[str, str], deal: str = "sprzedaz",
                city_name: str = "Wrocław") -> Optional[Listing]:
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
    if floor and not re.search(r"\w", floor):
        floor = None  # "-" = brak danych
    floor_num, floors_total = floor_numbers(floor)
    if floor_num is not None and floors_total is not None and floors_total < floor_num:
        floors_total = None
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
        txt = re.sub(r",?\s*%s$" % re.escape(city_name), "", txt).strip()
        # "Wrocław, dolnośląskie" = brak dzielnicy
        if txt and not txt.lower().startswith(city_name.lower()):
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
    deposit = None
    if deal != "sprzedaz":
        market = None  # podział na rynki nie dotyczy wynajmu
        dv = _num(attrs.get("kaucja"))
        deposit = int(dv) if dv else None
    seller = "deweloper" if data_id.startswith("i") else None

    images: List[str] = []
    for img in tile.select(".thumb-slider img"):
        src = img.get("src") or img.get("data-src")
        if src and src.startswith("http") and src not in images:
            images.append(src)

    slug = _slug_info(url)
    furnished = _yes_no(attrs.get("umeblowane") or attrs.get("umeblowany"))
    if furnished is None:
        furnished = slug.get("furnished")  # type: ignore[assignment]

    desc_el = tile.select_one("p.desc")
    desc = desc_el.get_text(" ", strip=True)[:500] if desc_el else None

    return Listing(
        source=SOURCE,
        id=lid,
        url=url,
        title=title,
        deal=deal,
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
        floor_num=floor_num,
        floors_total=floors_total,
        furnished=furnished,
        condition=slug.get("condition"),  # type: ignore[arg-type]
        building_type=slug.get("building_type"),  # type: ignore[arg-type]
        features=list(slug["features"]),  # type: ignore[call-overload]
        deposit=deposit,
    )


def _no_results(soup: BeautifulSoup) -> bool:
    """Strona "Nie znaleźliśmy nieruchomości, które spełniają wszystkie kryteria"."""
    return any("nie znaleźliśmy" in sp.get_text(" ", strip=True).lower() for sp in soup.select("span.plug"))


def _last_page(soup: BeautifulSoup) -> Optional[int]:
    nums = [int(n) for a in soup.find_all("a", href=True)
            if "szukaj.html" in a["href"]
            for n in re.findall(r"[&?]p=(\d+)", a["href"])]
    return max(nums) if nums else None


def scrape(config: dict, deal: str = "sprzedaz") -> List[Listing]:
    if deal not in _DEAL_PATH:
        raise ValueError(f"nieruchomosci_online: nieobsługiwany typ ogłoszenia {deal!r}")
    max_pages = int(config.get("max_pages") or 30)
    city_name = _location(config)[2] or "Wrocław"
    session = make_session()
    seen: set = set()
    results: List[Listing] = []
    last_page: Optional[int] = None
    for page in range(1, max_pages + 1):
        if page > 1:
            polite_sleep()
        url = _search_url(config, page, deal)
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
        except Exception as e:  # noqa: BLE001
            if page == 1:
                raise RuntimeError(
                    f"nieruchomosci_online: nie udało się pobrać 1. strony ({deal}): {e}") from e
            log.warning("nieruchomosci_online: błąd strony %d (%s): %s — przerywam", page, deal, e)
            break
        soup = BeautifulSoup(resp.text, "lxml")
        tiles = soup.select("div.tile[data-id]")
        if page == 1:
            if not tiles and _no_results(soup):
                # mała miejscowość: portal po prostu nie ma takich ofert
                log.info("nieruchomosci_online %s: brak ofert (%s)", deal, resp.url)
                return results
            if not tiles:
                raise RuntimeError(
                    f"nieruchomosci_online: brak kafelków ofert na 1. stronie ({deal}) — zmiana struktury "
                    f"strony lub blokada? URL: {resp.url}")
        if not tiles:
            break
        # Paginacja pokazuje tylko okno kilku stron dalej, więc "ostatnia strona"
        # rośnie w miarę przechodzenia — odświeżamy ją na każdej stronie.
        last_page = _last_page(soup)
        streets = _ld_streets(soup)
        new = 0
        for t in tiles:
            try:
                lst = _parse_tile(t, streets, deal, city_name)
            except Exception as e:  # noqa: BLE001
                log.warning("nieruchomosci_online: błąd parsowania kafelka %s: %s", t.get("data-id"), e)
                continue
            if lst is None or lst.key in seen:
                continue
            seen.add(lst.key)
            results.append(lst)
            new += 1
        log.info("nieruchomosci_online %s: strona %d — %d nowych", deal, page, new)
        if new == 0 or (last_page is not None and page >= last_page):
            break
    return results


if __name__ == "__main__":
    import sys

    import yaml

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    with open("config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    from scraper.cities import location_for

    # użycie: python -m scraper.sources.nieruchomosci_online [sprzedaz|wynajem|pokoj] [miejscowość] [max_pages]
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
