"""Jednorazowe zbudowanie gazetteera miejscowości (scraper/geo/<miejscowość>.json) z OpenStreetMap.

    python -m scraper.geo.build_gazetteer                 # Wrocław (scraper/geo/wroclaw.json)
    python -m scraper.geo.build_gazetteer --city krakow   # miejscowość z scraper/cities_cache.json

Dla innych miejscowości niż Wrocław granicę miasta znajdujemy po punkcie z OLX
(is_in: granica administracyjna o tej nazwie, gmina > miasto > ... — działa też dla
małych miast, np. Siechnice = gmina Siechnice z okolicznymi wsiami), a gdy jej nie ma (wieś bez granic w OSM) — bierzemy
prostokąt ~2,5 km wokół punktu. `python -m scraper.cities add` robi to samo automatycznie.

Wykonuje 3 zapytania do Overpass API (cały słownik naraz, nie per oferta) — zgodnie
z zasadami korzystania z OSM. Wynik jest commitowany do repozytorium; scraper
w czasie działania niczego nie pobiera. Dane: © OpenStreetMap contributors, ODbL.
Uruchamiać ponownie tylko, gdy trzeba odświeżyć nazwy ulic (np. raz na rok).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from scraper.geo import GAZETTEER_PATH, area_key, city_stopwords, gazetteer_path, haversine_km, street_key

ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
USER_AGENT = "mieszkania-gazetteer/1.0 (one-time build of a city street gazetteer; python-requests)"

CITY = 'area["name"="Wrocław"]["boundary"="administrative"]["admin_level"="8"]->.a;'
# okolice Wrocławia (do rozpoznawania ofert spod miasta, np. "Iwiny")
AROUND_BBOX = "50.95,16.70,51.30,17.35"

Q_AREAS = """[out:json][timeout:180];
%s
rel["name"="Wrocław"]["boundary"="administrative"]["admin_level"="8"];
out bb tags;
(
  rel(area.a)["boundary"="administrative"]["admin_level"~"^(9|10|11)$"];
  node(area.a)["place"~"^(suburb|quarter|neighbourhood|village|hamlet|square)$"];
);
out center tags;
""" % CITY

Q_OUTSIDE = """[out:json][timeout:120];
%s
(
  node(%s)["place"~"^(town|village|hamlet|suburb|neighbourhood|quarter|isolated_dwelling)$"];
  - node(area.a)["place"];
);
out tags;
""" % (CITY, AROUND_BBOX)

Q_STREETS = """[out:json][timeout:240];
%s
(
  way(area.a)["highway"]["name"]["highway"!~"^(platform|bus_stop|elevator|corridor|proposed)$"];
  way(area.a)["place"="square"]["name"];
);
out center tags;
""" % CITY

NAME_TAGS = ("name", "alt_name", "old_name", "official_name", "short_name", "loc_name")
SAMPLE_SPACING_KM = 0.4
MAX_SAMPLES = 10


_DEADLINE: Optional[float] = None  # time.time(), po którym przestajemy próbować (None = bez limitu)


def overpass(query: str) -> dict:
    last = None
    for attempt in range(3):
        for url in ENDPOINTS:
            left = (_DEADLINE - time.time()) if _DEADLINE else 300
            if left < 15:
                raise RuntimeError("Overpass: koniec czasu na budowę słownika (%s)" % last)
            try:
                r = requests.post(url, data={"data": query}, timeout=min(300, left),
                                  headers={"User-Agent": USER_AGENT})
                if r.status_code == 200 and r.text.lstrip().startswith("{"):
                    j = r.json()
                    remark = str(j.get("remark") or "")
                    # przeciążony serwer zwraca 200 z pustą listą i uwagą "runtime error: ... timed out"
                    if "error" not in remark.lower():
                        return j
                    raise requests.RequestException("odpowiedź z błędem: %s" % remark[:200])
                last = "%s: HTTP %s %s" % (url, r.status_code, r.text[:200])
            except requests.RequestException as e:
                last = "%s: %s" % (url, e)
            print("  nieudane:", last, file=sys.stderr)
            time.sleep(10)
        time.sleep(30 * (attempt + 1))
    raise RuntimeError("Overpass niedostępny: %s" % last)


def sample_points(pts: List[Tuple[float, float]]) -> List[List[float]]:
    """Pierwszy punkt = reprezentatywny (najbliżej średniej), potem do MAX_SAMPLES
    punktów rozłożonych wzdłuż ulicy (farthest-point sampling)."""
    mlat = sum(p[0] for p in pts) / len(pts)
    mlon = sum(p[1] for p in pts) / len(pts)
    rep = min(pts, key=lambda p: (p[0] - mlat) ** 2 + (p[1] - mlon) ** 2)
    chosen = [rep]
    dist = [haversine_km(p, rep) for p in pts]
    while len(chosen) < MAX_SAMPLES:
        i = max(range(len(pts)), key=lambda j: dist[j])
        if dist[i] < SAMPLE_SPACING_KM:
            break
        chosen.append(pts[i])
        dist = [min(d, haversine_km(p, pts[i])) for d, p in zip(dist, pts)]
    return [[round(p[0], 6), round(p[1], 6)] for p in chosen]


def center_of(el: dict):
    if "lat" in el:
        return el["lat"], el["lon"]
    c = el.get("center")
    if c:
        return c["lat"], c["lon"]
    return None


def build(out_path: Path) -> dict:
    """Wrocław — dokładnie te same zapytania co od początku."""
    return _build(out_path, Q_AREAS, Q_STREETS, Q_OUTSIDE, about_city="Wrocławia")


def _city_queries(rel_id: Optional[int], bbox: Tuple[float, float, float, float],
                  around: str) -> Tuple[str, str, str]:
    """Zapytania jak dla Wrocławia, ale dla granicy o danym id relacji
    (albo prostokąta bbox, gdy granicy brak)."""
    if rel_id:
        sel = "area(id:%d)->.a;" % (3600000000 + rel_id)
        flt = "(area.a)"
        rel = "rel(%d);\nout bb tags;" % rel_id
    else:
        sel = ""
        flt = "(%.5f,%.5f,%.5f,%.5f)" % bbox
        rel = ""
    q_areas = """[out:json][timeout:180];
