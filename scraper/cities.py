"""Miejscowości: ustalanie identyfikatorów lokalizacji w każdym portalu.

Każdy portal opisuje miasto inaczej (OLX: city_id/region_id, Otodom: ścieżka
"dolnoslaskie/wroclawski/siechnice/siechnice", gratka: "siechnice" albo
"radwanice-49617", ...). Ten moduł ustala je RAZ dla każdej miejscowości,
korzystając z tych samych podpowiedzi lokalizacji, których używają strony
portali, i zapisuje wynik w scraper/cities_cache.json (commitowany do repo).
Scraper w czasie działania czyta już tylko ten plik — chyba że w config.yaml
pojawi się nowa miejscowość, wtedy ustala ją automatycznie przy pierwszym
przebiegu (`ensure_cities`).

Użycie:
    python -m scraper.cities add "Kraków"          # ustal i zapisz (+ słownik ulic do mapy)
    python -m scraper.cities add siechnice --no-geo
    python -m scraper.cities add "Radwanice" --county wrocławski
    python -m scraper.cities show [slug]
    python -m scraper.cities add wroclaw --force   # ustal od nowa (nadpisuje zapis)

Źródła (sprawdzone na żywo, tym samym, czego używają strony portali):
  OLX      GET  https://www.olx.pl/api/v1/geo-encoder/location-autocomplete/?query=...
           -> city.id / region.id / city.normalized_name (+ powiat, gmina, lat/lon).
           Wynik OLX to "kotwica": daje województwo i powiat do rozróżniania
           miejscowości o tej samej nazwie w pozostałych portalach.
  Otodom   POST https://www.otodom.pl/api/query  (GraphQL `autocomplete`,
           levels=["city_or_village"]) -> locationsObjects[].id = ścieżka lokalizacji.
           Jedno małe zapytanie — nie dotyka stron wyników (WAF).
  gratka   POST https://gratka.pl/api-gratka (GraphQL `getLocationSuggestions`
           -> id lokalizacji, potem `decodeListingUrl("/nieruchomosci/mieszkania/<slug>-<id>")`
           -> uniqueUrlParts = kanoniczny fragment adresu, np. ["radwanice-49617"]).
  morizon  to samo API (/api-morizon) — `decodeListingUrl("/mieszkania/<slug gratki>/")`
           -> uniqueUrlParts, np. ["wroclawski", "miasto-siechnice"].
  nieruchomosci-online  GET https://www.nieruchomosci-online.pl/?module=locationTip&type=cityTip&cn=...
           (X-Requested-With) -> id miasta; wyszukiwarka przyjmuje "Nazwa:id".
  domiporta GET https://www.domiporta.pl/Autocomplete/City?term=... -> id,
           potem /mieszkanie/sprzedam?Localizations[0].Id=<id> przekierowuje na
           kanoniczną ścieżkę, np. "dolnoslaskie/wroclawski/radwanice".
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from scraper.http import make_session

log = logging.getLogger("scraper.cities")

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = Path(__file__).resolve().parent / "cities_cache.json"
DEFAULT_CITY = "wroclaw"
PORTALS = ("olx", "otodom", "gratka", "morizon", "nieruchomosci_online", "domiporta")

# Dokładnie to, czego scrapery używały przed wprowadzeniem miejscowości (twarde
# stałe dla Wrocławia). Gdy cities_cache.json nie ma wpisu wrocławskiego (np. ktoś
# go usunął), używamy tych wartości — zachowanie dla Wrocławia się nie zmienia.
WROCLAW = {
    "slug": "wroclaw",
    "name": "Wrocław",
    "voivodeship": "dolnośląskie",
    "county": "Wrocław",
    "municipality": "Wrocław",
    "lat": 51.10195,
    "lon": 17.03667,
    "portals": {
        "olx": {"city_id": 19701, "region_id": 3, "slug": "wroclaw"},
        "otodom": {"location": "dolnoslaskie/wroclaw/wroclaw/wroclaw"},
        "gratka": {"slug": "wroclaw", "location_id": "88889"},
        "morizon": {"slug": "wroclaw", "location_id": "88889"},
        "nieruchomosci_online": {"base": "https://wroclaw.nieruchomosci-online.pl/szukaj.html",
                                 "city_param": "Wrocław", "id": "17876"},
        "domiporta": {"path": "dolnoslaskie/wroclaw", "id": 57389},
    },
    "failed": {},
}

PAUSE = 1.0  # s między zapytaniami do jednego portalu


# ---------------------------------------------------------------- tekst

def norm(s: Any) -> str:
    """'Wrocław ' -> 'wroclaw', 'Kędzierzyn-Koźle' -> 'kedzierzyn-kozle'."""
    s = unicodedata.normalize("NFKD", str(s or "").strip().lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.replace("ł", "l")


def slugify(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", norm(s)).strip("-")


def same_name(a: Any, b: Any) -> bool:
    return slugify(a) == slugify(b) and bool(slugify(a))


# ---------------------------------------------------------------- config

def city_specs(cfg: dict) -> List[dict]:
    """config.yaml -> [{"slug", "name"?, "voivodeship"?, "county"?}, ...].

    Obsługuje:  cities: [wroclaw, {name: "Kraków", slug: krakow}, "Siechnice"]
    oraz stary zapis  city: wroclaw  (jedno miasto). Brak obu = Wrocław."""
    raw = cfg.get("cities")
    if raw is None:
        raw = [cfg.get("city")] if isinstance(cfg.get("city"), str) and cfg.get("city") else [DEFAULT_CITY]
    if isinstance(raw, (str, dict)):
        raw = [raw]
    out: List[dict] = []
    seen = set()
    for item in raw or []:
        if isinstance(item, str):
            spec = {"query": item.strip()}
        elif isinstance(item, dict):
            spec = {k: v for k, v in item.items() if v not in (None, "")}
            spec["query"] = str(spec.get("name") or spec.get("slug") or "").strip()
        else:
            continue
        if not spec["query"]:
            continue
        spec["slug"] = slugify(spec.get("slug") or spec["query"])
        if spec["slug"] in seen:
            continue
        seen.add(spec["slug"])
        out.append(spec)
    return out or [{"query": DEFAULT_CITY, "slug": DEFAULT_CITY}]


# ---------------------------------------------------------------- cache

def load_cache(path: Path = CACHE_PATH) -> Dict[str, dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as e:
        log.warning("Nie udało się wczytać %s (%s)", path, e)
        return {}


def save_cache(cache: Dict[str, dict], path: Path = CACHE_PATH) -> None:
    data = {k: cache[k] for k in sorted(cache)}
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_city(slug: str, cache: Optional[Dict[str, dict]] = None) -> Optional[dict]:
    cache = load_cache() if cache is None else cache
    entry = cache.get(slug)
    if entry is None and slug == DEFAULT_CITY:
        entry = json.loads(json.dumps(WROCLAW))
    return entry


def portal_location(entry: dict, portal: str) -> Optional[dict]:
    """Słownik przekazywany scraperowi jako config["city"] (None = portal nie zna miasta)."""
    loc = (entry.get("portals") or {}).get(portal)
    if not isinstance(loc, dict):
        return None
    out = {"slug": entry["slug"], "name": entry.get("name") or entry["slug"]}
    out.update(loc)
    return out


def location_for(city: str, portal: str) -> dict:
    """Dla bloków __main__ scraperów: config["city"] danej miejscowości z cities_cache.json.
    Kończy program z komunikatem, gdy miejscowość / portal nie jest ustalony."""
    entry = get_city(slugify(city))
    if entry is None:
        sys.exit("Brak %r w %s — najpierw: python -m scraper.cities add %r" % (city, CACHE_PATH.name, city))
    loc = portal_location(entry, portal)
    if loc is None:
        sys.exit("%s: portal nie zna miejscowości %s (%s)" % (
            portal, entry.get("name"), (entry.get("failed") or {}).get(portal, "brak wpisu")))
    return loc


# ---------------------------------------------------------------- kotwica (OLX)

OLX_AUTOCOMPLETE = "https://www.olx.pl/api/v1/geo-encoder/location-autocomplete/"


def _matches_hints(spec: dict, voiv: Any, county: Any) -> bool:
    if spec.get("voivodeship") and slugify(spec["voivodeship"]) != slugify(voiv):
        return False
    if spec.get("county"):
        c = slugify(re.sub(r"^(pow\.?|powiat)\s+", "", str(spec["county"]), flags=re.I))
        if c != slugify(county):
            return False
    return True


def resolve_anchor(session, spec: dict) -> Tuple[dict, List[str]]:
    """Ustala nazwę, województwo, powiat, gminę i współrzędne miejscowości (OLX).
    Zwraca (anchor, ostrzeżenia). Rzuca LookupError, gdy nic nie pasuje."""
    q = spec.get("name") or spec["query"]
    r = session.get(OLX_AUTOCOMPLETE, params={"query": q}, timeout=30,
                    headers={"Accept": "application/json"})
    r.raise_for_status()
    rows = [x for x in (r.json().get("data") or []) if isinstance(x, dict) and x.get("city")]
    by_id: Dict[Any, dict] = {}
    for x in rows:  # ten sam city.id powtarza się dla dzielnic miasta
        if same_name(x["city"].get("name"), q) and _matches_hints(
                spec, (x.get("region") or {}).get("name"), (x.get("county") or {}).get("name")):
            by_id.setdefault(x["city"].get("id"), x)
    cands = list(by_id.values())
    # "Kraków" i "Krąków" mają ten sam slug — wolimy dokładną pisownię
    cands = [x for x in cands if str(x["city"].get("name")).lower() == q.strip().lower()] or cands
    if not cands:
        raise LookupError("OLX nie zna miejscowości %r (podpowiedzi: %s)" % (
            q, ", ".join(_olx_label(x) for x in rows[:6]) or "brak"))
    # gdy w configu był slug ("krakow"), wolimy kandydata o dokładnie takiej nazwie w OLX
    exact = [x for x in cands if x["city"].get("normalized_name") == spec["slug"]]
    best = (exact or cands)[0]
    warnings = []
    if len(cands) > 1:
        other = next(x for x in cands if x is not best)
        warnings.append("Kilka miejscowości o nazwie %s: %s. Wybrano: %s. Inną wskażesz w config.yaml, "
                        "np. {name: \"%s\", county: \"%s\"}" % (
                            best["city"]["name"], "; ".join(_olx_label(x) for x in cands), _olx_label(best),
                            other["city"]["name"], (other.get("county") or {}).get("name") or ""))
    anchor = {
        "name": best["city"]["name"],
        "voivodeship": (best.get("region") or {}).get("name", "").lower() or None,
        "county": (best.get("county") or {}).get("name"),
        "municipality": (best.get("municipality") or {}).get("name"),
        "lat": best["city"].get("lat"),
        "lon": best["city"].get("lon"),
        "_olx": best,
    }
    return anchor, warnings


def _olx_label(x: dict) -> str:
    return "%s (pow. %s, gm. %s, %s)" % (
        (x.get("city") or {}).get("name"), (x.get("county") or {}).get("name"),
        (x.get("municipality") or {}).get("name"), (x.get("region") or {}).get("name"))


# ---------------------------------------------------------------- portale

def resolve_olx(session, anchor: dict) -> dict:
    c, reg = anchor["_olx"]["city"], anchor["_olx"]["region"]
    return {"city_id": int(c["id"]), "region_id": int(reg["id"]), "slug": c["normalized_name"]}


OTODOM_API = "https://www.otodom.pl/api/query"
_OTODOM_Q = """query autocomplete($query: String!, $ranking: RankingSystemInput, $levels: [String!]) {
  autocomplete(query: $query, ranking: $ranking, levels: $levels) {
    __typename
    ... on FoundLocations { locationsObjects { id detailedLevel name fullName } }
  }
}"""


def resolve_otodom(session, anchor: dict) -> dict:
    r = session.post(OTODOM_API, timeout=30, json={
        "operationName": "autocomplete", "query": _OTODOM_Q,
        "variables": {"query": anchor["name"], "ranking": {"type": "BLENDED_INFIX_LOOKUP_SUGGEST"},
                      "levels": ["city_or_village"]}},
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "Origin": "https://www.otodom.pl", "Referer": "https://www.otodom.pl/pl"})
    if r.status_code in (403, 405, 429):
        raise RuntimeError("Otodom blokuje zapytania (HTTP %s) — spróbuj później" % r.status_code)
    r.raise_for_status()
    ac = ((r.json().get("data") or {}).get("autocomplete") or {})
    locs = ac.get("locationsObjects") or []
    if not locs:
        raise LookupError("brak podpowiedzi (%s)" % ac.get("__typename"))
    voiv, county = slugify(anchor["voivodeship"]), slugify(anchor["county"])
    cands = []
    for l in locs:
        parts = str(l.get("id") or "").split("/")
        if not same_name(l.get("name"), anchor["name"]) or len(parts) < 2 or parts[0] != voiv:
            continue
        if county and parts[1] != county:
            continue
        cands.append(l)
    if not cands:
        raise LookupError("brak %s (%s, pow. %s) w podpowiedziach: %s" % (
            anchor["name"], anchor["voivodeship"], anchor["county"], ", ".join(l.get("id", "") for l in locs[:6])))
    muni = slugify(anchor.get("municipality"))
    cands.sort(key=lambda l: (l.get("detailedLevel") != "city",
                              bool(muni) and str(l["id"]).split("/")[2:3] != [muni]))
    return {"location": cands[0]["id"]}


def _mzn_api(portal: str):
    if portal == "morizon":
        from scraper.sources.morizon import MORIZON_API
        return MORIZON_API
    from scraper.sources.gratka import GRATKA_API
    return GRATKA_API


_GR_SUGGEST = ("query s($q: String) { getLocationSuggestions(first: 15, searchQuery: $q) "
               "{ edges { node { id name description } } } }")
_GR_DECODE = ("query d($url: String!) { decodeListingUrl(url: $url) { url totalCount "
              "listingParameters { locations { id name nameFull uniqueUrlParts } } } }")


def _gr_decode(session, portal: str, path: str) -> Optional[dict]:
    from scraper.sources.gratka import _gql
    data = _gql(session, _mzn_api(portal), _GR_DECODE, {"url": path})
    dec = data.get("decodeListingUrl")
    if not isinstance(dec, dict):
        return None
    locs = ((dec.get("listingParameters") or {}).get("locations")) or []
    return locs[0] if len(locs) == 1 and isinstance(locs[0], dict) else None


def resolve_gratka(session, anchor: dict) -> dict:
    from scraper.sources.gratka import _gql
    data = _gql(session, _mzn_api("gratka"), _GR_SUGGEST, {"q": anchor["name"]})
    nodes = [e.get("node") or {} for e in ((data.get("getLocationSuggestions") or {}).get("edges") or [])]
    voiv, county = slugify(anchor["voivodeship"]), slugify(anchor["county"])
    cands = []
    for n in nodes:
        # description: "miasto, dolnośląskie, wrocławski, Siechnice" / "wieś, ..." / "gmina, ..."
        desc = [p.strip() for p in str(n.get("description") or "").split(",")]
        if not same_name(n.get("name"), anchor["name"]) or not desc or desc[0] not in ("miasto", "wieś", "osada"):
            continue
        if len(desc) < 2 or slugify(desc[1]) != voiv:
            continue
        if len(desc) > 2 and county and slugify(desc[2]) not in (county, ""):
            continue
        cands.append(n)
    if not cands:
        raise LookupError("brak %s w podpowiedziach (%s)" % (
            anchor["name"], "; ".join("%s: %s" % (n.get("name"), n.get("description")) for n in nodes[:5]) or "brak"))
    cands.sort(key=lambda n: not str(n.get("description")).startswith("miasto"))
    loc_id = str(cands[0]["id"])
    # zwykle adres to sama nazwa ("siechnice"); przy nazwach niejednoznacznych portal
    # używa "<nazwa>-<liczba>" — "<nazwa>-<id lokalizacji>" przekierowuje na właściwy
    loc = None
    for path in ("/nieruchomosci/mieszkania/%s" % slugify(anchor["name"]),
                 "/nieruchomosci/mieszkania/%s-%s" % (slugify(anchor["name"]), loc_id)):
        time.sleep(PAUSE)
        loc = _gr_decode(session, "gratka", path)
        if loc and str(loc.get("id")) == loc_id and loc.get("uniqueUrlParts"):
            return {"slug": "/".join(loc["uniqueUrlParts"]), "location_id": loc_id}
    raise LookupError("decodeListingUrl nie potwierdził lokalizacji %s (%r)" % (loc_id, loc))


def resolve_morizon(session, anchor: dict, gratka: Optional[dict]) -> dict:
    if not gratka:
        raise LookupError("wymaga ustalonej lokalizacji gratki (wspólna baza)")
    loc = _gr_decode(session, "morizon", "/mieszkania/%s/" % gratka["slug"])
    if not loc or str(loc.get("id")) != str(gratka["location_id"]) or not loc.get("uniqueUrlParts"):
        raise LookupError("decodeListingUrl nie potwierdził lokalizacji (%r)" % (loc,))
    return {"slug": "/".join(loc["uniqueUrlParts"]), "location_id": str(loc["id"])}


NO_TIP = "https://www.nieruchomosci-online.pl/"
NO_SEARCH = "https://www.nieruchomosci-online.pl/szukaj.html"


def resolve_nieruchomosci_online(session, anchor: dict) -> dict:
    r = session.get(NO_TIP, params={"module": "locationTip", "type": "cityTip", "cn": anchor["name"].lower(),
                                    "httpc": "227"}, timeout=30,
                    headers={"X-Requested-With": "XMLHttpRequest",
                             "Accept": "application/json, text/javascript, */*; q=0.01"})
    r.raise_for_status()
    rows = r.json() if r.text.strip().startswith("[") else []
    voiv, county = slugify(anchor["voivodeship"]), slugify(anchor["county"])
    cands = []
    for x in rows:
        if not isinstance(x, dict) or not same_name(x.get("cn"), anchor["name"]) or int(x.get("idQ") or 0) or int(x.get("idS") or 0):
            continue
        # rn = województwo, a przy niejednoznacznych nazwach "pow. wrocławski" / "pow. m. Kraków"
        rn = slugify(re.sub(r"^pow\.\s*(m\.\s*)?", "", str(x.get("rn") or "")))
        if rn and rn not in (voiv, county):
            continue
        cands.append(x)
    if not cands:
        raise LookupError("brak %s w podpowiedziach (%s)" % (
            anchor["name"], "; ".join("%s (%s)" % (x.get("cn"), x.get("rn")) for x in rows[:6]) or "brak"))
    if len(cands) > 1:
        exact = [x for x in cands if slugify(re.sub(r"^pow\.\s*(m\.\s*)?", "", str(x.get("rn") or ""))) == county]
        cands = exact or cands
    x = cands[0]
    return {"base": NO_SEARCH, "city_param": "%s:%s" % (x["cn"], x["id"]), "id": str(x["id"])}


DP_BASE = "https://www.domiporta.pl"


def resolve_domiporta(session, anchor: dict) -> dict:
    r = session.get(DP_BASE + "/Autocomplete/City", params={"term": anchor["name"]}, timeout=30,
                    headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"})
    r.raise_for_status()
    voiv, county = slugify(anchor["voivodeship"]), slugify(anchor["county"])
    cands = []
    for x in r.json() or []:
        if not isinstance(x, dict) or any(x.get(k) for k in ("d", "s", "o")):
            continue  # dzielnica / ulica / osiedle
        m, reg, pw = x.get("m") or {}, x.get("r") or {}, x.get("p") or {}
        if not same_name(m.get("Name"), anchor["name"]) or slugify(reg.get("Name")) != voiv:
            continue
        if pw.get("Name") and county and slugify(pw["Name"]) != county:
            continue
        cands.append(x)
    if not cands:
        raise LookupError("brak %s (%s) w podpowiedziach" % (anchor["name"], anchor["voivodeship"]))
    lid = int(cands[0]["id"])
    time.sleep(PAUSE)
    # portal przekierowuje na kanoniczną ścieżkę lokalizacji
    r = session.get(DP_BASE + "/mieszkanie/sprzedam", params={"Localizations[0].Id": lid}, timeout=30)
    r.raise_for_status()
    path = unquote(urlparse(r.url).path)
    prefix = "/mieszkanie/sprzedam/"
    if not path.startswith(prefix) or len(path) <= len(prefix):
        raise LookupError("brak przekierowania na ścieżkę lokalizacji (%s)" % r.url)
    m = re.search(r'name="Localizations\[0\]\.Id"[^>]*value="(\d+)"', r.text) or \
        re.search(r'value="(\d+)"[^>]*name="Localizations\[0\]\.Id"', r.text)
    if not m or int(m.group(1)) != lid:
        raise LookupError("strona wyników nie potwierdziła lokalizacji %s" % lid)
    return {"path": path[len(prefix):].strip("/"), "id": lid}


# ---------------------------------------------------------------- całość

def resolve_city(spec: dict, portals: Optional[List[str]] = None,
                 session=None) -> dict:
    """Ustala lokalizację miejscowości we wszystkich portalach (sieć!). Nie zapisuje."""
    session = session or make_session()
    portals = list(portals or PORTALS)
    anchor, warnings = resolve_anchor(session, spec)
    entry: Dict[str, Any] = {
        "slug": spec["slug"],
        "name": spec.get("name") or anchor["name"],
        "voivodeship": anchor["voivodeship"],
        "county": anchor["county"],
        "municipality": anchor["municipality"],
        "lat": anchor["lat"],
        "lon": anchor["lon"],
        "resolved": _now_iso(),
        "portals": {},
        "failed": {},
    }
    if warnings:
        entry["warnings"] = warnings
    steps: List[Tuple[str, Callable[[], dict]]] = [
        ("olx", lambda: resolve_olx(session, anchor)),
        ("otodom", lambda: resolve_otodom(session, anchor)),
        ("gratka", lambda: resolve_gratka(session, anchor)),
        ("morizon", lambda: resolve_morizon(session, anchor, entry["portals"].get("gratka"))),
        ("nieruchomosci_online", lambda: resolve_nieruchomosci_online(session, anchor)),
        ("domiporta", lambda: resolve_domiporta(session, anchor)),
    ]
    for name, fn in steps:
        if name not in portals and not (name == "gratka" and "morizon" in portals):
            continue
        try:
            entry["portals"][name] = fn()
        except Exception as e:  # noqa: BLE001 — portal bez tej miejscowości jest pomijany
            entry["failed"][name] = ("%s: %s" % (type(e).__name__, e))[:300]
            log.warning("[%s] %s: nie udało się ustalić lokalizacji: %s", spec["slug"], name, e)
        time.sleep(PAUSE)
    if "gratka" not in portals and "morizon" in portals:
        entry["portals"].pop("gratka", None)
    return entry


def ensure_cities(cfg: dict, resolve_missing: bool = True,
                  build_geo: bool = True) -> List[dict]:
    """Wpisy cities_cache.json dla miejscowości z config.yaml (w tej kolejności).
    Nowe miejscowości ustala i zapisuje automatycznie (pierwszy przebieg po dopisaniu
    do config.yaml); gdy się nie da — pomija je z ostrzeżeniem."""
    cache = load_cache()
    out: List[dict] = []
    changed = False
    for spec in city_specs(cfg):
        entry = get_city(spec["slug"], cache)
        if entry is None and resolve_missing:
            log.info("[%s] nowa miejscowość — ustalam lokalizację w portalach…", spec["slug"])
            try:
                entry = resolve_city(spec)
                cache[spec["slug"]] = entry
                changed = True
                _log_entry(entry)
            except Exception as e:  # noqa: BLE001
                log.error("[%s] nie udało się ustalić miejscowości: %s — pomijam ją", spec["slug"], e)
                continue
        elif entry is not None and resolve_missing and _retry_due(entry):
            # portale, które przy ustalaniu padły z przyczyn przejściowych (sieć, blokada),
            # próbujemy ponownie najwyżej raz na dobę
            entry = _retry_failed(spec, entry)
            cache[spec["slug"]] = entry
            changed = True
        if entry is not None and build_geo and resolve_missing and _geo_due(entry):
            # słownik ulic do mapy: przy pierwszym przebiegu, a gdy Overpass zawiódł — raz na dobę
            entry = dict(entry, geo_tried=_now_iso())
            cache[spec["slug"]] = entry
            changed = True
            _build_geo(entry, max_seconds=GEO_MAX_SECONDS)
        if entry is None:
            log.error("[%s] brak w %s — uruchom: python -m scraper.cities add %s",
                      spec["slug"], CACHE_PATH.name, spec["query"])
            continue
        if spec.get("name") and spec["name"] != entry.get("name"):
            entry = dict(entry, name=spec["name"])  # nazwa wyświetlana z configu
        out.append(entry)
    if changed:
        save_cache(cache)
    return out


RETRY_EVERY_S = 24 * 3600
GEO_MAX_SECONDS = 8 * 60  # limit budowy słownika ulic w automatycznym przebiegu


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _age_s(stamp: Optional[str]) -> float:
    try:
        t = datetime.strptime(stamp or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return float("inf")
    return time.time() - t


def _geo_due(entry: dict) -> bool:
    from scraper.geo import gazetteer_path
    if gazetteer_path(entry["slug"]).exists():
        return False
    return _age_s(entry.get("geo_tried")) > RETRY_EVERY_S


def _transient(reason: str) -> bool:
    return not str(reason).startswith("LookupError")  # LookupError = portal nie zna miejscowości


def _retry_due(entry: dict) -> bool:
    if entry.get("slug") == DEFAULT_CITY and "resolved" not in entry:
        return False  # wbudowane wartości Wrocławia
    if not any(_transient(r) for r in (entry.get("failed") or {}).values()):
        return False
    return _age_s(entry.get("retried") or entry.get("resolved")) > RETRY_EVERY_S


def _retry_failed(spec: dict, entry: dict) -> dict:
    portals = [p for p, r in (entry.get("failed") or {}).items() if _transient(r)]
    entry = json.loads(json.dumps(entry))
    entry["retried"] = _now_iso()
    log.info("[%s] ponawiam ustalanie lokalizacji: %s", entry["slug"], ", ".join(portals))
    try:
        fresh = resolve_city(dict(spec, name=entry.get("name")), portals)
    except Exception as e:  # noqa: BLE001
        log.warning("[%s] ponowienie nie powiodło się: %s", entry["slug"], e)
        return entry
    for p in portals:
        if p in fresh["portals"]:
            entry.setdefault("portals", {})[p] = fresh["portals"][p]
            entry["failed"].pop(p, None)
            log.info("[%s] %s: ustalono %s", entry["slug"], p, fresh["portals"][p])
        elif p in fresh["failed"]:
            entry["failed"][p] = fresh["failed"][p]
    return entry


def _build_geo(entry: dict, max_seconds: Optional[float] = None) -> Optional[Path]:
    try:
        from scraper.geo import gazetteer_path
        from scraper.geo.build_gazetteer import build_for_city
        path = gazetteer_path(entry["slug"])
        if path.exists():
            return path
        log.info("[%s] buduję słownik ulic z OpenStreetMap (%s)…", entry["slug"], path.name)
        if not max_seconds:
            build_for_city(entry, path)
            return path
        # twardy limit czasu (pojedyncze zapytanie do przeciążonego Overpass potrafi wisieć
        # dłużej niż jego timeout) — wątek w tle, plik zapisywany atomowo na końcu
        import threading
        err: List[BaseException] = []

        def work() -> None:
            try:
                build_for_city(entry, path, max_seconds=max_seconds)
            except BaseException as e:  # noqa: BLE001
                err.append(e)

        th = threading.Thread(target=work, daemon=True)
        th.start()
        th.join(max_seconds + 30)
        if th.is_alive():
            raise TimeoutError("Overpass nie odpowiedział w %ds — spróbuję przy kolejnym przebiegu (za dobę)"
                               % max_seconds)
        if err:
            raise err[0]
        return path
    except Exception as e:  # noqa: BLE001 — mapa to dodatek
        log.warning("[%s] nie udało się zbudować słownika ulic (mapa będzie uboższa): %s", entry["slug"], e)
        return None


def _log_entry(entry: dict) -> None:
    for line in describe(entry).splitlines():
        log.info("%s", line)


def describe(entry: dict) -> str:
    lines = ["%s (%s): woj. %s, pow. %s, gm. %s, %.5f, %.5f" % (
        entry.get("name"), entry.get("slug"), entry.get("voivodeship"), entry.get("county"),
        entry.get("municipality"), entry.get("lat") or 0, entry.get("lon") or 0)]
    for p in PORTALS:
        if p in (entry.get("portals") or {}):
            lines.append("  OK    %-21s %s" % (p, json.dumps(entry["portals"][p], ensure_ascii=False)))
        elif p in (entry.get("failed") or {}):
            lines.append("  BRAK  %-21s %s" % (p, entry["failed"][p]))
    for w in entry.get("warnings") or []:
        lines.append("  UWAGA %s" % w)
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Miejscowości: ustalanie lokalizacji w portalach")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="ustal lokalizację miejscowości w portalach i zapisz")
    a.add_argument("name", help='nazwa, np. "Kraków" albo siechnice')
    a.add_argument("--slug", help="identyfikator w adresie strony (domyślnie z nazwy)")
    a.add_argument("--voivodeship", help="województwo (gdy nazwa niejednoznaczna)")
    a.add_argument("--county", help="powiat (gdy nazwa niejednoznaczna), np. wrocławski")
    a.add_argument("--portals", help="tylko te portale, po przecinku")
    a.add_argument("--force", action="store_true", help="ustal od nowa, nawet gdy już zapisana")
    a.add_argument("--dry-run", action="store_true", help="tylko pokaż, nie zapisuj")
    a.add_argument("--no-geo", action="store_true", help="nie buduj słownika ulic (scraper/geo/<slug>.json)")
    s = sub.add_parser("show", help="pokaż zapisane miejscowości")
    s.add_argument("slug", nargs="?")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)

    cache = load_cache()
    if args.cmd == "show":
        slugs = [args.slug] if args.slug else sorted(cache) or [DEFAULT_CITY]
        for sl in slugs:
            e = get_city(sl, cache)
            print(describe(e) if e else "%s: brak" % sl)
        return 0

    spec = {"query": args.name, "slug": slugify(args.slug or args.name)}
    if args.voivodeship:
        spec["voivodeship"] = args.voivodeship
    if args.county:
        spec["county"] = args.county
    if spec["slug"] in cache and not args.force and not args.dry_run:
        print("%s jest już zapisana (użyj --force, żeby ustalić od nowa):" % spec["slug"])
        print(describe(cache[spec["slug"]]))
        if not args.no_geo:
            _build_geo(cache[spec["slug"]])
        return 0
    portals = [p.strip() for p in args.portals.split(",")] if args.portals else None
    entry = resolve_city(spec, portals)
    if portals and spec["slug"] in cache:  # częściowe odświeżenie: reszta portali bez zmian
        old = cache[spec["slug"]]
        entry["portals"] = dict(old.get("portals") or {}, **entry["portals"])
        entry["failed"] = {k: v for k, v in dict(old.get("failed") or {}, **entry["failed"]).items()
                           if k not in entry["portals"]}
    print(describe(entry))
    if args.dry_run:
        return 0
    cache[spec["slug"]] = entry
    save_cache(cache)
    print("Zapisano w %s" % CACHE_PATH.relative_to(ROOT))
    if not args.no_geo:
        p = _build_geo(entry)
        if p:
            print("Słownik ulic: %s" % p.relative_to(ROOT))
    if not entry["portals"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
