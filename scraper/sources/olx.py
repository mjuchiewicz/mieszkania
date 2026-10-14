"""Scraper OLX — mieszkania na sprzedaż, na wynajem i pokoje (domyślnie we Wrocławiu;
miejscowość z config["city"] = {city_id, region_id, slug}, patrz scraper/cities.py).

Korzysta z publicznego JSON API frontendu OLX:
    https://www.olx.pl/api/v1/offers/
category_id=14  -> Nieruchomości / Mieszkania / Sprzedaż    (deal "sprzedaz")
category_id=15  -> Nieruchomości / Mieszkania / Wynajem     (deal "wynajem")
category_id=11  -> Nieruchomości / Stancje i pokoje         (deal "pokoj")
region_id=3     -> Dolnośląskie
city_id=19701   -> Wrocław
(wartości potwierdzone w window.__PRERENDERED_STATE__ stron
 https://www.olx.pl/nieruchomosci/mieszkania/sprzedaz/wroclaw/,
 .../mieszkania/wynajem/wroclaw/ i .../stancje-pokoje/wroclaw/)

Wynajem: price = czynsz najmu zł/mies., parametr "rent" ("Czynsz (dodatkowo)")
-> rent_extra. Kaucji ani daty dostępności OLX nie ma w parametrach — wyciągamy
je ostrożnie z opisu (gdy się nie da, zostają None). Pokoje nie mają metrażu
ani liczby pokoi.

API ogranicza offset do 0..1000 (max ~1040 ofert na zapytanie), więc zakres
cen dzielimy adaptacyjnie na pasma mieszczące się w tym limicie. Oferty zmirrorowane z otodom (external_url / partner otodom_pl)
są pomijane — otodom scrapujemy osobno.
"""
from __future__ import annotations

import html
import logging
import re
from datetime import date
from typing import Any, Dict, List, Optional, Set, Tuple

from scraper.http import make_session, polite_sleep
from scraper.models import Listing

log = logging.getLogger(__name__)

SOURCE = "olx"
API_URL = "https://www.olx.pl/api/v1/offers/"
DEALS = ("sprzedaz", "wynajem", "pokoj")
CATEGORY_IDS = {"sprzedaz": 14, "wynajem": 15, "pokoj": 11}
_REFERER_PATHS = {
    "sprzedaz": "https://www.olx.pl/nieruchomosci/mieszkania/sprzedaz/%s/",
    "wynajem": "https://www.olx.pl/nieruchomosci/mieszkania/wynajem/%s/",
    "pokoj": "https://www.olx.pl/nieruchomosci/stancje-pokoje/%s/",
}
REFERERS = {d: p % "wroclaw" for d, p in _REFERER_PATHS.items()}  # Wrocław
CATEGORY_ID = CATEGORY_IDS["sprzedaz"]  # zgodność wsteczna
REGION_ID = 3         # dolnośląskie (domyślnie)
CITY_ID = 19701       # Wrocław (domyślnie)
PAGE_SIZE = 40
MAX_OFFSET = 1000     # limit walidacji API
BAND_TARGET = 950     # pasmo cenowe z większą liczbą ofert dzielimy na pół
MIN_BAND_WIDTH = {"sprzedaz": 2000, "wynajem": 20, "pokoj": 20}  # zł — węższych pasm nie dzielimy
DEFAULT_PRICE_MAX = {"sprzedaz": 20_000_000, "wynajem": 100_000, "pokoj": 20_000}
MAX_REQUESTS = 90     # globalny limit zapytań na przebieg (~3 min z polite_sleep)

ROOMS_MAP = {"one": 1, "two": 2, "three": 3, "four": 4}

# params (wartości z definicji filtrów OLX, kat. 14/15/11):
#  floor_select: floor_-1 Suterena, floor_0 Parter, floor_1..floor_10,
#                floor_11 "Powyżej 10", floor_17 Poddasze (-> floor_num None)
#  furniture: yes/no;  builttype: blok/kamienica/wolnostojacy/szeregowiec/
#  apartamentowiec/loft/pozostale;  (tylko wynajem) winda: Tak/Nie,
#  pets: Tak/Nie, parking: [brak | przynależne na ulicy | parking strzeżony |
#  w garażu | identyfikator]. Liczby pięter budynku ani stanu OLX nie podaje.
BUILDING_TYPE_MAP = {
    "blok": "blok", "kamienica": "kamienica", "apartamentowiec": "apartamentowiec",
    "wolnostojacy": "dom", "szeregowiec": "szeregowiec", "loft": "loft",
}
FURNITURE_MAP = {"yes": True, "no": False}


def _location(config: dict) -> Tuple[int, int, str]:
    """(city_id, region_id, slug do adresu) z config["city"]; bez niego — Wrocław."""
    city = config.get("city")
    if isinstance(city, dict) and city.get("city_id"):
        return int(city["city_id"]), int(city["region_id"]), str(city.get("slug") or "")
    return CITY_ID, REGION_ID, "wroclaw"


