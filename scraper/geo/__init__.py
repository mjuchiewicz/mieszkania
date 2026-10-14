"""Przybliżone geokodowanie ofert offline, na podstawie statycznego gazetteera Wrocławia.

Większość portali w wynikach wyszukiwania nie podaje współrzędnych — tylko ulicę
i osiedle / dzielnicę. Ten moduł dopasowuje te napisy do słownika
scraper/geo/wroclaw.json (zbudowanego raz skryptem build_gazetteer.py z danych
OpenStreetMap) i uzupełnia lat/lon wraz z polem `geo` = dokładność:

    "dokładna"  – współrzędne z portalu,
    "ulica"     – punkt na ulicy (przy znanym osiedlu: fragment ulicy najbliżej osiedla),
    "osiedle"   – środek osiedla + stały, mały rozrzut (żeby pinezki się nie nakładały),
    "dzielnica" – środek jednej z 5 dawnych dzielnic (Krzyki, Fabryczna…) + większy rozrzut.

Nic nie jest pobierane z sieci w czasie działania scrapera.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import math
import re
import unicodedata
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

GAZETTEER_PATH = Path(__file__).resolve().parent / "wroclaw.json"

GEO_EXACT = "dokładna"
GEO_STREET = "ulica"
GEO_AREA = "osiedle"
GEO_DISTRICT = "dzielnica"
APPROX = (GEO_STREET, GEO_AREA, GEO_DISTRICT)

# Granice Wrocławia (gdy gazetteer nie ma własnych) — S, W, N, E
DEFAULT_BBOX = (51.0426, 16.8073, 51.2100, 17.1762)
BBOX_MARGIN = 0.01  # ~1 km zapasu

# Dawne dzielnice (do 1990 r.) — portale wciąż ich używają zamiast osiedli.
# Punkty to przybliżone środki zabudowy mieszkaniowej każdej dzielnicy.
BIG_DISTRICTS = {
    "stare miasto": ("Stare Miasto", 51.1075, 17.0250),
    "srodmiescie": ("Śródmieście", 51.1200, 17.0560),
    "krzyki": ("Krzyki", 51.0780, 17.0300),
    "fabryczna": ("Fabryczna", 51.1150, 16.9600),
    "psie pole": ("Psie Pole", 51.1470, 17.0700),
}

# Promień rozrzutu (m) punktów przybliżonych: (min, max)
JITTER_M = {GEO_AREA: (150.0, 300.0), GEO_DISTRICT: (300.0, 1200.0)}

# Jak daleko (km) od środka osiedla / dzielnicy może leżeć dopasowana ulica.
# Dawne dzielnice są ogromne (Fabryczna sięga od Szczepina po Leśnicę), więc przy nich
# ufamy ulicy bez limitu odległości.
MAX_STREET_DIST_KM = {GEO_AREA: 3.5, GEO_DISTRICT: float("inf")}
# Ulica bez znanego osiedla: gdy kandydaci (ulice o tej samej nazwie) leżą dalej
# od siebie niż tyle km — nie zgadujemy.
MAX_AMBIGUOUS_SPREAD_KM = 1.5

# Słowa pomijane w nazwach ulic: typy ulic, tytuły, skróty, śmieci z ogłoszeń.
STREET_STOPWORDS = set("""
ul ulica ulicy al aleja aleje alei pl plac placu os osiedle osiedla skwer rondo
gen generala sw swietego swietej swietych bl blogoslawionego blogoslawionej
ks ksiedza bp biskupa abp arcybiskupa kard kardynala marsz marszalka prof profesora
dr doktora inz o ojca pplk plk pulkownika ppor por porucznika kpt kapitana mjr majora
kr krola krolowej hetm hetmana im imienia nr wroclaw wroclawiu okolice okolica okolicy
w we ok rejon rejonie przy blisko obok centrum dolnoslaskie
""".split())

AREA_STOPWORDS = set("wroclaw wroclawiu dolnoslaskie dolnoslaski osiedle os dzielnica gmina powiat".split())

# Literówki / warianty spotykane na portalach → nazwa w gazetteerze (po normalizacji)
AREA_ALIASES = {
    "krzyk": "krzyki",
    "krzki": "krzyki",
    "gadow maly": "gadow",
    "rynek": "stare miasto",
    "zawidawie": "psie pole zawidawie",
}

_HOUSE_NO_RE = re.compile(r"(?<=[^\W\d_])\s+(nr\.?\s*)?\d+\s*[a-zA-Z]?(\s*[/-]\s*\d+\s*[a-zA-Z]?)?\b.*$", re.U)


def norm_text(s: Optional[str]) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s).lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("ł", "l")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def street_tokens(s: Optional[str]) -> List[str]:
    """'ul. gen. Józefa Bema 12/4' -> ['jozefa', 'bema']."""
    if not s:
        return []
    s = _HOUSE_NO_RE.sub("", str(s).strip())  # numer domu i wszystko po nim
    toks = [t for t in norm_text(s).split() if t not in STREET_STOPWORDS]
    # pojedyncze litery (inicjały: "J. Bema") i samotne liczby na końcu
    toks = [t for t in toks if len(t) > 1 or t.isdigit()]
    while toks and toks[-1].isdigit() and len(toks) > 1:
        toks.pop()
    return toks


def _nominative(t: str) -> str:
    """Dopełniacz → mianownik dla nazw przymiotnikowych: 'swieradowskiej' -> 'swieradowska'."""
    if len(t) > 5 and t.endswith(("kiej", "giej")):
        return t[:-3] + "a"
    if len(t) > 4 and t.endswith("ej"):
        return t[:-2] + "a"
    return t


def street_key(s: Optional[str]) -> str:
    return " ".join(street_tokens(s))


def street_parts(s: Optional[str]) -> List[str]:
    """Napis z portalu może mieć kilka ulic lub śmieci: 'Powstańców Śląskich, Zaporoska',
    'Okrętowa okolice/3 pokoje'. Zwraca kolejne kandydujące fragmenty."""
    if not s:
        return []
    parts = re.split(r"[,;/|()\[\]]|\s+[-–]\s+|\s+(?:i|oraz|róg|rog)\s+", str(s))
    return [p.strip() for p in parts if p and p.strip()]


def area_key(s: Optional[str]) -> str:
    toks = [t for t in norm_text(s).split() if t not in AREA_STOPWORDS]
    return " ".join(toks)


def haversine_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def jitter(key: str, lat: float, lon: float, rmin: float, rmax: float) -> Tuple[float, float]:
    """Stały (zależny tylko od klucza oferty) rozrzut punktu o rmin..rmax metrów."""
    h = hashlib.sha1(key.encode("utf-8")).digest()
    u = int.from_bytes(h[:4], "big") / 2.0 ** 32
    v = int.from_bytes(h[4:8], "big") / 2.0 ** 32
    ang = 2 * math.pi * u
    r = math.sqrt(rmin ** 2 + v * (rmax ** 2 - rmin ** 2))  # równomiernie w pierścieniu
    dlat = r * math.cos(ang) / 111320.0
    dlon = r * math.sin(ang) / (111320.0 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


class Gazetteer:
    def __init__(self, data: dict):
        self.bbox = tuple(data.get("bbox") or DEFAULT_BBOX)
        self.streets: Dict[str, List[Tuple[float, float]]] = {
            k: [tuple(p) for p in v["pts"]] for k, v in (data.get("streets") or {}).items()
        }
        self.areas: Dict[str, Tuple[float, float]] = {
            k: (v["lat"], v["lon"]) for k, v in (data.get("areas") or {}).items()
        }
        self.outside = set(data.get("outside") or ())
        # indeksy pomocnicze ulic
        self.by_set: Dict[str, List[str]] = {}
        self.by_last: Dict[str, List[str]] = {}
        for k in self.streets:
            toks = k.split()
            self.by_set.setdefault(" ".join(sorted(toks)), []).append(k)
            if toks:
                self.by_last.setdefault(toks[-1], []).append(k)
        self._keys = sorted(self.streets)
        self._lasts = sorted(self.by_last)
        # części nazw złożonych osiedli: "psie pole zawidawie" -> też "zawidawie"
        self.area_parts: Dict[str, str] = {}
        for k in self.areas:
            toks = k.split()
            if len(toks) < 2:
                continue
            for i in range(len(toks)):
                for j in range(i + 1, len(toks) + 1):
                    sub = " ".join(toks[i:j])
                    if sub != k and sub not in self.areas and len(sub) > 3:
                        self.area_parts.setdefault(sub, k)

    @classmethod
    def load(cls, path: Path = GAZETTEER_PATH) -> "Gazetteer":
        with open(path, encoding="utf-8") as f:
            return cls(json.load(f))

    # ------------------------------------------------------------ lookups

    def in_bbox(self, lat: float, lon: float) -> bool:
        s, w, n, e = self.bbox
        m = BBOX_MARGIN
        return (s - m) <= lat <= (n + m) and (w - m) <= lon <= (e + m)

    def area(self, name: Optional[str]) -> Tuple[Optional[str], Optional[Tuple[float, float]]]:
        """Zwraca (dokładność, punkt) dla nazwy osiedla / dzielnicy.
        ("poza", None) gdy to miejscowość pod Wrocławiem, (None, None) gdy nieznane."""
        k = area_key(name)
        if not k:
            return None, None
        k = AREA_ALIASES.get(k, k)
        if k in BIG_DISTRICTS:
            _, lat, lon = BIG_DISTRICTS[k]
            return GEO_DISTRICT, (lat, lon)
        if k in self.areas:
            return GEO_AREA, self.areas[k]
        if k in self.area_parts:
            return GEO_AREA, self.areas[self.area_parts[k]]
        # "Krzyki-Partynice" itp. gdy pełnej nazwy nie ma: pierwszy znany człon
        toks = k.split()
        for t in toks:
            t = AREA_ALIASES.get(t, t)
            if t in self.areas:
                return GEO_AREA, self.areas[t]
        if k in self.outside:
            return "poza", None
        for t in toks:
            if t in BIG_DISTRICTS:
                _, lat, lon = BIG_DISTRICTS[t]
                return GEO_DISTRICT, (lat, lon)
        return None, None

    def street_candidates(self, s: str) -> List[str]:
        toks = street_tokens(s)
        if not toks:
            return []
        k = " ".join(toks)
        if k in self.streets:
            return [k]
        ks = " ".join(sorted(toks))
        if ks in self.by_set:
            return self.by_set[ks]
        # po nazwisku / ostatnim słowie, z kontrolą pozostałych słów (mogą być skrótami)
        out = []
        for last in (toks[-1], toks[0]):
            for cand in self.by_last.get(last, ()):
                ctoks = cand.split()
                if all(any(ct.startswith(t) for ct in ctoks) for t in toks):
                    out.append(cand)
            if out:
                return out
        # "ok. ul. Świeradowskiej" — nazwa w dopełniaczu
        nom = [_nominative(t) for t in toks]
        if nom != toks:
            k2 = " ".join(nom)
            if k2 in self.streets:
                return [k2]
            for cand in self.by_last.get(nom[-1], ()):
                if all(any(ct.startswith(t) for ct in cand.split()) for t in nom):
                    out.append(cand)
            if out:
                return out
        # literówki / warianty pisowni ("Ernesta Maya" -> "Ernsta Maya")
        if len(k) >= 6:
            out = difflib.get_close_matches(k, self._keys, n=1, cutoff=0.88)
            if not out:
                # samo nazwisko: "Ernesta Maya" vs "maya" w kluczu "ernsta maya"
                lasts = difflib.get_close_matches(toks[-1], self._lasts, n=1, cutoff=0.9) if len(toks[-1]) >= 6 else []
                out = list(self.by_last.get(lasts[0], ())) if lasts else []
        return out

    def street_point(self, street: Optional[str],
                     near: Optional[Tuple[float, float]],
                     max_km: float) -> Optional[Tuple[float, float]]:
        parts = street_parts(street)
        if near is not None:
            # "Krawiecka Justin Center", "Mennicza Wrocław centrum GOTOWIEC" — śmieci po nazwie;
            # krótsze początki sprawdzamy tylko przy znanym osiedlu (kontrola odległości)
            for p in list(parts):
                toks = p.split()
                parts += [" ".join(toks[:n]) for n in range(min(len(toks) - 1, 3), 0, -1)]
        for part in parts:
            cands = self.street_candidates(part)
            if not cands:
                continue
            pts = [p for c in cands for p in self.streets[c]]
            if near is not None:
                best = min(pts, key=lambda p: haversine_km(p, near))
                if haversine_km(best, near) <= max_km:
                    return best
                continue  # ta ulica jest gdzie indziej niż podane osiedle
            reps = [self.streets[c][0] for c in cands]
            if len(reps) > 1 and max(haversine_km(reps[0], r) for r in reps[1:]) > MAX_AMBIGUOUS_SPREAD_KM:
                continue  # kilka różnych ulic o tej nazwie — nie wiadomo która
            return reps[0]
        return None

    def locate(self, rec: dict) -> Tuple[Optional[str], Optional[Tuple[float, float]]]:
        """(dokładność, (lat, lon)) dla oferty bez współrzędnych z portalu."""
        prec, center = self.area(rec.get("district"))
        if prec == "poza":
            return None, None  # oferta spod Wrocławia — nazwy ulic by nas zmyliły
        if rec.get("street"):
            p = self.street_point(rec.get("street"), center,
                                  MAX_STREET_DIST_KM.get(prec or GEO_AREA, 2.5))
            if p is not None:
                return GEO_STREET, p
        if center is not None:
            rmin, rmax = JITTER_M[prec]
            return prec, jitter(rec.get("key") or "", center[0], center[1], rmin, rmax)
        return None, None


_GAZ: Optional[Gazetteer] = None


def get_gazetteer() -> Optional[Gazetteer]:
    global _GAZ
    if _GAZ is None and GAZETTEER_PATH.exists():
        _GAZ = Gazetteer.load()
    return _GAZ


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def geocode_listings(listings: Iterable[dict], gaz: Optional[Gazetteer] = None) -> Dict[str, int]:
    """Uzupełnia lat/lon/geo w miejscu. Prawdziwych współrzędnych z portalu nie nadpisuje.
    Zwraca statystykę {dokładność: liczba ofert} (klucz "brak" = bez lokalizacji)."""
    gaz = gaz or get_gazetteer()
    stats: Dict[str, int] = {}
    for rec in listings:
        lat, lon, g = _num(rec.get("lat")), _num(rec.get("lon")), rec.get("geo")
        if g in APPROX:
            lat = lon = None  # nasze własne przybliżenie z poprzedniego przebiegu — liczymy od nowa
        if lat is not None and lon is not None:
            in_box = gaz.in_bbox(lat, lon) if gaz else (
                DEFAULT_BBOX[0] - BBOX_MARGIN <= lat <= DEFAULT_BBOX[2] + BBOX_MARGIN
                and DEFAULT_BBOX[1] - BBOX_MARGIN <= lon <= DEFAULT_BBOX[3] + BBOX_MARGIN)
            if in_box:
                rec["lat"], rec["lon"], rec["geo"] = lat, lon, GEO_EXACT
                stats[GEO_EXACT] = stats.get(GEO_EXACT, 0) + 1
                continue
            lat = lon = None  # poza Wrocławiem / błędne
        rec["lat"] = rec["lon"] = None
        rec.pop("geo", None)
        prec, pt = gaz.locate(rec) if gaz else (None, None)
        if pt is not None:
            rec["lat"], rec["lon"], rec["geo"] = round(pt[0], 6), round(pt[1], 6), prec
        stats[prec or "brak"] = stats.get(prec or "brak", 0) + 1
    return stats
