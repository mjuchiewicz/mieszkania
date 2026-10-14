"""Jednorazowe zbudowanie gazetteera Wrocławia (scraper/geo/wroclaw.json) z OpenStreetMap.

    python -m scraper.geo.build_gazetteer

Wykonuje 3 zapytania do Overpass API (cały słownik naraz, nie per oferta) — zgodnie
z zasadami korzystania z OSM. Wynik jest commitowany do repozytorium; scraper
w czasie działania niczego nie pobiera. Dane: © OpenStreetMap contributors, ODbL.
Uruchamiać ponownie tylko, gdy trzeba odświeżyć nazwy ulic (np. raz na rok).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

import requests

from scraper.geo import GAZETTEER_PATH, area_key, haversine_km, street_key

ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
USER_AGENT = "flat-scrapper-gazetteer/1.0 (one-time build of Wroclaw street gazetteer; python-requests)"

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


def overpass(query: str) -> dict:
    last = None
    for attempt in range(3):
        for url in ENDPOINTS:
            try:
                r = requests.post(url, data={"data": query}, timeout=300,
                                  headers={"User-Agent": USER_AGENT})
                if r.status_code == 200 and r.text.lstrip().startswith("{"):
                    return r.json()
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
    print("Overpass: osiedla i granice…", file=sys.stderr)
    a = overpass(Q_AREAS)
    time.sleep(5)
    print("Overpass: ulice…", file=sys.stderr)
    s = overpass(Q_STREETS)
    time.sleep(5)
    print("Overpass: miejscowości wokół Wrocławia…", file=sys.stderr)
    o = overpass(Q_OUTSIDE)

    bbox = None
    areas: Dict[str, dict] = {}
    rank_of: Dict[str, int] = {}
    outside = set()
    # priorytet: węzeł place (środek „społeczny”) > granica osiedla (środek bbox)
    RANK = {"suburb": 0, "quarter": 0, "neighbourhood": 1, "9": 2, "10": 3, "11": 3,
            "village": 4, "hamlet": 4, "square": 5}
    for el in a["elements"]:
        t = el.get("tags") or {}
        if el["type"] == "relation" and t.get("admin_level") == "8":
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
            k = area_key(nm)
            if not k:
                continue
            r = RANK.get(kind, 9)
            if k not in areas or r < rank_of[k]:
                areas[k] = {"name": name, "lat": round(pt[0], 6), "lon": round(pt[1], 6)}
                rank_of[k] = r

    for el in o["elements"]:
        k = area_key((el.get("tags") or {}).get("name"))
        if k and k not in areas:  # nazwy osiedli Wrocławia mają pierwszeństwo
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
        "_about": "Gazetteer Wrocławia do przybliżonego geokodowania ofert. "
                  "Dane © OpenStreetMap contributors (ODbL). Generator: scraper/geo/build_gazetteer.py",
        "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bbox": bbox,
        "areas": dict(sorted(areas.items())),
        "outside": sorted(outside),
        "streets": streets,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
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
    print("Zapisano %s: %d ulic, %d osiedli/miejsc, %d miejscowości wokół, bbox=%s"
          % (out_path, len(streets), len(areas), len(outside), bbox), file=sys.stderr)
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--out", default=str(GAZETTEER_PATH))
    args = ap.parse_args()
    build(Path(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
