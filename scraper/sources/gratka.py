"""Scraper gratka.pl — mieszkania na sprzedaż, mieszkania na wynajem, pokoje.

gratka.pl i morizon.pl należą do Grupy Morizon-Gratka i działają na tym samym
backendzie (Nuxt 3 + wspólne API GraphQL, te same ID ofert, te same dane).

Pobieranie listingu: frontend renderuje stronę z zapytania GraphQL
``searchProperties(url: <ścieżka listingu>)`` wysyłanego do ``/api-gratka``
(morizon: ``/api-morizon``). Wołamy to API bezpośrednio, prosząc tylko o
potrzebne pola — ~40 KB zamiast ~1 MB HTML na stronę, bez dekodowania
devalue, a do tego z współrzędnymi (``location.map.center``). Rozmiar
strony jest sztywny (35 ofert; ``numberOfResults`` nie da się zmienić przez
URL ani API). Gdy API zawiedzie, przechodzimy na stary tryb: HTML listingu
i stan z ``<script id="__NUXT_DATA__">`` (format "devalue"). JSON-LD
(``getListingHeadTags``) daje dokładny metraż (z częścią dziesiętną).

Rynek (pierwotny/wtórny) nie jest dostępny w danych kafelka, więc dla
sprzedaży pobieramy dwa wyszukiwania z filtrem rynku — wtedy pole ``market``
jest pewne. Dla wynajmu/pokoi rynek nie ma znaczenia (``market=None``).

Szczegóły ofert dociągamy z ``getProperty`` — jednym zbiorczym zapytaniem
(aliasy p0..p34) na stronę listingu, dla wszystkich typów ogłoszeń: piętro /
liczba pięter, typ budynku, stan, rok budowy, cechy (balkon, taras, loggia,
ogród — ``detailedInformation``; winda, piwnica, miejsce postojowe —
``facilities``; meble — ``equipments``), a dla wynajmu/pokoi także kaucja /
dodatkowe opłaty / "dostępne od". Koszt: +1 zapytanie (~2-3 s) na stronę;
``config["gratka_details"] = False`` wyłącza je dla sprzedaży.

Adresy (zweryfikowane):
  sprzedaż: /nieruchomosci/mieszkania/wroclaw/{wtorny|pierwotny}
  wynajem:  /nieruchomosci/mieszkania/wroclaw/wynajem
  pokoje:   /nieruchomosci/pokoje/wroclaw   (tylko wynajem; ".../wynajem" daje 404)

Wspólny silnik (``scrape_portal``) jest używany także przez morizon.py.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
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


def floor_numbers(s: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    """'3/8' -> (3, 8), 'parter/4' -> (0, 4), 'suterena' -> (-1, None),
    'piętro 5' -> (5, None), '> 10' / 'poddasze' -> (None, None)."""
    if not s:
        return None, None
    t = str(s).strip().lower()
    t = re.sub(r"^pi[eę]tro:?\s*", "", t)
    parts = re.split(r"\s*/\s*|\s+z\s+", t, maxsplit=1)

    def one(x: str) -> Optional[int]:
        x = x.strip()
        if x.startswith("parter") or x == "0":
            return 0
        if "suteren" in x or "piwnic" in x:
            return -1
        m = re.match(r"^(-?\d+)\b", x)
        return int(m.group(1)) if m else None

    floor = one(parts[0])
    total = None
    if len(parts) > 1:
        m = re.match(r"^(\d+)\b", parts[1].strip())
        total = int(m.group(1)) if m else None
    if floor is not None and not -1 <= floor <= 60:
        floor = None
    if total is not None and not 0 <= total <= 60:
        total = None
    return floor, total


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
    deal: str = "sprzedaz",
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

    title = ((node.get("advertisementText") or "").strip() or (node.get("title") or "").strip()
             or ("Pokój" if deal == "pokoj" else "Mieszkanie"))
    desc = (node.get("description") or "").strip() or None
    if desc and len(desc) > 500:
        desc = desc[:497].rstrip() + "..."

    floor = _parse_floor(node.get("floorFormatted"))
    floor_num, floors_total = floor_numbers(floor)

    return Listing(
        source=source,
        id=lid,
        url=url,
        title=title,
        deal=deal,
        price=price,
        area=area,
        # dla pokoju "liczba pokoi" to pokoje całego mieszkania — nie podajemy
        rooms=None if deal == "pokoj" else (
            _parse_rooms(node.get("numberOfRooms")) or _to_int(item_ld.get("numberOfRooms"))),
        floor=floor,
        floor_num=floor_num,
        floors_total=floors_total,
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
# API GraphQL (to samo, z czego korzysta frontend portalu)
# ---------------------------------------------------------------------------

# pola węzła — jak w zapytaniu frontendu getPropertyListingData, plus współrzędne
_NODE_FIELDS = """
  addedAt(format: "y-MM-dd")
  advertisementText
  area
  contact {
    company { name type }
    person { name type }
  }
  development { id name }
  description(maxLength: 500)
  floorFormatted
  id
  idOnFrontend
  location {
    location
    street
    map { center { latitude longitude } }
  }
  numberOfRooms
  photos { id name }
  price { amount currency }
  priceFormatted
  title
  url