%s
%s
(
  rel%s["boundary"="administrative"]["admin_level"~"^(9|10|11)$"];
  node%s["place"~"^(suburb|quarter|neighbourhood|village|hamlet|square)$"];
);
out center tags;
""" % (sel, rel, flt, flt)
    q_outside = """[out:json][timeout:120];
%s
(
  node(%s)["place"~"^(town|village|hamlet|suburb|neighbourhood|quarter|isolated_dwelling)$"];
  - node%s["place"];
);
out tags;
""" % (sel, around, flt)
    q_streets = """[out:json][timeout:240];
%s
(
  way%s["highway"]["name"]["highway"!~"^(platform|bus_stop|elevator|corridor|proposed)$"];
  way%s["place"="square"]["name"];
);
out center tags;
""" % (sel, flt, flt)
    return q_areas, q_streets, q_outside


def find_boundary(name: str, lat: float, lon: float) -> Optional[Tuple[int, str]]:
    """(id relacji, admin_level) granicy administracyjnej o tej nazwie zawierającej punkt."""
    q = """[out:json][timeout:60];
is_in(%.6f,%.6f)->.x;
area.x["boundary"="administrative"]["admin_level"~"^(6|7|8|9)$"];
out tags;
""" % (lat, lon)
    res = overpass(q)
    want = area_key(name)
    best = None
    for el in res.get("elements") or []:
        t = el.get("tags") or {}
        if area_key(t.get("name")) != want or not str(el.get("id", "")).startswith("36"):
            continue
        # gmina (7) przed miastem (8): portale przy małych miastach podają też oferty
        # z wsi tej gminy (np. "Siechnice" obejmuje Radwanice i Świętą Katarzynę)
        rank = {"7": 0, "8": 1, "9": 2, "6": 3}.get(t.get("admin_level"), 9)
        if best is None or rank < best[0]:
            best = (rank, int(el["id"]) - 3600000000, t.get("admin_level"))
    return (best[1], best[2]) if best else None


def build_for_city(entry: dict, out_path: Optional[Path] = None,
                   max_seconds: Optional[float] = None) -> dict:
    """Gazetteer dowolnej miejscowości z cities_cache.json (wpis z nazwą i lat/lon).
    max_seconds — limit czasu (Overpass bywa przeciążony; w przebiegu co godzinę nie czekamy w nieskończoność)."""
    global _DEADLINE
    _DEADLINE = time.time() + max_seconds if max_seconds else None
    try:
        return _build_for_city(entry, out_path)
    finally:
        _DEADLINE = None


def _build_for_city(entry: dict, out_path: Optional[Path] = None) -> dict:
    slug, name = entry["slug"], entry["name"]
    if slug == "wroclaw":
        return build(out_path or GAZETTEER_PATH)
    lat, lon = float(entry["lat"]), float(entry["lon"])
    out_path = out_path or gazetteer_path(slug)
    print("Overpass: granica %s…" % name, file=sys.stderr)
    found = find_boundary(name, lat, lon)
    time.sleep(5)
    # prostokąt zastępczy ~2,5 km wokół punktu (gdy granicy brak) i okolice do "outside"
    dlat, dlon = 0.0225, 0.0225 / max(0.2, math.cos(math.radians(lat)))
    bbox = (lat - dlat, lon - dlon, lat + dlat, lon + dlon)
    rel_id = found[0] if found else None
    if found:
        print("  granica: relacja %d (admin_level %s)" % found, file=sys.stderr)
    else:
        print("  brak granicy o nazwie %s — używam prostokąta wokół punktu" % name, file=sys.stderr)
    # okolice: zakres granicy znamy dopiero po pierwszym zapytaniu, więc bierzemy
    # hojny prostokąt wokół punktu (duże miasta mają ~15-20 km promienia)
    m_lat, m_lon = 0.25, 0.25 / max(0.2, math.cos(math.radians(lat)))
    around = "%.4f,%.4f,%.4f,%.4f" % (lat - m_lat, lon - m_lon, lat + m_lat, lon + m_lon)
    qa, qs, qo = _city_queries(rel_id, bbox, around)
    return _build(out_path, qa, qs, qo, about_city=name, city=slug, city_name=name,
                  fallback_bbox=None if rel_id else list(bbox),
                  extra_stop=city_stopwords(name))


def _build(out_path: Path, q_areas: str, q_streets: str, q_outside: str, about_city: str,
           city: Optional[str] = None, city_name: Optional[str] = None,
           fallback_bbox: Optional[List[float]] = None, extra_stop=()) -> dict:
    print("Overpass: osiedla i granice…", file=sys.stderr)
    a = overpass(q_areas)
    time.sleep(5)
    print("Overpass: ulice…", file=sys.stderr)
    s = overpass(q_streets)
    time.sleep(5)
    print("Overpass: miejscowości wokół…", file=sys.stderr)
    o = overpass(q_outside)

    bbox = fallback_bbox
    areas: Dict[str, dict] = {}
    rank_of: Dict[str, int] = {}
    outside = set()
    # priorytet: węzeł place (środek „społeczny”) > granica osiedla (środek bbox)
    RANK = {"suburb": 0, "quarter": 0, "neighbourhood": 1, "9": 2, "10": 3, "11": 3,
            "village": 4, "hamlet": 4, "square": 5}
    for el in a["elements"]:
        t = el.get("tags") or {}
        if el["type"] == "relation" and (t.get("admin_level") == "8" if city is None
                                         else t.get("admin_level") not in ("9", "10", "11")):
            b = el.get("bounds")
            if b:
                bbox = [b["minlat"], b["minlon"], b["maxlat"], b["maxlon"]]
            continue
        pt = center_of(el)
        name = t.get("name")
        if not name or not pt:
            continue
        kind = t.get("admin_level") or t.get("place")
        for nm in {t.get(k) for k in NAME_TAGS if t.get(k)}:
            k = area_key(nm, extra_stop)
            if not k:
                continue
            r = RANK.get(kind, 9)
            if k not in areas or r < rank_of[k]:
                areas[k] = {"name": name, "lat": round(pt[0], 6), "lon": round(pt[1], 6)}
                rank_of[k] = r

    for el in o["elements"]:
        k = area_key((el.get("tags") or {}).get("name"), extra_stop)
        if k and k not in areas:  # nazwy osiedli miasta mają pierwszeństwo
            outside.add(k)

    by_name: Dict[str, List[Tuple[float, float]]] = {}
    display: Dict[str, str] = {}
    for el in s["elements"]:
        t = el.get("tags") or {}
        pt = center_of(el)
        if not pt:
            continue
        for nm in {t.get(k) for k in NAME_TAGS if t.get(k)}:
            for single in nm.split(";"):
                k = street_key(single)
                if not k or k.isdigit():
                    continue
                by_name.setdefault(k, []).append(pt)
                display.setdefault(k, t.get("name") or single)

    streets = {k: {"name": display[k], "pts": sample_points(v)} for k, v in sorted(by_name.items())}
    data = {
        "_about": "Gazetteer %s do przybliżonego geokodowania ofert. "
                  "Dane © OpenStreetMap contributors (ODbL). Generator: scraper/geo/build_gazetteer.py" % about_city,
    }
    if city:
        data.update(city=city, name=city_name)
    data.update({
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bbox": bbox,
        "areas": dict(sorted(areas.items())),
        "outside": sorted(outside),
        "streets": streets,
    })
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(out_path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write("{\n")
        items = list(data.items())
        for i, (k, v) in enumerate(items):
            f.write(json.dumps(k, ensure_ascii=False) + ":")
            if isinstance(v, dict) and v and k in ("areas", "streets"):
                f.write("{\n")
                rows = list(v.items())
                f.write(",\n".join(json.dumps(rk, ensure_ascii=False) + ":" +
                                   json.dumps(rv, ensure_ascii=False, separators=(",", ":"))
                                   for rk, rv in rows))
                f.write("\n}")
            else:
                f.write(json.dumps(v, ensure_ascii=False, separators=(",", ":")))
            f.write(",\n" if i < len(items) - 1 else "\n")
        f.write("}\n")
    tmp_path.replace(out_path)
    print("Zapisano %s: %d ulic, %d osiedli/miejsc, %d miejscowości wokół, bbox=%s"
          % (out_path, len(streets), len(areas), len(outside), bbox), file=sys.stderr)
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--city", default="wroclaw", help="slug miejscowości z scraper/cities_cache.json")
    args = ap.parse_args()
    if args.city == "wroclaw":
        build(Path(args.out or GAZETTEER_PATH))
        return 0
    from scraper.cities import get_city, slugify
    entry = get_city(slugify(args.city))
    if not entry:
        print("Brak %s w scraper/cities_cache.json — najpierw: python -m scraper.cities add %s"
              % (args.city, args.city), file=sys.stderr)
        return 1
    build_for_city(entry, Path(args.out) if args.out else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
