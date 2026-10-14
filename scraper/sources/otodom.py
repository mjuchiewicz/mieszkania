"""Scraper Otodom — mieszkania na sprzedaż, na wynajem i pokoje we Wrocławiu.

Strony wyników to Next.js; dane ofert siedzą w <script id="__NEXT_DATA__">
pod props.pageProps.data.searchAds.items. Lista nie zawiera pola "rynek",
więc przy sprzedaży pobieramy osobno rynek wtórny i pierwotny (filtr market=...)
i rynek przypisujemy z zapytania. Strony obu rynków są przeplatane, a łączny
limit żądań (na jeden typ ogłoszenia) to min(max_pages, otodom_max_pages).

Wynajem / pokoje: jeden strumień bez podziału na rynki. W __NEXT_DATA__
totalPrice = czynsz najmu zł/mies. (-> price), rentPrice = czynsz
administracyjny/opłaty dodatkowo (-> rent_extra). Kaucji ani daty dostępności
lista wyników nie zawiera (są tylko na stronie oferty, której nie pobieramy
ze względu na WAF), więc deposit/available_from zostają None. Pokoje mają
estate=ROOM (metraż zwykle jest, liczby pokoi/piętra brak).

Cechy: floorNumber -> floor / floor_num; tagi listy (BALCONY, PARKING_SPOT,
BASEMENT, ...) -> features; TOP_FLOOR -> floors_total = floor_num. Umeblowania,
stanu i typu budynku lista nie zawiera (uzupełnia je scraper/features.py z opisu).
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from scraper.http import make_session, polite_sleep
from scraper.models import Listing

log = logging.getLogger(__name__)

SOURCE = "otodom"
BASE = "https://www.otodom.pl"
DEALS = ("sprzedaz", "wynajem", "pokoj")
_LOC = "/dolnoslaskie/wroclaw/wroclaw/wroclaw"
SEARCH_URLS = {
    "sprzedaz": BASE + "/pl/wyniki/sprzedaz/mieszkanie" + _LOC,
    "wynajem": BASE + "/pl/wyniki/wynajem/mieszkanie" + _LOC,
    "pokoj": BASE + "/pl/wyniki/wynajem/pokoj" + _LOC,
}
SEARCH_URL = SEARCH_URLS["sprzedaz"]  # zgodność wsteczna
# (estate, transaction) oczekiwane w wynikach danego typu
_DEAL_KIND = {
    "sprzedaz": ("FLAT", "SELL"),
    "wynajem": ("FLAT", "RENT"),
    "pokoj": ("ROOM", "RENT"),
}
LIMIT = 72

# Tempo / blokady: Otodom stoi za CloudFront + AWS WAF (reguła rate-based
# per IP). Po przekroczeniu limitu odpowiada HTTP 405 ze stroną
# "Human Verification" (AWS WAF CAPTCHA, nie do przejścia bez JS). Blokada
# trwa od ~1 min do >8 min i po "rozgrzaniu" IP wraca już po ~10-20 żądaniach.
# Dlatego: wolne tempo, backoff na tej samej stronie aż do budżetu czasu,
# a kolejność stron = najpierw najnowsze (HEAD_PAGES), potem reszta od losowego
# miejsca — przy niepełnych przebiegach kolejne uruchomienia pokrywają różne
# fragmenty listy, więc stare oferty też są co jakiś czas odświeżane.
SLEEP_RANGE = (3.0, 5.0)       # s między stronami (bazowo)
SLOWDOWN_FACTOR = 1.5          # po każdej blokadzie tempo zwalnia ×1.5 ...
MAX_SLOWDOWN = 3.0             # ... maks. ×3
COOLDOWN_EVERY = 40            # co tyle żądań dłuższa pauza
COOLDOWN_S = 20
BLOCK_STATUSES = (403, 405, 429)
BACKOFF_S = (30, 60, 120, 180)  # dalej powtarzane 180 s aż do końca budżetu
HEAD_PAGES = 6                 # tyle pierwszych (najnowszych) stron zawsze najpierw
MAX_FAIL_STREAK = 3            # inne błędy z rzędu, po których porzucamy rynek
MAX_RUNTIME_S = 5 * 60         # budżet czasu na jeden typ ogłoszeń (z backoffem)

MARKETS = (("SECONDARY", "wtorny"), ("PRIMARY", "pierwotny"))
RENT_STREAMS = (("ALL", None),)  # wynajem/pokoje: bez filtra rynku


def _streams(deal: str) -> Tuple[Tuple[str, Optional[str]], ...]:
    """(klucz strumienia / wartość filtra market, etykieta rynku dla Listing)."""
    return MARKETS if deal == "sprzedaz" else RENT_STREAMS

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
# floorNumber -> floor_num (0 = parter, -1 = suterena, 11 = powyżej 10; poddasze = nieznane)
_FLOOR_NUMS = {
    "CELLAR": -1, "GROUND": 0, "FIRST": 1, "SECOND": 2, "THIRD": 3, "FOURTH": 4,
    "FIFTH": 5, "SIXTH": 6, "SEVENTH": 7, "EIGHTH": 8, "NINTH": 9, "TENTH": 10,
    "ABOVE_TENTH": 11, "HIGHER_THAN_TENTH": 11, "GARRET": None,
}
# tagi z listy wyników (item["tags"][].value) -> Listing.features.
# Zaobserwowane: BALCONY, PARKING_SPOT, SECURE_BUILDING, SEPARATE_KITCHEN,
# AIR_CONDITIONING, BASEMENT, TOP_FLOOR, STORAGE_ROOM, GROUND_FLOOR, TERRACE,
# GARDEN, TWO_LEVEL (pokoje tagów nie mają). LIFT/GARAGE/LOGGIA — na zapas.
_TAG_FEATURES = {
    "BALCONY": "balkon", "TERRACE": "taras", "LOGGIA": "loggia", "GARDEN": "ogrodek",
    "LIFT": "winda", "ELEVATOR": "winda", "PARKING_SPOT": "parking", "GARAGE": "garaz",
    "BASEMENT": "piwnica", "STORAGE_ROOM": "komorka", "AIR_CONDITIONING": "klimatyzacja",
}


class OtodomError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Pobieranie
# --------------------------------------------------------------------------- #

def _build_params(config: dict, market: str, page: int) -> Dict[str, Any]:
    pre = config.get("prefilter") or {}
    params: Dict[str, Any] = {"limit": LIMIT}
    if market and market != "ALL":
        params["market"] = market
    params.update({
        "by": "LATEST",
        "direction": "DESC",
        "page": page,
    })
    if pre.get("price_min"):
        params["priceMin"] = int(pre["price_min"])
    if pre.get("price_max"):
        params["priceMax"] = int(pre["price_max"])
    if pre.get("area_min"):
        params["areaMin"] = pre["area_min"]
    return params


class BlockedError(OtodomError):
    """Odpowiedź wyglądająca na blokadę / rate limit (403/405/429, captcha)."""


def _fetch_search(session, params: Dict[str, Any],
                  url: str = SEARCH_URL) -> Tuple[List[dict], dict]:
    """Zwraca (items, pagination). Rzuca BlockedError przy blokadzie, inne wyjątki przy błędzie."""
    resp = session.get(url, params=params, timeout=30)
    if resp.status_code in BLOCK_STATUSES:
        raise BlockedError(
            "HTTP %s (server=%s, cf-id=%s): %s"
            % (resp.status_code, resp.headers.get("Server"),
               resp.headers.get("X-Amz-Cf-Id"), resp.text[:200].replace("\n", " "))
        )
    resp.raise_for_status()
    m = _NEXT_DATA_RE.search(resp.text)
    if not m:
        raise BlockedError(
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


def _fetch_with_backoff(session, params: Dict[str, Any], stats: Dict[str, Any],
                        deadline: float, url: str = SEARCH_URL) -> Tuple[List[dict], dict]:
    """Pobiera stronę; przy blokadzie czeka (30, 60, 120, 180, 180... s) i ponawia
    TĘ SAMĄ stronę, dopóki starcza budżetu czasu."""
    attempt = 0
    while True:
        stats["requests"] += 1
        try:
            return _fetch_search(session, params, url)
        except BlockedError as e:
            stats["blocked"] += 1
            stats["slowdown"] = min(MAX_SLOWDOWN, stats["slowdown"] * SLOWDOWN_FACTOR)
            wait = BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
            if time.time() + wait > deadline:
                raise BlockedError("%s (koniec budżetu czasu)" % e)
            attempt += 1
            log.warning("otodom %s strona %s: %s — czekam %ds (próba %d)",
                        params.get("market", "ALL"), params.get("page"), str(e)[:120], wait, attempt)
            time.sleep(wait)


def _page_order(total_pages: int, rng: random.Random) -> List[int]:
    """Strony 2..HEAD_PAGES po kolei, potem reszta od losowego miejsca (z zawinięciem)."""
    head = list(range(2, min(HEAD_PAGES, total_pages) + 1))
    tail = list(range(HEAD_PAGES + 1, total_pages + 1))
    if tail:
        k = rng.randrange(len(tail))
        tail = tail[k:] + tail[:k]
    return head + tail


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


def _floor_num(val: Any) -> Optional[int]:
    if not val:
        return None
    s = str(val).upper()
    if s in _FLOOR_NUMS:
        return _FLOOR_NUMS[s]
    m = re.search(r"-?\d+", s)
    return int(m.group(0)) if m else None


def _tags(item: dict) -> Set[str]:
    out = set()
    for t in item.get("tags") or []:
        v = t.get("value") if isinstance(t, dict) else t
        if isinstance(v, str):
            out.add(v.upper())
    return out


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


def _parse_item(item: dict, market: Optional[str],
                deal: str = "sprzedaz") -> Optional[Listing]:
    estate, transaction = _DEAL_KIND[deal]
    if item.get("estate") != estate:
        return None
    if item.get("transaction") not in (None, transaction):
        return None
    # sprzedaż: totalPrice = cena całkowita; wynajem/pokój: totalPrice = najem zł/mies.
    price = None if item.get("hidePrice") else _money(item.get("totalPrice"))
    # wynajem/pokój: rentPrice = czynsz administracyjny / opłaty dodatkowo
    rent_extra = _money(item.get("rentPrice")) if deal != "sprzedaz" else None
    area = item.get("areaInSquareMeters")
    location = item.get("location") or {}
    floor = _floor(item.get("floorNumber"))
    floor_num = _floor_num(item.get("floorNumber"))
    tags = _tags(item)
    if floor is None and "GROUND_FLOOR" in tags:
        floor, floor_num = "parter", 0
    # TOP_FLOOR = ostatnie piętro -> liczba pięter budynku = piętro oferty
    floors_total = None
    if "TOP_FLOOR" in tags and floor_num is not None and 0 <= floor_num <= 10:
        floors_total = floor_num
    features = sorted({_TAG_FEATURES[t] for t in tags if t in _TAG_FEATURES})
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
        deal=deal,
        price=price,
        area=float(area) if area is not None else None,
        rooms=_rooms(item.get("roomsNumber")),
        floor=floor,
        district=_district(location),
        street=_street(location),
        market=market,
        seller=_seller(item),
        images=_images(item),
        description=desc,
        created_at=item.get("createdAtFirst") or item.get("dateCreated"),
        rent_extra=rent_extra,
        floor_num=floor_num,
        floors_total=floors_total,
        features=features,
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


def _iter_units(items: Iterable[dict], expand_investments: bool = True
                ) -> Iterable[Tuple[dict, bool]]:
    """(item, is_related) — rozwija inwestycje do pojedynczych lokali.
    Przy wynajmie inwestycje (promowane, na sprzedaż) pomijamy."""
    for it in items:
        if not isinstance(it, dict):
            continue
        if it.get("estate") == "INVESTMENT":
            if not expand_investments:
                continue
            for rel in it.get("relatedAds") or []:
                if isinstance(rel, dict):
                    yield rel, True
        else:
            yield it, False


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

def scrape(config: dict, deal: str = "sprzedaz") -> List[Listing]:
    if deal not in DEALS:
        raise ValueError("otodom: nieobsługiwany typ ogłoszenia %r (obsługiwane: %s)" % (deal, DEALS))
    url = SEARCH_URLS[deal]
    streams = _streams(deal)
    max_pages = int(config.get("max_pages") or 1)
    # Otodom blokuje częste zapytania (CAPTCHA AWS WAF), więc pobieramy tylko
    # najnowsze strony; starsze oferty trzyma aggregator (partial_sources).
    max_pages = min(max_pages, int(config.get("otodom_max_pages") or max_pages))
    session = make_session()
    rng = random.Random()
    results: List[Listing] = []
    seen: Set[str] = set()
    t0 = time.time()
    deadline = t0 + MAX_RUNTIME_S

    market_label = dict(streams)
    # kolejka stron per rynek; po pobraniu strony 1 dopisujemy resztę wg _page_order
    queue: Dict[str, List[int]] = {m: [1] for m, _ in streams}
    total_pages: Dict[str, Optional[int]] = {m: None for m, _ in streams}
    done_pages: Dict[str, int] = {m: 0 for m, _ in streams}
    fail_streak: Dict[str, int] = {m: 0 for m, _ in streams}
    stats: Dict[str, Any] = {"requests": 0, "blocked": 0, "slowdown": 1.0}
    pages_tried = 0
    first_errors: List[str] = []
    stop_reason: Optional[str] = None

    while not stop_reason and pages_tried < max_pages and any(queue.values()):
        for market, _label in streams:
            if not queue[market] or pages_tried >= max_pages:
                continue
            if time.time() > deadline:
                stop_reason = "koniec budżetu czasu %ds" % MAX_RUNTIME_S
                break
            page = queue[market].pop(0)
            if stats["requests"]:
                if stats["requests"] % COOLDOWN_EVERY == 0:
                    time.sleep(COOLDOWN_S)
                k = stats["slowdown"]
                polite_sleep(SLEEP_RANGE[0] * k, SLEEP_RANGE[1] * k)
            pages_tried += 1
            params = _build_params(config, market, page)
            try:
                items, pagination = _fetch_with_backoff(session, params, stats, deadline, url)
            except BlockedError as e:
                log.warning("otodom %s strona %d: blokada nie minęła: %s",
                            market, page, str(e)[:160])
                if page == 1:
                    first_errors.append("%s: %s" % (market, str(e)[:160]))
                stop_reason = "blokada WAF (HTTP 405 'Human Verification') do końca budżetu czasu"
                break
            except Exception as e:  # noqa: BLE001
                log.warning("otodom %s strona %d: błąd: %s", market, page, e)
                if page == 1:
                    first_errors.append("%s: %s" % (market, e))
                    continue
                fail_streak[market] += 1
                if fail_streak[market] >= MAX_FAIL_STREAK:
                    log.warning("otodom %s: %d błędów z rzędu — przerywam ten rynek",
                                market, fail_streak[market])
                    queue[market] = []
                continue
            fail_streak[market] = 0

            current = pagination.get("currentPage")
            if page == 1 and pagination.get("totalPages") is not None:
                total_pages[market] = int(pagination["totalPages"])
                queue[market] = _page_order(total_pages[market], rng)
            # strona poza zakresem -> Otodom przekierowuje na inną (bez filtrów); pomijamy
            if current is not None and int(current) != page:
                log.info("otodom %s: strona %d poza zakresem (currentPage=%s) — pomijam",
                         market, page, current)
                continue
            done_pages[market] += 1

            added = 0
            for raw, is_related in _iter_units(items, expand_investments=(deal == "sprzedaz")):
                try:
                    listing = _parse_item(raw, market_label[market], deal)
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
            log.info("otodom %s %s strona %d/%s: %d pozycji, %d nowych ofert",
                     deal, market, page, total_pages[market], len(items), added)

    if not any(done_pages.values()):
        raise OtodomError(
            "Otodom (%s): nie udało się pobrać pierwszej strony wyników (%s)"
            % (deal, "; ".join(first_errors or [stop_reason or "brak wyników"]))
        )

    missed = 0
    for market, _ in streams:
        tp = total_pages[market]
        if tp is not None:
            missed += max(0, tp - done_pages[market])
    elapsed = time.time() - t0
    summary = "%s: %d ofert, %d żądań, %d blokad (HTTP 405), %.0fs; pobrano %s" % (
        deal, len(results), stats["requests"], stats["blocked"], elapsed,
        ", ".join("%s %d/%s" % (m, done_pages[m], total_pages[m]) for m, _ in streams))
    if missed:
        log.warning("otodom: przebieg NIEPEŁNY — pominięto %d stron (%s); %s",
                    missed, stop_reason or "limit max_pages / błędy stron", summary)
    else:
        log.info("otodom: komplet — %s", summary)
    return results


if __name__ == "__main__":
    import os
    import sys
    import yaml

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(root, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # użycie: python -m scraper.sources.otodom [sprzedaz|wynajem|pokoj]
    deal = sys.argv[1] if len(sys.argv) > 1 else "sprzedaz"
    if "deals" in cfg:
        cfg["prefilter"] = ((cfg["deals"].get(deal) or {}).get("prefilter")
                            or cfg.get("prefilter") or {})
    cfg["max_pages"] = 2
    out = scrape(cfg, deal)
    print("count:", len(out), deal)
    print(json.dumps([l.to_dict() for l in out[:3]], ensure_ascii=False, indent=2))