"""

_LISTING_QUERY = (
    "query listing($url: String!) {"
    " headTags: getListingHeadTags(url: $url) { script { type innerHTML } }"
    " searchResult: searchProperties(url: $url) {"
    " properties { totalCount nodes {" + _NODE_FIELDS + "} } } }"
)

_DETAILS_FIELDS = (
    'idOnFrontend detailedInformation(dateFormat: "y-MM-dd") { label value }'
    " buildingDetailedInformation { label value }"
    " facilities { icon label } equipments { icon label }"
)


class _GqlError(RuntimeError):
    pass


def _gql(session: requests.Session, api: "Api", query: str, variables: dict,
         allow_partial: bool = False, nonce: str = "") -> dict:
    # Cloudflare cache'uje odpowiedzi POST po treści zapytania (bez nagłówków,
    # do kilku godzin) — komentarz z identyfikatorem przebiegu daje świeże dane.
    if nonce:
        query = f"{query}\n# {nonce}"
    r = session.post(
        api.url,
        json={"query": query, "variables": variables},
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-MZN-Client": api.client,
            "X-MZN-Type": api.type,
            "X-MZN-Cache-Response": "false",
            "X-MZN-VERSION": "v2",  # wersja backendu używana przez SSR strony (v1 daje inne wyniki)
            "X-FF-Sort-Refreshed-At": "1",  # domyślne sortowanie jak w HTML (po odświeżeniu)
            "Origin": api.origin,
            "Referer": api.origin + "/",
        },
        timeout=30,
    )
    r.raise_for_status()
    j = r.json()
    data = j.get("data") if isinstance(j, dict) else None
    errs = j.get("errors") if isinstance(j, dict) else None
    if errs and (not allow_partial or not isinstance(data, dict)):
        raise _GqlError(f"błąd API: {errs[0].get('message') if isinstance(errs[0], dict) else errs[0]}")
    if not isinstance(data, dict):
        raise _GqlError("brak 'data' w odpowiedzi API")
    return data


class Api:
    """Adres i nagłówki API GraphQL danego portalu."""

    def __init__(self, url: str, client: str, type_: str, origin: str):
        self.url, self.client, self.type, self.origin = url, client, type_, origin


def _fetch_page_api(session: requests.Session, api: Api, path: str,
                    nonce: str = "") -> Tuple[Optional[dict], int]:
    data = _gql(session, api, _LISTING_QUERY, {"url": path}, nonce=nonce)
    sr = data.get("searchResult")
    if not isinstance(sr, dict):
        raise _GqlError("brak searchResult w odpowiedzi API")
    total = 0
    try:
        total = int(((sr.get("properties") or {}).get("totalCount")) or 0)
    except (TypeError, ValueError):
        pass
    return {"searchResult": sr, "headTags": data.get("headTags") or {}}, total


# ---------------------------------------------------------------------------
# Szczegóły ofert najmu (kaucja, opłaty, dostępność) — zbiorczo przez aliasy
# ---------------------------------------------------------------------------

def _money(v: Any) -> Optional[int]:
    if v is None:
        return None
    s = str(v).replace("\xa0", " ").lower()
    s = re.sub(r"(zł|pln|/\s*mies.*)", "", s).strip()
    s = s.replace(" ", "")
    m = re.search(r"\d+(?:[.,]\d+)?", s)
    n = _to_int(m.group()) if m else None
    return n if n and n > 0 else None


# "Stan nieruchomości" -> CONDITIONS (kolejność ma znaczenie: pierwsze trafienie)
_CONDITION_RULES = (
    ("do remontu", "do_remontu"),
    ("do odnowienia", "do_remontu"),
    ("bez białego montażu", "stan_deweloperski"),
    ("deweloper", "stan_deweloperski"),
    ("do wykończenia", "do_wykonczenia"),
    ("surowy", "do_wykonczenia"),
    ("do odświeżenia", "do_zamieszkania"),
    ("dobry", "do_zamieszkania"),          # "Dobry", "Bardzo dobry"
    ("wysoki standard", "do_zamieszkania"),
    ("po remoncie", "do_zamieszkania"),
    ("do zamieszkania", "do_zamieszkania"),
    ("pod klucz", "do_zamieszkania"),
    ("idealny", "do_zamieszkania"),
)

# "Typ budynku" -> BUILDING_TYPES ("Dom wielorodzinny" to w praktyce mały blok)
_BUILDING_RULES = (
    ("apartamentowiec", "apartamentowiec"),
    ("kamienica", "kamienica"),
    ("loft", "loft"),
    ("szeregow", "szeregowiec"),
    ("segment", "szeregowiec"),
    ("wielorodzinny", "blok"),
    ("blok", "blok"),
    ("wieżowiec", "blok"),
    ("dom", "dom"),
    ("bliźniak", "dom"),
)

# etykiety typu "Balkon: Tak" / "Pow. balkonu: 5 m²" -> FEATURES
_LABEL_FEATURES = (
    ("balkon", "balkon"),
    ("taras", "taras"),
    ("loggia", "loggia"),
    ("ogród", "ogrodek"),
    ("ogródek", "ogrodek"),
    ("ogródka", "ogrodek"),
    ("winda", "winda"),
    ("piwnica", "piwnica"),
    ("komórka", "komorka"),
    ("klimatyzacja", "klimatyzacja"),
    ("garaż", "garaz"),
    ("miejsce parkingowe", "parking"),
    ("miejsce postojowe", "parking"),
    ("zwierzęta", "zwierzeta"),
)


def _map_rules(value: Any, rules: Tuple[Tuple[str, str], ...]) -> Optional[str]:
    v = str(value or "").strip().lower()
    for needle, out in rules:
        if needle in v:
            return out
    return None


def _yes(value: Any) -> Optional[bool]:
    v = str(value or "").strip().lower()
    if v in ("tak", "yes", "true"):
        return True
    if v in ("nie", "no", "false", "brak"):
        return False
    return None


def _positive_area(value: Any) -> bool:
    m = re.search(r"\d+(?:[.,]\d+)?", str(value or ""))
    return bool(m and (_to_float(m.group()) or 0) > 0)


def _amenity_features(items: Any) -> Tuple[List[str], Optional[bool]]:
    """facilities / equipments ({icon, label}) -> (cechy, umeblowane?)."""
    feats: List[str] = []
    furnished: Optional[bool] = None
    for it in items or []:
        if not isinstance(it, dict):
            continue
        icon = (it.get("icon") or "").lower()
        label = (it.get("label") or "").lower()
        if icon == "elevator" or "winda" in label:
            feats.append("winda")
        elif icon == "basement" or "piwnic" in label:
            feats.append("piwnica")
        elif icon == "parking_places" or "miejsce postojowe" in label or "garaż" in label:
            if "na ulicy" in label:
                continue  # parkowanie na ulicy to nie cecha mieszkania
            feats.append("parking")
            if "garaż" in label or "podziemn" in label:
                feats.append("garaz")
        elif icon == "furniture" or label in ("meble", "umeblowane"):
            furnished = True
        elif "klimatyz" in label or "air_cond" in icon:
            feats.append("klimatyzacja")
        elif "komórk" in label or "storage" in icon:
            feats.append("komorka")
    return feats, furnished


def _add_features(lst: Listing, feats: List[str]) -> None:
    for f in feats:
        if f not in lst.features:
            lst.features.append(f)


def _apply_details(lst: Listing, prop: dict) -> None:
    rental = lst.deal != "sprzedaz"
    feats: List[str] = []
    for item in (prop.get("detailedInformation") or []) + (prop.get("buildingDetailedInformation") or []):
        if not isinstance(item, dict):
            continue
        label = (item.get("label") or "").strip().lower()
        value = item.get("value")
        if not label or value in (None, ""):
            continue
        if label == "piętro":
            fn, ft = floor_numbers(str(value))
            if lst.floor is None and str(value).strip():
                lst.floor = str(value).strip()
            if lst.floor_num is None:
                lst.floor_num = fn
            if lst.floors_total is None:
                lst.floors_total = ft
        elif label == "liczba pięter":
            n = _to_int(re.sub(r"[^\d]", "", str(value)) or None)
            if lst.floors_total is None and n is not None and 0 <= n <= 60:
                lst.floors_total = n
        elif label == "typ budynku":
            lst.building_type = lst.building_type or _map_rules(value, _BUILDING_RULES)
        elif label in ("stan nieruchomości", "stan mieszkania", "stan wykończenia"):
            lst.condition = lst.condition or _map_rules(value, _CONDITION_RULES)
        elif "umeblow" in label:
            yn = _yes(value)
            if yn is not None and lst.furnished is None:
                lst.furnished = yn
        elif label == "lokalizacja garażu":
            if _yes(value) is not False:
                feats += ["garaz", "parking"]
        elif label.startswith("pow. "):
            f = _map_rules(label[5:], _LABEL_FEATURES)
            if f and _positive_area(value):
                feats.append(f)
        elif rental and ("depozyt" in label or "kaucj" in label):
            lst.deposit = lst.deposit or _money(value)
        elif rental and ("dodatkowe koszty" in label or "czynsz" in label or "opłaty" in label
                         or "oplaty" in label):
            lst.rent_extra = lst.rent_extra or _money(value)
        elif rental and ("dostępne od" in label or "dostepne od" in label):
            m = re.match(r"\d{4}-\d{2}-\d{2}", str(value).strip())
            if m:
                lst.available_from = m.group()
        elif "rok budowy" in label and lst.build_year is None:
            y = _to_int(re.sub(r"\D", "", str(value))[:4] or None)
            if y and 1800 <= y <= 2100:
                lst.build_year = y
        else:
            f = _map_rules(label, _LABEL_FEATURES)
            yn = _yes(value)
            # "Miejsce parkingowe: w garażu" itp. — wartość opisowa też znaczy "jest"
            if f and (yn or (yn is None and f in ("parking", "garaz"))):
                feats.append(f)
    fac, _ = _amenity_features(prop.get("facilities"))
    eq, furnished = _amenity_features(prop.get("equipments"))
    feats += fac + eq
    if furnished and lst.furnished is None:
        lst.furnished = True
    _add_features(lst, feats)
    if lst.floor_num is not None and lst.floors_total is not None and lst.floors_total < lst.floor_num:
        lst.floors_total = None  # sprzeczne dane portalu
    # portal miewa śmieci w tych polach (np. "73280", "26,5000") — odrzucamy nierealne
    if lst.price:
        if lst.rent_extra is not None and not (50 <= lst.rent_extra <= lst.price):
            lst.rent_extra = None
        if lst.deposit is not None and not (100 <= lst.deposit <= 10 * lst.price):
            lst.deposit = None


def _enrich_details(session: requests.Session, api: Api, base_url: str,
                    items: List[Listing], nonce: str = "") -> None:
    """Jedno zapytanie GraphQL z aliasami p0..pN: getProperty dla każdej oferty."""
    if not items:
        return
    decl, parts, variables = [], [], {}
    for i, lst in enumerate(items):
        path = lst.url[len(base_url):] if lst.url.startswith(base_url) else lst.url
        decl.append(f"$u{i}: String!")
        parts.append(f"p{i}: getProperty(url: $u{i}) {{ {_DETAILS_FIELDS} }}")
        variables[f"u{i}"] = path
    query = "query details(" + ", ".join(decl) + ") { " + " ".join(parts) + " }"
    data = _gql(session, api, query, variables, allow_partial=True, nonce=nonce)
    for i, lst in enumerate(items):
        prop = data.get(f"p{i}")
        if isinstance(prop, dict):
            try:
                _apply_details(lst, prop)
            except Exception:  # noqa: BLE001 — szczegóły to tylko dodatek
                log.debug("nie udało się zastosować szczegółów %s", lst.url, exc_info=True)


# ---------------------------------------------------------------------------
# Pętla pobierania (wspólna dla gratka i morizon)
# ---------------------------------------------------------------------------

# (config, deal, market|None, page) -> pełny URL listingu
UrlBuilder = Callable[[dict, str, Optional[str], int], str]


def _fetch_page(session: requests.Session, url: str) -> Tuple[Optional[dict], int]:
    """Tryb awaryjny (HTML): zwraca (dane strony listingu albo None gdy brak
    wyników, totalCount)."""
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
    api: Optional[Api] = None,
    deal: str = "sprzedaz",
) -> List[Listing]:
    max_pages = int(config.get("max_pages") or 10)
    markets: List[Optional[str]] = ["wtorny", "pierwotny"] if deal == "sprzedaz" else [None]
    rental = deal != "sprzedaz"
    session = make_session()
    results: List[Listing] = []
    seen = set()
    pages_left = max_pages
    errors: List[str] = []
    use_api = api is not None
    nonce = f"run {int(time.time())}"
    # szczegóły (getProperty) — dla sprzedaży można je wyłączyć w configu
    details_ok = use_api and (rental or bool(config.get("gratka_details", True)))

    def pause() -> None:
        # API zwraca ~40 KB w ~0.3 s — krótsza przerwa wystarczy; HTML jak dotąd
        if use_api:
            polite_sleep(0.3, 0.8)
        else:
            polite_sleep()

    for mi, market in enumerate(markets):
        mlabel = market or deal
        # budżet stron dzielony po równo między rynki (resztę przejmuje kolejny)
        limit = max(1, math.ceil(pages_left / (len(markets) - mi)))
        page = 1
        while page <= limit:
            url = build_url(config, deal, market, page)
            page_data = None
            total = 0
            try:
                if use_api:
                    try:
                        page_data, total = _fetch_page_api(session, api, url[len(base_url):], nonce)
                    except Exception as e:  # noqa: BLE001
                        log.warning("%s: API zawiodło (%s) — przechodzę na pobieranie HTML", source, e)
                        use_api = False
                        polite_sleep()
                if not use_api:
                    page_data, total = _fetch_page(session, url)
            except Exception as e:  # noqa: BLE001
                if page == 1:
                    msg = f"{source}: nie udało się pobrać 1. strony ({mlabel}) {url}: {e}"
                    log.error(msg)
                    errors.append(msg)
                else:
                    log.warning("%s: błąd strony %d (%s): %s — przerywam", source, page, mlabel, e)
                break
            if page_data is None:
                if page == 1:
                    errors.append(f"{source}: 1. strona ({mlabel}) bez danych listingu: {url}")
                break
            nodes = ((page_data.get("searchResult") or {}).get("properties") or {}).get("nodes") or []
            if not nodes:
                break
            ld = _ldjson_offers(page_data)
            new: List[Listing] = []
            for node in nodes:
                try:
                    if not isinstance(node, dict):
                        continue
                    lst = parse_node(node, source=source, base_url=base_url,
                                     thumb_host=thumb_host, market=market, ld=ld, deal=deal)
                except Exception:  # noqa: BLE001
                    log.warning("%s: pominięto wadliwą ofertę %r", source,
                                node.get("idOnFrontend") if isinstance(node, dict) else node,
                                exc_info=True)
                    continue
                if lst is None or lst.id in seen:
                    continue
                seen.add(lst.id)
                new.append(lst)
            if details_ok and new:
                pause()
                try:
                    _enrich_details(session, api, base_url, new, nonce)
                except Exception as e:  # noqa: BLE001 — bez szczegółów też się da
                    log.warning("%s: nie udało się pobrać szczegółów ofert (%s) — pomijam je dalej", source, e)
                    details_ok = False
            results.extend(new)
            log.info("%s: %s strona %d — %d ofert (łącznie %d)", source, mlabel, page, len(nodes), len(results))
            pages_left -= 1
            if total and page * PER_PAGE >= total:
                break
            page += 1
            if page <= limit:
                pause()
        if mi < len(markets) - 1:
            pause()

    if not results and errors:
        raise RuntimeError("; ".join(errors))
    _drop_outlier_coords(results)
    return results


def _drop_outlier_coords(items: List[Listing], max_deg: float = 0.5) -> None:
    """Portal miewa błędne współrzędne (np. środek Polski 51.92/19.15 jako
    zaślepka, ulica zgeokodowana w innym mieście). Wszystkie oferty są z jednego
    miasta, więc punkty dalej niż ~0.5° od mediany wyników zerujemy."""
    pts = sorted((x.lat, x.lon) for x in items if x.lat is not None and x.lon is not None)
    if len(pts) < 5:
        return
    mlat = sorted(p[0] for p in pts)[len(pts) // 2]
    mlon = sorted(p[1] for p in pts)[len(pts) // 2]
    for x in items:
        if x.lat is not None and x.lon is not None and (
                abs(x.lat - mlat) > max_deg or abs(x.lon - mlon) > max_deg):
            x.lat = x.lon = None


# ---------------------------------------------------------------------------
# gratka.pl
# ---------------------------------------------------------------------------

DEALS = ("sprzedaz", "wynajem", "pokoj")

GRATKA_BASE = "https://gratka.pl"
GRATKA_THUMBS = "https://thumbs.cdngr.pl"
GRATKA_API = Api("https://gratka.pl/api-gratka", "GRATKA", "GRATKA", GRATKA_BASE)


def _gratka_url(config: dict, deal: str, market: Optional[str], page: int) -> str:
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
    if deal == "pokoj":
        url = f"{GRATKA_BASE}/nieruchomosci/pokoje/{city}"
    elif deal == "wynajem":
        url = f"{GRATKA_BASE}/nieruchomosci/mieszkania/{city}/wynajem"
    else:
        url = f"{GRATKA_BASE}/nieruchomosci/mieszkania/{city}/{market}"
    return url + ("?" + "&".join(params) if params else "")


def scrape(config: dict, deal: str = "sprzedaz") -> List[Listing]:
    if deal not in DEALS:
        raise ValueError(f"{SOURCE}: nieobsługiwany typ ogłoszenia {deal!r}")
    return scrape_portal(config, source=SOURCE, base_url=GRATKA_BASE,
                         thumb_host=GRATKA_THUMBS, build_url=_gratka_url,
                         api=GRATKA_API, deal=deal)


def _demo(scrape_fn: Callable[..., List[Listing]]) -> None:
    """Użycie: python -m scraper.sources.gratka [sprzedaz|wynajem|pokoj] [max_pages]"""
    import os
    import sys
    import yaml

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    deal = sys.argv[1] if len(sys.argv) > 1 else "sprzedaz"
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    with open(os.path.join(root, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    deal_cfg = (cfg.get("deals") or {}).get(deal) or {}
    cfg["prefilter"] = deal_cfg.get("prefilter") or cfg.get("prefilter") or {}
    cfg["max_pages"] = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    t0 = time.time()
    items = scrape_fn(cfg, deal)
    print(f"deal: {deal}  count: {len(items)}  time: {time.time() - t0:.1f}s")
    print(json.dumps([x.to_dict() for x in items[:3]], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _demo(scrape)