def _build_params(config: dict, offset: int, price_from: Optional[int] = None,
                  price_to: Optional[int] = None, deal: str = "sprzedaz") -> Dict[str, Any]:
    city_id, region_id, _slug = _location(config)
    params: Dict[str, Any] = {
        "offset": offset,
        "limit": PAGE_SIZE,
        "category_id": CATEGORY_IDS[deal],
        "region_id": region_id,
        "city_id": city_id,
        "sort_by": "created_at:desc",
    }
    pre = config.get("prefilter") or {}
    if price_from is not None:
        params["filter_float_price:from"] = int(price_from)
    if price_to is not None:
        params["filter_float_price:to"] = int(price_to)
    if pre.get("area_min") and deal != "pokoj":  # pokoje nie mają parametru metrażu
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


def _clean_description_full(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    t = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip() or None


def _clean_description(text: Optional[str]) -> Optional[str]:
    t = _clean_description_full(text)
    if not t:
        return None
    if len(t) > 500:
        t = t[:497].rstrip() + "..."
    return t or None


def _int_from(v: Any) -> Optional[int]:
    """'1 500', '600 zł', '2.400' -> int."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(round(v))
    digits = re.sub(r"[^\d]", "", str(v).split(",")[0])
    return int(digits) if digits else None


_DEPOSIT_RE = re.compile(
    r"kaucj\w*([^0-9.\n]{0,35}?)(\d{1,3}(?:[ .\u00a0]\d{3})+(?!\d)|\d+)(\s*[-–]?\s*\w{0,6})",
    re.I)
_MONTHS = {
    "stycz": 1, "lut": 2, "mar": 3, "kwie": 4, "maj": 5, "czerw": 6, "lip": 7,
    "sierp": 8, "wrze": 9, "paźdz": 10, "listop": 11, "grud": 12,
}
_AVAIL_RE = re.compile(
    r"(?:dost\u0119pn\w*|woln\w*)(?:\s+jest)?\s*:?\s*od\s*(?:dnia\s*)?:?\s*"
    r"(?:(\d{1,2})[./-](\d{1,2})(?:[./-](\d{2,4}))?(?!\d)"
    r"|(\d{1,2})?\s*([a-z\u0105\u0107\u0119\u0142\u0144\u00f3\u015b\u017a\u017c]{3,})(?:\s+(\d{4}))?)",
    re.I)


def _deposit_from_text(text: Optional[str]) -> Optional[int]:
    """'Kaucja: 2 900 zł', 'kaucja zwrotna 3000,-zł', 'Kaucja w wysokości 2050 zł' -> kwota.
    Odrzuca '2-miesięczna', 'x2' itp. oraz kwoty spoza 100..50000."""
    if not text:
        return None
    for m in _DEPOSIT_RE.finditer(text):
        between, num, after = m.group(1), m.group(2), m.group(3).lower()
        if re.search(r"(?i)x|razy|krot|czynsz|op\u0142at", between):
            continue
        if re.match(r"\s*[-–]?\s*(mies|msc|m-c|krot|x)", after):
            continue
        val = int(re.sub(r"\D", "", num))
        if 100 <= val <= 50000:
            return val
    return None


def _available_from_text(text: Optional[str], ref: Optional[date] = None) -> Optional[str]:
    """'Dostępne od 01.11.2026', 'wolne od 1 grudnia 2026', 'dostępne od listopada'
    -> ISO date. 'od zaraz' / brak daty -> None."""
    if not text:
        return None
    ref = ref or date.today()
    for m in _AVAIL_RE.finditer(text):
        d_num, m_num, y_num, d_word, month_word, y_word = m.groups()
        try:
            if m_num:
                day, month, year = int(d_num), int(m_num), y_num
            else:
                word = month_word.lower()
                month = next((v for k, v in _MONTHS.items() if word.startswith(k)), None)
                if month is None:
                    continue  # np. "od zaraz"
                day, year = int(d_word or 1), y_word
            if year:
                year = int(year)
                if year < 100:
                    year += 2000
            else:
                year = ref.year if month >= ref.month - 2 else ref.year + 1
            return date(year, month, day).isoformat()
        except (ValueError, TypeError):
            continue
    return None


def _is_yes(v: Any) -> bool:
    if isinstance(v, list):
        return any(_is_yes(x) for x in v)
    return isinstance(v, str) and v.strip().lower() in ("tak", "yes")


def _parse_item(item: dict, deal: str = "sprzedaz") -> Listing:
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

    floor_num = None
    fm = re.fullmatch(r"floor_(-?\d+)", fk)
    if fm and fk != "floor_17":  # floor_17 = poddasze (piętro nieznane)
        floor_num = int(fm.group(1))

    furnished = FURNITURE_MAP.get(pval("furniture").get("key"))
    building_type = BUILDING_TYPE_MAP.get(pval("builttype").get("key"))

    features: List[str] = []
    if _is_yes(pval("winda").get("key")):
        features.append("winda")
    if _is_yes(pval("pets").get("key")):
        features.append("zwierzeta")
    parking = pval("parking").get("key")
    parking = [parking] if isinstance(parking, str) else (parking or [])
    for pk in parking:
        pk = str(pk).lower()
        if "garaż" in pk or "garaz" in pk:
            features.append("garaz")
        elif "ulicy" in pk or "strzeżony" in pk or "strzezony" in pk:
            features.append("parking")
    features = sorted(set(features))

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

    rent_extra = deposit = available_from = None
    if deal != "sprzedaz":
        rent_extra = _int_from(pval("rent").get("key"))
        desc_text = _clean_description_full(item.get("description"))
        deposit = _deposit_from_text(desc_text)
        available_from = _available_from_text(desc_text)

    return Listing(
        source=SOURCE,
        id=str(item["id"]),
        url=item["url"],
        title=(item.get("title") or "").strip(),
        deal=deal,
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
        rent_extra=rent_extra,
        deposit=deposit,
        available_from=available_from,
        floor_num=floor_num,
        furnished=furnished,
        building_type=building_type,
        features=features,
    )


def _price_range(config: dict, deal: str = "sprzedaz") -> Tuple[int, int]:
    pre = config.get("prefilter") or {}
    lo = int(pre.get("price_min") or 0)
    hi = int(pre.get("price_max") or DEFAULT_PRICE_MAX[deal])
    return lo, hi


def scrape(config: dict, deal: str = "sprzedaz") -> List[Listing]:
    """Pobiera oferty danego typu (deal), dzieląc zakres cen na pasma tak, by każde
    mieściło się w limicie offsetu API (adaptacyjnie: pasmo z > BAND_TARGET ofert
    dzielone na pół)."""
    if deal not in DEALS:
        raise ValueError("olx: nieobsługiwany typ ogłoszenia %r (obsługiwane: %s)" % (deal, DEALS))
    city_id, _region_id, slug = _location(config)
    session = make_session()
    session.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Referer": _REFERER_PATHS[deal] % slug,
    })
    min_band = MIN_BAND_WIDTH[deal]
    max_pages = int(config.get("max_pages") or 1)   # limit stron na pasmo

    results: List[Listing] = []
    seen: Set[str] = set()
    stats = {"otodom": 0, "requests": 0}

    def fetch(offset: int, lo: int, hi: int) -> dict:
        if stats["requests"] > 0:
            polite_sleep()
        stats["requests"] += 1
        resp = session.get(API_URL, params=_build_params(config, offset, lo, hi, deal), timeout=30)
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
                if city is not None and city != city_id:
                    continue
                listing = _parse_item(item, deal)
                if listing.id in seen:
                    continue
                seen.add(listing.id)
                results.append(listing)
            except Exception as e:  # jedna zepsuta oferta nie zabija przebiegu
                log.warning("olx: pominięto ofertę %s: %s",
                            item.get("id") if isinstance(item, dict) else "?", e)

    lo0, hi0 = _price_range(config, deal)
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
                    f"olx {deal}: nie udało się pobrać pierwszej strony API ({API_URL}): {e}") from e
            log.warning("olx: błąd pasma %d-%d: %s — pomijam", lo, hi, e)
            continue
        first = False

        meta = data.get("metadata") or {}
        total = int(meta.get("visible_total_count") or meta.get("total_elements") or 0)
        if total > BAND_TARGET and hi - lo > min_band:
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

    log.info("olx %s: %d ofert (pominięto %d mirrorów otodom, %d zapytań)",
             deal, len(results), stats["otodom"], stats["requests"])
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
    # użycie: python -m scraper.sources.olx [sprzedaz|wynajem|pokoj] [miejscowość] [--full]
    from scraper.cities import location_for
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    deal = args[0] if args else "sprzedaz"
    cfg["city"] = location_for(args[1] if len(args) > 1 else "wroclaw", SOURCE)
    if "prefilter" not in cfg or "deals" in cfg:
        cfg["prefilter"] = (((cfg.get("deals") or {}).get(deal) or {}).get("prefilter")
                            or cfg.get("prefilter") or {})
    if "--full" not in sys.argv:  # --full: pełny przebieg z max_pages z configu
        cfg["max_pages"] = 2
    listings = scrape(cfg, deal)
    print(f"Pobrano {len(listings)} ofert ({deal})")
    for l in listings[:3]:
        print(json.dumps(l.to_dict(), ensure_ascii=False, indent=2))
