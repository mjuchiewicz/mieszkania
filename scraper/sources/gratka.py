"""Scraper gratka.pl — mieszkania na sprzedaż.

gratka.pl i morizon.pl należą do Grupy Morizon-Gratka i działają na tym samym
backendzie (Nuxt 3, te same ID ofert, te same dane). Strona listingu jest
renderowana po stronie serwera i zawiera kompletny stan w
``<script id="__NUXT_DATA__">`` (format "devalue" — spłaszczona tablica
z referencjami po indeksach). Parsujemy go zamiast HTML-a; dodatkowo
JSON-LD z tej samej strony daje dokładny metraż (z częścią dziesiętną).

Rynek (pierwotny/wtórny) nie jest dostępny w danych kafelka, więc
pobieramy dwa wyszukiwania z filtrem rynku — wtedy pole ``market`` jest pewne.

Wspólny silnik (``scrape_portal``) jest używany także przez morizon.py.
"""
from __future__ import annotations

import json
import logging
import math
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from scraper.http import make_session, polite_sleep
from scraper.models import Listing

log = logging.getLogger(__name__)

SOURCE = "gratka"
PER_PAGE = 35  # tyle ofert zwraca portal na stronę

_NUXT_RE = re.compile(
    r'<script[^>]*\bid="__NUXT_DATA__"[^>]*>(.*?)</script>', re.S
)

# typy kontaktu w danych portalu -> nasz słownik
_SELLER_MAP = {
    "AGENCY": "agencja",
    "OWNER": "prywatny",
    "PRIVATE": "prywatny",
    "SALES_OFFICE": "deweloper",
    "DEVELOPER": "deweloper",
    "INVESTOR": "deweloper",
}


# ---------------------------------------------------------------------------
# Dekoder formatu devalue (Nuxt 3 payload)
# ---------------------------------------------------------------------------

_WRAPPERS = {"Reactive", "ShallowReactive", "Ref", "ShallowRef"}
_SPECIAL = _WRAPPERS | {
    "EmptyRef", "EmptyShallowRef", "NuxtError", "Set", "Map", "Date",
    "RegExp", "BigInt", "null", "undefined", "Object",
}


def _devalue_unflatten(arr: list) -> Any:
    """Odtwarza obiekt ze spłaszczonej tablicy devalue (bez rekurencji Pythona
    w głąb dla zwykłych list/dictów nie da się prosto, ale głębokość payloadu
    jest niewielka)."""
    cache: Dict[int, Any] = {}

    def h(i: Any) -> Any:
        if not isinstance(i, int):
            return i
        if i < 0:  # -1 undefined, -2 null, -3 NaN, -4 inf, -5 -inf, -6 -0
            return None
        if i in cache:
            return cache[i]
        v = arr[i]
        if isinstance(v, list):
            if v and isinstance(v[0], str) and v[0] in _SPECIAL:
                tag = v[0]
                if tag in _WRAPPERS:
                    r = h(v[1]) if len(v) > 1 else None
                elif tag == "Date":
                    r = v[1] if len(v) > 1 else None
                elif tag == "Set":
                    r = [h(x) for x in v[1:]]
                elif tag == "Map":
                    r = {str(h(v[k])): h(v[k + 1]) for k in range(1, len(v) - 1, 2)}
                else:
                    r = None
                cache[i] = r
                return r
            out: list = []
            cache[i] = out
            out.extend(h(x) for x in v)
            return out
        if isinstance(v, dict):
            d: dict = {}
            cache[i] = d
            for k, x in v.items():
                d[k] = h(x)
            return d
        cache[i] = v
        return v

    return h(0)


def extract_nuxt_data(html: str) -> dict:
    m = _NUXT_RE.search(html)
    if not m:
        raise RuntimeError("brak __NUXT_DATA__ w odpowiedzi (zmiana strony lub blokada anty-bot)")
    root = _devalue_unflatten(json.loads(m.group(1)))
    if not isinstance(root, dict) or not isinstance(root.get("data"), dict):
        raise RuntimeError("nieoczekiwana struktura __NUXT_DATA__")
    return root["data"]


def _find_key(data: dict, prefix: str) -> Optional[dict]:
    for k, v in data.items():
        if k.startswith(prefix) and isinstance(v, dict):
            return v.get("data") if isinstance(v.get("data"), dict) else None
    return None


# ---------------------------------------------------------------------------
# Parsowanie pojedynczej oferty
# ---------------------------------------------------------------------------

def _to_int(s: Any) -> Optional[int]:
    if s is None:
        return None
    try:
        return int(round(float(str(s).replace(",", ".").replace(" ", ""))))
    except ValueError:
        return None


def _to_float(s: Any) -> Optional[float]:
    if s is None:
        return None
    try:
        return float(str(s).replace(",", ".").replace(" ", ""))
    except ValueError:
        return None


def _parse_floor(s: Optional[str]) -> Optional[str]:
    # "piętro 2/4" -> "2/4", "parter/1" -> "parter/1", "piętro 5" -> "5"
    if not s:
        return None
    s = s.strip()
    s = re.sub(r"^pi[eę]tro\s*", "", s, flags=re.I)
    return s or None


def _parse_rooms(s: Any) -> Optional[int]:
    if s is None:
        return None
    m = re.search(r"\d+", str(s))
    return int(m.group()) if m else None


def _district(loc: List[str]) -> Optional[str]:
    """['dolnośląskie','Wrocław','Krzyki','Jagodno'] -> 'Jagodno' (najdokładniejsza
    jednostka po nazwie miasta), 'Os. Stare Miasto' -> 'Stare Miasto'."""
    if not loc:
        return None
    try:
        idx = [x.lower() for x in loc].index("wrocław")
    except ValueError:
        idx = 1 if len(loc) > 1 else 0
    rest = [x for x in loc[idx + 1:] if x]
    if not rest:
        return None
    return re.sub(r"^os\.\s*", "", rest[-1], flags=re.I).strip() or None


def _ldjson_offers(page_data: dict) -> Dict[str, dict]:
    """url oferty -> itemOffered z JSON-LD (dokładny metraż, piętro)."""
    out: Dict[str, dict] = {}
    try:
        for s in (page_data.get("headTags") or {}).get("script") or []:
            if not isinstance(s, dict) or s.get("type") != "application/ld+json":
                continue
            try:
                j = json.loads(s.get("innerHTML") or "")
            except ValueError:
                continue
            offers = ((j.get("offers") or {}).get("offers")) if isinstance(j, dict) else None
            for o in offers or []:
                if isinstance(o, dict) and o.get("url"):
                    out[o["url"].split("?")[0]] = o.get("itemOffered") or {}
    except Exception:  # noqa: BLE001 — JSON-LD to tylko dodatek
        log.debug("nie udało się sparsować JSON-LD", exc_info=True)
    return out


def parse_node(
    node: dict,
    *,
    source: str,
    base_url: str,
    thumb_host: str,
    market: Optional[str],
    ld: Dict[str, dict],
) -> Optional[Listing]:
    """Zamienia węzeł z searchResult.properties.nodes na Listing.
    Zwraca None dla ofert do pominięcia (brak ceny, zarezerwowane)."""
    price_obj = node.get("price") or {}
    price = _to_int(price_obj.get("amount"))
    if not price or (price_obj.get("currency") or "PLN") != "PLN":
        return None  # zbiorcze wpisy inwestycji / "zapytaj o cenę" / waluta obca
    if "zarezerw" in (node.get("priceFormatted") or "").lower():
        return None  # zarezerwowane — nie są realnie na sprzedaż

    rel = node.get("url") or ""
    url = rel if rel.startswith("http") else base_url.rstrip("/") + rel
    lid = str(node.get("idOnFrontend") or node.get("id") or "")
    if not lid or not rel:
        return None

    loc = node.get("location") or {}
    item_ld = ld.get(url.split("?")[0]) or {}
    area = _to_float(((item_ld.get("floorSize") or {}).get("value"))) or _to_float(node.get("area"))

    contact = node.get("contact") or {}
    ctype = ((contact.get("company") or {}).get("type")
             or (contact.get("person") or {}).get("type") or "")
    seller = _SELLER_MAP.get(ctype.upper())
    if node.get("development") and seller is None:
        seller = "deweloper"

    images: List[str] = []
    for p in node.get("photos") or []:
        if isinstance(p, dict) and p.get("id"):
            images.append(
                f"{thumb_host}/thumb/{p['id']}/3x2_m:fill_and_crop/{p.get('name') or 'zdjecie'}.jpg"
            )

    lat = lon = None
    center = ((loc.get("map") or {}).get("center")) or {}
    if center:
        lat, lon = _to_float(center.get("latitude")), _to_float(center.get("longitude"))

    title = (node.get("advertisementText") or "").strip() or (node.get("title") or "").strip() or "Mieszkanie"
    desc = (node.get("description") or "").strip() or None
    if desc and len(desc) > 500:
        desc = desc[:497].rstrip() + "..."

    return Listing(
        source=source,
        id=lid,
        url=url,
        title=title,
        price=price,
        area=area,
        rooms=_parse_rooms(node.get("numberOfRooms")) or _to_int(item_ld.get("numberOfRooms")),
        floor=_parse_floor(node.get("floorFormatted")),
        district=_district(loc.get("location") or []),
        street=(loc.get("street") or None),
        market=market,
        seller=seller,
        images=images,
        description=desc,
        lat=lat,
        lon=lon,
        created_at=node.get("addedAt") or None,
    )


# ---------------------------------------------------------------------------
# Pętla pobierania (wspólna dla gratka i morizon)
# ---------------------------------------------------------------------------

UrlBuilder = Callable[[dict, str, int], str]  # (config, market, page) -> url


def _fetch_page(session: requests.Session, url: str) -> Tuple[Optional[dict], int]:
    """Zwraca (dane strony listingu albo None gdy brak wyników, totalCount)."""
    r = session.get(url, timeout=30)
    if r.status_code == 404:
        return None, 0
    r.raise_for_status()
    data = extract_nuxt_data(r.text)
    page_data = _find_key(data, "property-listing-data-")
    decode = _find_key(data, "decode-listing-url-query-") or {}
    total = 0
    try:
        total = int((decode.get("decodeListingUrl") or {}).get("totalCount") or 0)
    except (TypeError, ValueError):
        pass
    return page_data, total


def scrape_portal(
    config: dict,
    *,
    source: str,
    base_url: str,
    thumb_host: str,
    build_url: UrlBuilder,
) -> List[Listing]:
    max_pages = int(config.get("max_pages") or 10)
    markets = ["wtorny", "pierwotny"]
    session = make_session()
    results: List[Listing] = []
    seen = set()
    pages_left = max_pages
    errors: List[str] = []

    for mi, market in enumerate(markets):
        # budżet stron dzielony po równo między rynki (resztę przejmuje kolejny)
        limit = max(1, math.ceil(pages_left / (len(markets) - mi)))
        page = 1
        while page <= limit:
            url = build_url(config, market, page)
            try:
                page_data, total = _fetch_page(session, url)
            except Exception as e:  # noqa: BLE001
                if page == 1:
                    msg = f"{source}: nie udało się pobrać 1. strony ({market}) {url}: {e}"
                    log.error(msg)
                    errors.append(msg)
                else:
                    log.warning("%s: błąd strony %d (%s): %s — przerywam ten rynek", source, page, market, e)
                break
            if page_data is None:
                if page == 1:
                    errors.append(f"{source}: 1. strona ({market}) bez danych listingu: {url}")
                break
            nodes = ((page_data.get("searchResult") or {}).get("properties") or {}).get("nodes") or []
            if not nodes:
                break
            ld = _ldjson_offers(page_data)
            for node in nodes:
                try:
                    if not isinstance(node, dict):
                        continue
                    lst = parse_node(node, source=source, base_url=base_url,
                                     thumb_host=thumb_host, market=market, ld=ld)
                except Exception:  # noqa: BLE001
                    log.warning("%s: pominięto wadliwą ofertę %r", source,
                                node.get("idOnFrontend") if isinstance(node, dict) else node,
                                exc_info=True)
                    continue
                if lst is None or lst.id in seen:
                    continue
                seen.add(lst.id)
                results.append(lst)
            log.info("%s: %s strona %d — %d ofert (łącznie %d)", source, market, page, len(nodes), len(results))
            pages_left -= 1
            if total and page * PER_PAGE >= total:
                break
            page += 1
            if page <= limit:
                polite_sleep()
        if mi < len(markets) - 1:
            polite_sleep()

    if not results and errors:
        raise RuntimeError("; ".join(errors))
    return results


# ---------------------------------------------------------------------------
# gratka.pl
# ---------------------------------------------------------------------------

GRATKA_BASE = "https://gratka.pl"
GRATKA_THUMBS = "https://thumbs.cdngr.pl"


def _gratka_url(config: dict, market: str, page: int) -> str:
    city = config.get("city") or "wroclaw"
    pf = config.get("prefilter") or {}
    params = []
    if pf.get("price_min"):
        params.append(f"cena-calkowita:min={int(pf['price_min'])}")
    if pf.get("price_max"):
        params.append(f"cena-calkowita:max={int(pf['price_max'])}")
    if pf.get("area_min"):
        params.append(f"powierzchnia-w-m2:min={int(pf['area_min'])}")
    if page > 1:
        params.append(f"page={page}")
    url = f"{GRATKA_BASE}/nieruchomosci/mieszkania/{city}/{market}"
    return url + ("?" + "&".join(params) if params else "")


def scrape(config: dict) -> List[Listing]:
    return scrape_portal(config, source=SOURCE, base_url=GRATKA_BASE,
                         thumb_host=GRATKA_THUMBS, build_url=_gratka_url)


def _demo(scrape_fn: Callable[[dict], List[Listing]]) -> None:
    import os
    import yaml

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(root, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["max_pages"] = 2
    items = scrape_fn(cfg)
    print(f"count: {len(items)}")
    print(json.dumps([x.to_dict() for x in items[:3]], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _demo(scrape)
