"""Powiadomienia e-mail o nowych ofertach: `python -m scraper.alerts`.

Uruchamiane w GitHub Actions zaraz po `python -m scraper.main`. Dla każdego alertu
(sekret repozytorium ALERT_<NAZWA>, wartość = „kod alertu” ze strony) wybiera oferty,
które pojawiły się w TYM przebiegu pobierania i pasują do filtrów alertu, i wysyła
jeden e-mail (Gmail SMTP). Gdy nic nowego — nic nie wysyła.

Repozytorium jest publiczne, a logi Actions też. Dlatego:
  * definicje alertów i adresy e-mail są tylko w sekretach (nigdy w plikach repo),
  * w logach są wyłącznie liczby („alert 2: 3 nowe oferty, wysłano”),
  * plik stanu docs/data/alerts_state.json zawiera tylko skróty HMAC i daty.

Kod alertu = base64url(JSON), JSON w wersji 1:
  {"v":1, "name":"Krzyki", "emails":["a@b.pl"], "deal":"wynajem", "city":"wroclaw",
   "filters":{"price_max":3500, "districts":["Krzyki"], "features":["balkon"], ...},
   "area":{"lat":51.08, "lon":17.03, "radius_m":1500}, "price_drops":true}
Brak "emails" = wysyłka na adres GMAIL_USER.

Filtry działają DOKŁADNIE tak jak na stronie (docs/app.js, funkcja applyFilters).

Test lokalny (bez wysyłania):
  .venv/bin/python -m scraper.alerts --dry-run --alerts-file alerty.txt --since 2026-10-14T00:00:00Z
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import json
import logging
import math
import os
import re
import smtplib
import ssl
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import quote, urlencode

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "docs" / "data"
STATE_PATH = DATA_DIR / "alerts_state.json"

DEFAULT_CITY = "wroclaw"
CITY_NAMES = {"wroclaw": "Wrocław"}
SITE_URL = "https://mjuchiewicz.github.io/mieszkania/"
REPO = "mjuchiewicz/mieszkania"
SECRET_PREFIX = "ALERT_"
ALERT_VERSION = 1

MAX_PER_EMAIL = 25          # tyle ofert pokazujemy w jednym mailu, reszta = link do strony
MAX_LISTING_AGE_DAYS = 7    # oferta „nowa” dla nas, ale opublikowana dawno temu = nie nowość
BULK_MIN = 150              # portal nagle „znalazł” tyle nowych ofert naraz…
BULK_FRACTION = 0.15        # …i to więcej niż taki ułamek jego ofert = doładowanie, nie nowości
STALE_RUN_HOURS = 6         # dane starsze niż tyle godzin = nie było świeżego pobierania
STATE_KEEP_DAYS = 60
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465

DEALS = ("sprzedaz", "wynajem", "pokoj")
MONTHLY = ("wynajem", "pokoj")

# --- grupy wartości — identyczne jak w docs/app.js (COND_OF, BT_OF, FEAT_OF)
COND_OF = {"do_zamieszkania": "zam", "stan_deweloperski": "dew", "do_wykonczenia": "dew", "do_remontu": "rem"}
CONDS = ("zam", "dew", "rem")
BT_OF = {"blok": "blok", "kamienica": "kamienica", "apartamentowiec": "apartamentowiec", "dom": "dom",
         "szeregowiec": "dom", "loft": "loft"}
BTYPES = ("blok", "kamienica", "apartamentowiec", "dom", "loft")
FEAT_OF = {"balkon": "balkon", "taras": "balkon", "loggia": "balkon", "ogrodek": "ogrodek", "winda": "winda",
           "parking": "parking", "garaz": "parking", "piwnica": "piwnica", "komorka": "piwnica",
           "klimatyzacja": "klima", "zwierzeta": "zwierzeta"}
FEATS = ("balkon", "ogrodek", "winda", "parking", "piwnica", "klima", "zwierzeta")
RENT_ONLY_FEATS = ("zwierzeta",)
ROOM_KEYS = ("1", "2", "3", "4", "5+")
GEO_APPROX = {"osiedle": "osiedle", "dzielnica": "dzielnica", "ulica": "wg ulicy"}

FEAT_LABEL = {"balkon": "balkon / taras", "ogrodek": "ogródek", "winda": "winda", "parking": "parking / garaż",
              "piwnica": "piwnica / komórka", "klima": "klimatyzacja", "zwierzeta": "zwierzęta OK"}
COND_LABEL = {"zam": "do zamieszkania", "dew": "stan deweloperski", "rem": "do remontu"}
SOURCE_NAMES = {"otodom": "Otodom", "olx": "OLX", "gratka": "Gratka", "morizon": "Morizon",
                "nieruchomosci_online": "Nieruchomosci-online", "domiporta": "Domiporta"}
DEAL_WHAT = {"sprzedaz": "na sprzedaż", "wynajem": "na wynajem", "pokoj": "do wynajęcia"}

EMAIL_RE = re.compile(r"^[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+$")

log = logging.getLogger("alerts")


# ================================================================ kod alertu

def encode_alert(alert: dict) -> str:
    raw = json.dumps(alert, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_alert(code: str) -> dict:
    """Kod (base64url JSON, dopuszczalne spacje/nowe linie i zwykły JSON) -> znormalizowany alert.
    Rzuca ValueError z opisem po polsku (bez treści alertu)."""
    s = re.sub(r"\s+", "", str(code or ""))
    if not s:
        raise ValueError("pusty kod alertu")
    if s.startswith("{"):
        data = json.loads(s)
    else:
        s = s.replace("+", "-").replace("/", "_").rstrip("=")
        try:
            raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValueError("kod alertu jest uszkodzony (skopiuj go jeszcze raz w całości)")
    return normalize_alert(data)


def _num(v: Any) -> Optional[float]:
    """Jak num() w app.js: '700 000' -> 700000, '3,5' -> 3.5, '' -> None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    t = re.sub(r"\s", "", str(v)).replace(",", ".", 1)
    t = re.sub(r"[^0-9.]", "", t)
    m = re.match(r"\d*\.?\d+|\d+", t)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def js_round(x: float) -> int:
    return int(math.floor(x + 0.5))


def _floor_input(v: Any) -> Optional[int]:
    """Jak floorNum() w app.js: liczba albo 'parter' (= 0)."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return js_round(v)
    s = str(v)
    if not s.strip():
        return None
    if re.match(r"^\s*p", s, re.I):
        return 0
    n = _num(s)
    return None if n is None else js_round(n)


def _str_list(v: Any) -> List[str]:
    if v is None:
        return []
    if isinstance(v, (str, int, float)):
        v = [v]
    out: List[str] = []
    for x in v if isinstance(v, (list, tuple)) else []:
        if isinstance(x, bool) or x is None:
            continue
        s = str(int(x)) if isinstance(x, float) and x.is_integer() else str(x).strip()
        if s and s not in out:
            out.append(s)
    return out


def _grouped(values: Iterable[str], allowed: Tuple[str, ...], mapping: Dict[str, str]) -> List[str]:
    out: List[str] = []
    for v in values:
        g = v if v in allowed else mapping.get(v)
        if g and g not in out:
            out.append(g)
    return out


def _bool_or_none(v: Any) -> Optional[bool]:
    if isinstance(v, bool):
        return v
    if v in ("tak", "1", 1, "true"):
        return True
    if v in ("nie", "0", 0, "false"):
        return False
    return None


def normalize_alert(data: Any) -> dict:
    if not isinstance(data, dict):
        raise ValueError("kod alertu nie zawiera alertu")
    v = data.get("v", 1)
    if not isinstance(v, int) or v < 1:
        raise ValueError("nieznana wersja alertu")
    if v > ALERT_VERSION:
        raise ValueError("alert z nowszej wersji strony — zaktualizuj skrypt")
    deal = data.get("deal") or "sprzedaz"
    if deal not in DEALS:
        raise ValueError("nieznany rodzaj ofert w alercie")
    city = str(data.get("city") or DEFAULT_CITY).strip().lower()
    if not re.match(r"^[a-z0-9_-]{1,40}$", city):
        raise ValueError("nieprawidłowe miasto w alercie")
    emails = [e for e in _str_list(data.get("emails")) if EMAIL_RE.match(e)]
    f = data.get("filters") or {}
    if not isinstance(f, dict):
        f = {}
    filters: Dict[str, Any] = {}
    for k in ("price_min", "price_max", "area_min", "area_max", "ppm_max"):
        n = _num(f.get(k))
        if n is not None:
            filters[k] = int(n) if n.is_integer() else n
    for k in ("floor_min", "floor_max"):
        n = _floor_input(f.get(k))
        if n is not None:
            filters[k] = n
    rooms = [r for r in ("5+" if r in ("5", "5+") or (r.isdigit() and int(r) >= 5) else r
                         for r in _str_list(f.get("rooms"))) if r in ROOM_KEYS]
    if rooms:
        filters["rooms"] = list(dict.fromkeys(rooms))
    for k in ("districts", "market", "seller", "sources"):
        lst = _str_list(f.get(k))
        if lst:
            filters[k] = lst
    conds = _grouped(_str_list(f.get("conditions")), CONDS, COND_OF)
    if conds:
        filters["conditions"] = conds
    bts = _grouped(_str_list(f.get("building_types")), BTYPES, BT_OF)
    if bts:
        filters["building_types"] = bts
    feats = _grouped(_str_list(f.get("features")), FEATS, dict(FEAT_OF, klima="klima"))
    if feats:
        filters["features"] = feats
    furn = _bool_or_none(f.get("furnished"))
    if furn is not None:
        filters["furnished"] = furn
    for k in ("no_ground", "no_top"):
        if f.get(k) is True or f.get(k) in (1, "1"):
            filters[k] = True
    text = str(f.get("text") or "").strip()
    if text:
        filters["text"] = text[:200]
    out = {"v": ALERT_VERSION, "name": str(data.get("name") or "").strip()[:80], "emails": emails[:20],
           "deal": deal, "city": city, "filters": filters, "price_drops": bool(data.get("price_drops"))}
    a = data.get("area")
    if isinstance(a, dict):
        lat, lon, r = _num_signed(a.get("lat")), _num_signed(a.get("lon")), _num(a.get("radius_m"))
        if lat is not None and lon is not None and r and -90 <= lat <= 90 and -180 <= lon <= 180:
            out["area"] = {"lat": round(lat, 6), "lon": round(lon, 6), "radius_m": int(min(max(r, 100), 50000))}
    return out


def _num_signed(v: Any) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


# ================================================================ dopasowanie (jak applyFilters w app.js)

def norm(s: Any) -> str:
    """Jak norm() w app.js: małe litery, bez polskich znaków."""
    t = unicodedata.normalize("NFD", str(s or "").lower())
    t = "".join(ch for ch in t if not ("̀" <= ch <= "ͯ"))
    return t.replace("ł", "l")


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def listing_ppm(l: dict) -> Optional[int]:
    if l.get("deal", "sprzedaz") == "sprzedaz" and l.get("price") and l.get("area"):
        return js_round(l["price"] / l["area"])
    return None


def room_key(r: Any) -> str:
    if r is None or not _is_num(r):
        return ""
    if r >= 5:
        return "5+"
    return str(int(r)) if float(r).is_integer() else str(r)


def matches(l: dict, alert: dict) -> bool:
    """Czy oferta pasuje do alertu. Semantyka 1:1 ze stroną (applyFilters w docs/app.js):
    nieznana wartość nie przechodzi filtra „musi być X”, ale nie odpada przy „bez parteru /
    bez ostatniego piętra” (te odrzucają tylko pewne przypadki)."""
    deal = alert.get("deal") or "sprzedaz"
    if l.get("deal") and l.get("deal") != deal:
        return False
    if not l.get("active"):
        return False
    f = alert.get("filters") or {}
    price, area = l.get("price"), l.get("area")

    pmin, pmax = _num(f.get("price_min")), _num(f.get("price_max"))
    amin, amax = _num(f.get("area_min")), _num(f.get("area_max"))
    if pmin is not None and not (_is_num(price) and price >= pmin):
        return False
    if pmax is not None and not (price and price <= pmax):
        return False
    if amin is not None and not (_is_num(area) and area >= amin):
        return False
    if amax is not None and not (area and area <= amax):
        return False
    if deal == "sprzedaz":
        ppm = _num(f.get("ppm_max"))
        if ppm is not None:
            lp = listing_ppm(l)
            if not (lp and lp <= ppm):
                return False
    rooms = f.get("rooms") or []
    if deal != "pokoj" and rooms and room_key(l.get("rooms")) not in rooms:
        return False
    dists = f.get("districts") or []
    if dists and str(l.get("district") or "").strip() not in dists:
        return False
    market = f.get("market") or []
    if deal == "sprzedaz" and market and l.get("market") not in market:
        return False
    seller = f.get("seller") or []
    if seller and l.get("seller") not in seller:
        return False
    srcs = f.get("sources") or []
    if srcs and l.get("source") not in srcs:
        return False
    q = norm(f.get("text") or "").strip()
    if q:
        hay = norm(" ".join(str(l.get(k) or "") for k in ("title", "street", "district", "description")))
        if any(w not in hay for w in q.split()):
            return False

    fl = l.get("floor_num") if _is_num(l.get("floor_num")) else None
    ft = l.get("floors_total")
    ft = ft if _is_num(ft) and ft > 0 else None
    fmin, fmax = _floor_input(f.get("floor_min")), _floor_input(f.get("floor_max"))
    if (fmin is not None or fmax is not None) and (
            fl is None or (fmin is not None and fl < fmin) or (fmax is not None and fl > fmax)):
        return False
    if f.get("no_ground") and fl is not None and fl <= 0:
        return False
    if f.get("no_top") and fl is not None and ft is not None and fl >= ft:
        return False

    furn = f.get("furnished")
    if furn is True and l.get("furnished") is not True:
        return False
    if furn is False and l.get("furnished") is not False:
        return False
    conds = f.get("conditions") or []
    if conds and COND_OF.get(l.get("condition") or "", "") not in conds:
        return False
    bts = f.get("building_types") or []
    if bts and BT_OF.get(l.get("building_type") or "", "") not in bts:
        return False
    feats = [x for x in (f.get("features") or []) if deal in MONTHLY or x not in RENT_ONLY_FEATS]
    if feats:
        have = {FEAT_OF[x] for x in (l.get("features") or []) if x in FEAT_OF}
        if any(x not in have for x in feats):
            return False

    a = alert.get("area")
    if a:
        lat, lon = l.get("lat"), l.get("lon")
        if not (_is_num(lat) and _is_num(lon)):
            return False
        if haversine_m(a["lat"], a["lon"], lat, lon) > a["radius_m"]:
            return False
    return True


# ================================================================ dane

def data_file(city: str, name: str) -> Optional[Path]:
    """Jedyne miejsce, które zna układ plików: docs/data/<miasto>/<plik>, a dla domyślnego
    miasta także stary układ docs/data/<plik>."""
    p = DATA_DIR / city / name
    if p.exists():
        return p
    if city == DEFAULT_CITY:
        p = DATA_DIR / name
        if p.exists():
            return p
    return None


def _load_json(path: Optional[Path], default: Any) -> Any:
    if path is None:
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def load_meta(city: str) -> dict:
    m = _load_json(data_file(city, "meta.json"), {})
    return m if isinstance(m, dict) else {}


def load_listings(city: str, deal: str) -> List[dict]:
    raw = _load_json(data_file(city, "%s.json" % deal), [])
    if isinstance(raw, dict):
        raw = raw.get("listings") or []
    return [r for r in raw if isinstance(r, dict) and r.get("key")] if isinstance(raw, list) else []


def parse_ts(s: Any) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _old_price(l: dict) -> Optional[int]:
    h = l.get("price_history") or []
    if len(h) >= 2 and l.get("price"):
        prev = h[-2].get("price")
        if prev and prev > l["price"]:
            return prev
    return None


def candidates(listings: List[dict], deal_meta: dict, since: datetime,
               now: Optional[datetime] = None) -> Tuple[List[dict], List[dict], Dict[str, int]]:
    """Oferty nowe od `since` i obniżki cen od `since` (przed filtrami alertów).

    Nowa = pierwszy raz widziana w tym przebiegu (first_seen >= since), aktywna, ale NIE:
      * z pierwszego udanego przebiegu danego portalu (first_ok) — to stan wyjściowy,
      * gdy ta sama oferta (grupa duplikatów) była już wcześniej na innym portalu,
      * gdy portal podaje datę publikacji starszą niż MAX_LISTING_AGE_DAYS,
      * gdy portal nagle „znalazł” setki ofert naraz (doładowanie, nie nowości).
    Zwraca (nowe, obniżki, statystyki pominiętych)."""
    now = now or datetime.now(timezone.utc)
    srcs = (deal_meta or {}).get("sources") or {}
    deal_first = parse_ts((deal_meta or {}).get("first_run"))
    stats = {"baseline": 0, "repost": 0, "old": 0, "bulk": 0}

    groups: Dict[str, List[dict]] = {}
    for l in listings:
        if l.get("dup_group"):
            groups.setdefault(l["dup_group"], []).append(l)

    fresh: List[dict] = []
    known_by_src: Dict[str, int] = {}
    for l in listings:
        fs = parse_ts(l.get("first_seen"))
        if fs is None:
            continue
        if fs >= since:
            fresh.append(l)
        elif l.get("active"):
            known_by_src[l.get("source")] = known_by_src.get(l.get("source"), 0) + 1

    new_by_src: Dict[str, int] = {}
    for l in fresh:
        new_by_src[l.get("source")] = new_by_src.get(l.get("source"), 0) + 1
    bulk = {s for s, n in new_by_src.items() if n >= BULK_MIN and n > BULK_FRACTION * known_by_src.get(s, 0)}

    new: List[dict] = []
    for l in fresh:
        if not l.get("active"):
            continue
        fs = parse_ts(l.get("first_seen"))
        first_ok = parse_ts((srcs.get(l.get("source")) or {}).get("first_ok"))
        if first_ok is None or fs <= first_ok or (deal_first is not None and fs <= deal_first):
            stats["baseline"] += 1
            continue
        if l.get("source") in bulk:
            stats["bulk"] += 1
            continue
        created = parse_ts(l.get("created_at"))
        if (created is not None and datetime(2000, 1, 1, tzinfo=timezone.utc) < created <= now + timedelta(days=1)
                and created < fs - timedelta(days=MAX_LISTING_AGE_DAYS)):
            stats["old"] += 1
            continue
        g = groups.get(l.get("dup_group") or "", [])
        if any((parse_ts(o.get("first_seen")) or fs) < fs for o in g if o is not l):
            stats["repost"] += 1
            continue
        new.append(l)

    drops: List[dict] = []
    for l in listings:
        if not l.get("active"):
            continue
        fs = parse_ts(l.get("first_seen"))
        if fs is None or fs >= since:
            continue
        h = l.get("price_history") or []
        last = parse_ts(h[-1].get("date")) if h else None
        if last is not None and last >= since and _old_price(l):
            drops.append(l)
    return new, drops, stats


def pick_for_alert(alert: dict, new: List[dict], drops: List[dict]) -> Tuple[List[dict], List[dict]]:
    """Filtry alertu + jedna pozycja na grupę duplikatów (jak „jedna karta” na stronie)."""
    def dedup(items: List[dict], used: set) -> List[dict]:
        out = []
        for l in sorted(items, key=_rank):
            g = l.get("dup_group")
            if g:
                if g in used:
                    continue
                used.add(g)
            out.append(l)
        return out

    used: set = set()
    n = dedup([l for l in new if matches(l, alert)], used)
    d = dedup([l for l in drops if matches(l, alert)], used) if alert.get("price_drops") else []
    return n, d


def _rank(l: dict) -> Tuple:
    # najtańsze pierwsze; przy remisie oferta ze zdjęciem i dokładną lokalizacją
    return (l.get("price") or 10 ** 12, 0 if l.get("images") else 1,
            0 if l.get("geo") in (None, "dokładna") else 1, l.get("key"))


# ================================================================ opis alertu / formatowanie

def fmt_int(n: Any, sep: str = " ") -> str:
    try:
        return "{:,}".format(int(round(float(n)))).replace(",", sep)
    except (TypeError, ValueError):
        return str(n)


def fmt_dec(n: Any) -> str:
    x = round(float(n), 1)
    return fmt_int(x) if x.is_integer() else ("%.1f" % x).replace(".", ",")


def short_zl(n: float) -> str:
    """Jak shortZl() w app.js."""
    if n >= 1000000:
        return fmt_dec(n / 1000000) + " mln"
    if n >= 10000:
        return fmt_dec(n / 1000) + " tys."
    return fmt_int(n) + " zł"


def plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n == 1:
        return one
    if 2 <= n10 <= 4 and (n100 < 12 or n100 > 14):
        return few
    return many


def _range(a: Any, b: Any, fmt) -> str:
    x, y = _num(a), _num(b)
    if x is not None and y is not None:
        return "%s – %s" % (fmt(x), fmt(y))
    if x is not None:
        return "od " + fmt(x)
    if y is not None:
        return "do " + fmt(y)
    return ""


def radius_text(m: int) -> str:
    return ("%s km" % fmt_dec(m / 1000)) if m >= 1000 else ("%d m" % m)


def summary_parts(alert: dict) -> List[str]:
    """Krótki opis filtrów, np. ['Krzyki', 'do 3 500 zł', '2–3 pok.']."""
    f, deal = alert.get("filters") or {}, alert.get("deal")
    out: List[str] = []
    d = f.get("districts") or []
    if d:
        out.append(", ".join(d[:3]) + (" +%d" % (len(d) - 3) if len(d) > 3 else ""))
    if alert.get("area"):
        out.append("do %s od punktu" % radius_text(alert["area"]["radius_m"]))
    pr = _range(f.get("price_min"), f.get("price_max"),
                (lambda n: fmt_int(n) + " zł") if deal in MONTHLY else short_zl)
    if pr:
        out.append(pr)
    if deal != "pokoj" and f.get("rooms"):
        out.append(", ".join(sorted(f["rooms"])) + " pok.")
    ar = _range(f.get("area_min"), f.get("area_max"), lambda n: fmt_dec(n) + " m²")
    if ar:
        out.append(ar)
    if deal == "sprzedaz" and f.get("ppm_max") is not None:
        out.append("do %s zł/m²" % fmt_int(f["ppm_max"]))
    a, b = f.get("floor_min"), f.get("floor_max")
    if a is not None or b is not None:
        if a is not None and b is not None:
            out.append(("parter" if a == 0 else "piętro %d" % a) if a == b else "piętro %d–%d" % (a, b))
        elif a is not None:
            out.append("piętro od %d" % a)
        else:
            out.append("piętro do %d" % b)
    if f.get("no_ground"):
        out.append("bez parteru")
    if f.get("no_top"):
        out.append("bez ostatniego piętra")
    for x in f.get("features") or []:
        if deal in MONTHLY or x not in RENT_ONLY_FEATS:
            out.append(FEAT_LABEL.get(x, x))
    if f.get("furnished") is True:
        out.append("umeblowane")
    elif f.get("furnished") is False:
        out.append("nieumeblowane")
    out += [COND_LABEL.get(x, x) for x in f.get("conditions") or []]
    out += [x for x in f.get("building_types") or []]
    if deal == "sprzedaz":
        out += ["rynek " + ("wtórny" if m == "wtorny" else m) for m in f.get("market") or []]
    out += [{"prywatny": "osoba prywatna", "agencja": "biuro"}.get(s, s) for s in f.get("seller") or []]
    out += [SOURCE_NAMES.get(s, s) for s in f.get("sources") or []]
    if f.get("text"):
        out.append("„%s”" % f["text"])
    return out


def alert_title(alert: dict) -> str:
    if alert.get("name"):
        return alert["name"]
    p = summary_parts(alert)[:2]
    return ", ".join(p) if p else {"sprzedaz": "Kupno", "wynajem": "Wynajem", "pokoj": "Pokoje"}[alert["deal"]]


def count_phrase(n: int, deal: str) -> str:
    if deal == "pokoj":
        w = plural(n, "nowy pokój", "nowe pokoje", "nowych pokoi")
    else:
        w = plural(n, "nowe mieszkanie", "nowe mieszkania", "nowych mieszkań")
    return "%d %s" % (n, w)


def subject_for(alert: dict, n_new: int, n_drop: int) -> str:
    parts = []
    if n_new:
        parts.append(count_phrase(n_new, alert["deal"]))
    if n_drop:
        parts.append("%d %s" % (n_drop, plural(n_drop, "obniżka ceny", "obniżki cen", "obniżek cen")))
    head = " i ".join(parts)
    return ("%s: %s" % (head, alert_title(alert))).replace(" ", " ")


def site_link(alert: dict, base: str, recent: bool = False) -> str:
    """Adres strony z filtrami alertu w #hash (ten sam format co toParams() w app.js)."""
    f, deal = alert.get("filters") or {}, alert["deal"]
    p: List[Tuple[str, str]] = [("deal", deal)]
    if alert.get("city") and alert["city"] != DEFAULT_CITY:
        p.append(("city", alert["city"]))

    def n(v: Any) -> str:
        x = _num(v)
        return "" if x is None else (str(int(x)) if x.is_integer() else str(x))

    for key, k in (("q", "text"), ("pmin", "price_min"), ("pmax", "price_max"), ("amin", "area_min"),
                   ("amax", "area_max"), ("ppm", "ppm_max"), ("fmin", "floor_min"), ("fmax", "floor_max")):
        v = f.get(k)
        if v is None or v == "":
            continue
        p.append((key, str(v) if k == "text" or k.startswith("floor") else n(v)))
    for key, k in (("rooms", "rooms"), ("districts", "districts"), ("market", "market"), ("seller", "seller"),
                   ("sources", "sources"), ("cond", "conditions"), ("btype", "building_types"), ("feat", "features")):
        if f.get(k):
            p.append((key, "|".join(f[k])))
    if f.get("furnished") is not None:
        p.append(("furn", "tak" if f["furnished"] else "nie"))
    if f.get("no_ground"):
        p.append(("noGround", "1"))
    if f.get("no_top"):
        p.append(("noTop", "1"))
    if recent:
        p.append(("days", "1"))
    return base + "#" + urlencode(p, quote_via=quote)


def floor_text(l: dict) -> str:
    """Jak floorText() w app.js."""
    fl = l.get("floor_num") if _is_num(l.get("floor_num")) else None
    ft = l.get("floors_total") if _is_num(l.get("floors_total")) and l.get("floors_total") > 0 else None
    if fl is not None:
        if fl < 0:
            return "suterena"
        if fl == 0:
            return "parter"
        return "piętro %d%s" % (fl, "/%d" % ft if ft and fl <= ft else "")
    s = str(l.get("floor") or "").strip()
    if s:
        if re.match(r"^(0|parter)$", s, re.I):
            return "parter"
        return ("piętro " + s) if s[0].isdigit() else s.lower()
    return ""


FEAT_WORD = {"balkon": "balkon", "taras": "taras", "loggia": "loggia", "ogrodek": "ogródek", "winda": "winda",
             "garaz": "garaż", "parking": "parking", "piwnica": "piwnica", "komorka": "komórka",
             "klimatyzacja": "klimatyzacja", "zwierzeta": "zwierzęta OK"}


def feat_line(l: dict) -> List[str]:
    """Jak featLine() w app.js (do 4 pozycji)."""
    fs = set(l.get("features") or [])

    def pick(*names: str) -> str:
        for x in names:
            if x in fs:
                return FEAT_WORD[x]
        return ""

    out = []
    fl = floor_text(l)
    if fl:
        out.append(fl)
    furn = "umeblowane" if l.get("furnished") is True else ("bez mebli" if l.get("furnished") is False else "")
    c = l.get("condition")
    cond = "do remontu" if c == "do_remontu" else ("stan deweloperski" if c in ("stan_deweloperski", "do_wykonczenia") else "")
    if l.get("deal") in MONTHLY:
        cand = [furn, pick("balkon", "taras", "loggia"), pick("winda"), pick("zwierzeta"), pick("ogrodek"),
                pick("garaz", "parking"), pick("klimatyzacja")]
    else:
        cand = [cond, pick("balkon", "taras", "loggia"), pick("winda"), pick("ogrodek"), pick("garaz", "parking"),
                pick("piwnica", "komorka"), furn]
    for x in cand:
        if x and len(out) < 4:
            out.append(x)
    return out


def _safe_url(u: Any) -> str:
    return u if isinstance(u, str) and re.match(r"^https?://", u, re.I) else ""


# ================================================================ e-mail

ACCENT = "#0f766e"
TEXT = "#18181b"
MUTED = "#71717a"
FAINT = "#a1a1aa"
LINE = "#e7e7e4"
BG = "#fafaf9"
FONT = "Inter,-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial,sans-serif"


def _e(s: Any) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def _listing_facts(l: dict, deal: str) -> List[str]:
    facts = []
    if l.get("area"):
        facts.append(fmt_dec(l["area"]) + " m²")
    if deal != "pokoj" and l.get("rooms"):
        facts.append("%d %s" % (l["rooms"], plural(int(l["rooms"]), "pokój", "pokoje", "pokoi")))
    ppm = listing_ppm(l)
    if ppm:
        facts.append(fmt_int(ppm) + " zł/m²")
    return facts


def _price_text(l: dict, deal: str) -> str:
    if not l.get("price"):
        return "Cena do negocjacji"
    return fmt_int(l["price"]) + " zł" + (" / mies." if deal in MONTHLY else "")


def _group_others(l: dict, by_group: Dict[str, List[dict]]) -> List[dict]:
    return [o for o in by_group.get(l.get("dup_group") or "", []) if o is not l and o.get("active")]


def _approx_note(l: dict, alert: dict) -> str:
    if not alert.get("area"):
        return ""
    g = l.get("geo")
    if g in GEO_APPROX:
        return "lokalizacja przybliżona (%s)" % GEO_APPROX[g]
    return ""


def _item_html(l: dict, alert: dict, by_group: Dict[str, List[dict]], drop: bool) -> str:
    deal = alert["deal"]
    url = _safe_url(l.get("url")) or "#"
    img = _safe_url((l.get("images") or [None])[0])
    thumb = ('<img src="%s" width="132" height="99" alt="" style="display:block;width:132px;height:99px;'
             'object-fit:cover;border-radius:10px;border:1px solid %s;background:#efefed">' % (_e(img), LINE)
             if img else
             '<div style="width:132px;height:99px;border-radius:10px;background:#efefed;border:1px solid %s;'
             'color:%s;font-size:12px;line-height:99px;text-align:center">brak zdjęcia</div>' % (LINE, FAINT))
    price = '<span style="font-size:19px;font-weight:700;color:%s;letter-spacing:-0.01em">%s</span>' % (
        TEXT, _e(_price_text(l, deal)))
    old = _old_price(l) if drop else None
    if old:
        pct = round((1 - l["price"] / old) * 100)
        price += (' <span style="font-size:13px;color:%s;text-decoration:line-through">%s zł</span>'
                  ' <span style="font-size:12px;font-weight:700;color:%s">−%d%%</span>' % (FAINT, fmt_int(old), ACCENT, pct))
    lines = []
    facts = _listing_facts(l, deal)
    if facts:
        lines.append('<div style="font-size:14px;color:#3f3f46;margin-top:2px">%s</div>' % " · ".join(map(_e, facts)))
    feats = feat_line(l)
    if feats:
        lines.append('<div style="font-size:13px;color:%s;margin-top:2px">%s</div>' % (MUTED, " · ".join(map(_e, feats))))
    if deal in MONTHLY:
        extra = []
        if l.get("rent_extra"):
            extra.append("+ %s zł opłaty" % fmt_int(l["rent_extra"]))
        if l.get("deposit"):
            extra.append("kaucja %s zł" % fmt_int(l["deposit"]))
        if extra:
            lines.append('<div style="font-size:13px;color:%s;margin-top:2px">%s</div>' % (MUTED, " · ".join(map(_e, extra))))
    loc = ", ".join(x for x in (l.get("district"), l.get("street")) if x)
    if loc:
        lines.append('<div style="font-size:14px;color:#3f3f46;margin-top:2px">%s</div>' % _e(loc))
    note = _approx_note(l, alert)
    if note:
        lines.append('<div style="font-size:12px;color:%s;font-style:italic;margin-top:1px">%s</div>' % (MUTED, _e(note)))
    title = l.get("title") or ("Pokój" if deal == "pokoj" else "Mieszkanie")
    lines.append('<div style="font-size:13px;line-height:1.4;margin-top:4px"><a href="%s" style="color:%s;text-decoration:none">%s</a></div>'
                 % (_e(url), MUTED, _e(title[:140])))
    src = '<a href="%s" style="color:%s;font-weight:600;text-decoration:none">%s ↗</a>' % (
        _e(url), ACCENT, _e(SOURCE_NAMES.get(l.get("source"), l.get("source"))))
    others = _group_others(l, by_group)
    if others:
        src += ' <span style="color:%s">też na</span> ' % FAINT + ", ".join(
            '<a href="%s" style="color:%s;text-decoration:none">%s</a>' % (
                _e(_safe_url(o.get("url")) or "#"), MUTED, _e(SOURCE_NAMES.get(o.get("source"), o.get("source"))))
            for o in others)
    lines.append('<div style="font-size:13px;margin-top:6px">%s</div>' % src)
    return ('<tr><td style="padding:16px 0;border-top:1px solid %s">'
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%%"><tr>'
            '<td width="132" valign="top" style="width:132px;padding-right:16px"><a href="%s">%s</a></td>'
            '<td valign="top" style="font-family:%s">%s%s</td>'
            '</tr></table></td></tr>') % (LINE, _e(url), thumb, FONT, price, "".join(lines))


def _item_text(l: dict, alert: dict, by_group: Dict[str, List[dict]], drop: bool) -> str:
    deal = alert["deal"]
    head = _price_text(l, deal).replace(" ", " ")
    old = _old_price(l) if drop else None
    if old:
        head += " (było %s zł)" % fmt_int(old, " ")
    parts = [head] + _listing_facts(l, deal)
    out = ["• " + " · ".join(parts).replace(" ", " ")]
    extra = feat_line(l)
    loc = ", ".join(x for x in (l.get("district"), l.get("street")) if x)
    if loc:
        extra.append(loc)
    note = _approx_note(l, alert)
    if note:
        extra.append(note)
    if extra:
        out.append("  " + " · ".join(extra))
    out.append("  %s: %s" % (SOURCE_NAMES.get(l.get("source"), l.get("source")), l.get("url") or ""))
    for o in _group_others(l, by_group):
        out.append("  też na %s: %s" % (SOURCE_NAMES.get(o.get("source"), o.get("source")), o.get("url") or ""))
    return "\n".join(out)


def render_email(alert: dict, new: List[dict], drops: List[dict], all_listings: List[dict],
                 secret_name: str = "", site_url: str = SITE_URL, repo: str = REPO) -> Tuple[str, str, str]:
    """(temat, HTML, tekst). Maks. MAX_PER_EMAIL ofert, reszta jako link do strony."""
    deal = alert["deal"]
    by_group: Dict[str, List[dict]] = {}
    for l in all_listings:
        if l.get("dup_group"):
            by_group.setdefault(l["dup_group"], []).append(l)
    # obniżki dostają swoje miejsca (do 1/3 maila), żeby nie zniknęły pod nowościami
    show_drop = drops[:max(MAX_PER_EMAIL // 3, MAX_PER_EMAIL - len(new))]
    show_new = new[:MAX_PER_EMAIL - len(show_drop)]
    rest = len(new) + len(drops) - len(show_new) - len(show_drop)
    subject = subject_for(alert, len(new), len(drops))
    link = site_link(alert, site_url)
    link_recent = site_link(alert, site_url, recent=True)
    city = CITY_NAMES.get(alert.get("city") or DEFAULT_CITY, (alert.get("city") or "").capitalize())
    title = alert_title(alert)
    chips = summary_parts(alert)
    secrets_url = "https://github.com/%s/settings/secrets/actions" % repo

    if new:
        h1 = "%s %s" % (count_phrase(len(new), deal), DEAL_WHAT[deal])
        if drops:
            h1 += " i %d %s" % (len(drops), plural(len(drops), "obniżka", "obniżki", "obniżek"))
    else:
        h1 = "%d %s" % (len(drops), plural(len(drops), "obniżka ceny", "obniżki cen", "obniżek cen"))

    def section(label: str, items: List[dict], drop: bool) -> str:
        if not items:
            return ""
        if not label:
            return "".join(_item_html(l, alert, by_group, drop) for l in items)
        return ('<tr><td style="padding:22px 0 2px;font-family:%s;font-size:12px;font-weight:600;letter-spacing:.06em;'
                'text-transform:uppercase;color:%s">%s</td></tr>' % (FONT, MUTED, label)
                + "".join(_item_html(l, alert, by_group, drop) for l in items))

    chip_html = " ".join(
        '<span style="display:inline-block;margin:0 4px 6px 0;padding:3px 10px;border-radius:999px;'
        'background:#f4f4f3;border:1px solid %s;font-size:12.5px;color:#3f3f46">%s</span>' % (LINE, _e(c))
        for c in chips)
    more = ""
    if rest > 0:
        more = ('<tr><td align="center" style="padding:22px 0 6px">'
                '<a href="%s" style="display:inline-block;padding:11px 20px;border-radius:11px;background:%s;color:#ffffff;'
                'font-family:%s;font-size:14px;font-weight:600;text-decoration:none">i %d więcej — zobacz na stronie</a></td></tr>'
                % (_e(link_recent), TEXT, FONT, rest))
    area_note = ""
    if alert.get("area"):
        area_note = ('<div style="margin-top:4px;font-size:12.5px;color:%s">Okolica: do %s od wybranego punktu na mapie. '
                     'Oferty bez dokładnego adresu sprawdzamy po ulicy lub osiedlu.</div>'
                     % (MUTED, _e(radius_text(alert["area"]["radius_m"]))))
    off = ("Żeby wyłączyć ten alert, usuń sekret <b>%s</b> w <a href=\"%s\" style=\"color:%s\">ustawieniach repozytorium</a> "
           "(Settings → Secrets and variables → Actions)." % (_e(secret_name), _e(secrets_url), MUTED)
           if secret_name else
           "Żeby wyłączyć ten alert, usuń jego sekret ALERT_… w <a href=\"%s\" style=\"color:%s\">ustawieniach repozytorium</a>."
           % (_e(secrets_url), MUTED))

    body = (
        '<!doctype html><html lang="pl"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light">'
        '<title>%(subject)s</title></head>'
        '<body style="margin:0;padding:0;background:%(bg)s">'
        '<div style="display:none;max-height:0;overflow:hidden">%(pre)s</div>'
        '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0" style="background:%(bg)s">'
        '<tr><td align="center" style="padding:24px 12px">'
        '<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0" '
        'style="max-width:600px;background:#ffffff;border:1px solid %(line)s;border-radius:16px">'
        '<tr><td style="padding:22px 24px 6px;font-family:%(font)s">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
        '<td style="width:26px;height:26px;border-radius:8px;background:%(accent)s;color:#fff;font-size:15px;'
        'text-align:center;line-height:26px">&#8962;</td>'
        '<td style="padding-left:9px;font-size:14px;font-weight:650;color:%(text)s">Mieszkania '
        '<span style="color:%(muted)s;font-weight:500">%(city)s</span></td></tr></table>'
        '<h1 style="margin:18px 0 4px;font-size:22px;line-height:1.25;font-weight:700;letter-spacing:-0.02em;color:%(text)s">%(h1)s</h1>'
        '<div style="font-size:14px;color:%(muted)s;margin-bottom:10px">Alert „%(title)s”%(drops_note)s</div>'
        '<div>%(chips)s</div>%(area_note)s'
        '</td></tr>'
        '<tr><td style="padding:0 24px 8px"><table role="presentation" width="100%%" cellpadding="0" cellspacing="0" border="0">'
        '%(new)s%(drop)s%(more)s</table></td></tr>'
        '<tr><td style="padding:18px 24px 22px;border-top:1px solid %(line)s;font-family:%(font)s;font-size:12.5px;line-height:1.55;color:%(muted)s">'
        '<a href="%(link)s" style="color:%(accent)s;font-weight:600;text-decoration:none">Otwórz stronę z filtrami tego alertu ↗</a><br>'
        'Wysyłamy tylko oferty, które pojawiły się od ostatniego sprawdzenia (co godzinę).<br>%(off)s'
        '</td></tr></table></td></tr></table></body></html>'
    ) % {
        "subject": _e(subject), "bg": BG, "line": LINE, "font": FONT, "accent": ACCENT, "text": TEXT, "muted": MUTED,
        "pre": _e(", ".join(chips[:4])), "city": _e(city), "h1": _e(h1), "title": _e(title),
        "drops_note": " · także obniżki cen" if alert.get("price_drops") else "",
        "chips": chip_html, "area_note": area_note,
        "new": section("Nowe oferty" if show_drop else "", show_new, False),
        "drop": section("Obniżki cen", show_drop, True), "more": more, "link": _e(link), "off": off,
    }

    t = [subject, ""]
    if chips:
        t.append("Filtry: " + " · ".join(chips).replace(" ", " "))
        t.append("")
    if show_new:
        t.append("NOWE OFERTY")
        t += [_item_text(l, alert, by_group, False) + "\n" for l in show_new]
    if show_drop:
        t.append("OBNIŻKI CEN")
        t += [_item_text(l, alert, by_group, True) + "\n" for l in show_drop]
    if rest > 0:
        t.append("…i %d więcej — zobacz na stronie: %s" % (rest, link_recent))
        t.append("")
    t.append("Strona z filtrami alertu: " + link)
    t.append("Wyłączenie alertu: usuń sekret %s w %s" % (secret_name or "ALERT_…", secrets_url))
    return subject, body, "\n".join(t).replace(" ", " ")


def build_message(sender: str, to: str, subject: str, html_body: str, text_body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr(("Mieszkania Wrocław", sender))
    msg["To"] = to
    msg["Message-ID"] = make_msgid(domain="mieszkania.local")
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return msg


# ================================================================ stan (ochrona przed podwójną wysyłką)

def alert_hash(alert: dict, key: str = "") -> str:
    """HMAC (klucz = hasło aplikacji Gmaila) — w publicznym repo nie da się z niego nic odczytać."""
    canon = json.dumps(alert, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hmac.new(("mw-alert:" + key).encode("utf-8"), canon, hashlib.sha256).hexdigest()[:16]


def load_state(path: Optional[Path] = None) -> dict:
    s = _load_json(path or STATE_PATH, {})
    if not isinstance(s, dict) or not isinstance(s.get("sent"), dict):
        s = {"v": 1, "sent": {}}
    return s


def save_state(state: dict, now: datetime, path: Optional[Path] = None) -> None:
    path = path or STATE_PATH
    cutoff = now - timedelta(days=STATE_KEEP_DAYS)
    state["sent"] = {k: v for k, v in sorted(state["sent"].items())
                     if (parse_ts(v) or now) >= cutoff}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"v": 1, "sent": state["sent"]}, indent=2) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(path))


# ================================================================ źródła alertów

def alerts_from_env(env: Optional[Dict[str, str]] = None) -> List[Tuple[str, str]]:
    """[(nazwa sekretu, kod)] z ALL_SECRETS (JSON ze wszystkimi sekretami) albo zmiennych ALERT_*."""
    env = os.environ if env is None else env
    found: Dict[str, str] = {}
    raw = env.get("ALL_SECRETS")
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            data = {}
            log.warning("ALL_SECRETS nie jest poprawnym JSON-em — pomijam")
        if isinstance(data, dict):
            for k, v in data.items():
                if isinstance(k, str) and k.upper().startswith(SECRET_PREFIX) and isinstance(v, str):
                    found[k.upper()] = v
    for k, v in env.items():
        if k.upper().startswith(SECRET_PREFIX) and k.upper() not in found and v:
            found[k.upper()] = v
    return sorted(found.items())


def alerts_from_file(path: str) -> List[Tuple[str, str]]:
    """Plik do testów: JSON {nazwa: kod} albo linie „ALERT_NAZWA=kod” / „kod”."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "v" not in data:
            return sorted((str(k), v if isinstance(v, str) else json.dumps(v)) for k, v in data.items())
        if isinstance(data, dict):
            return [("ALERT_1", text)]
    except ValueError:
        pass
    out = []
    for i, line in enumerate(l.strip() for l in text.splitlines()):
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^(ALERT_[A-Za-z0-9_]+)\s*=\s*(.+)$", line)
        out.append((m.group(1), m.group(2)) if m else ("ALERT_%d" % (len(out) + 1), line))
    return out


# ================================================================ main

def site_url_default() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if repo and "/" in repo:
        owner, name = repo.split("/", 1)
        return "https://%s.github.io/%s/" % (owner.lower(), name)
    return SITE_URL


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Powiadomienia e-mail o nowych ofertach (alerty z sekretów ALERT_*)")
    ap.add_argument("--dry-run", action="store_true", help="nic nie wysyłaj — pokaż, co by poszło, i zapisz podgląd HTML")
    ap.add_argument("--alerts-file", help="plik z kodami alertów (zamiast sekretów; do testów)")
    ap.add_argument("--since", help="traktuj jako nowe oferty widziane od tej chwili (ISO, np. 2026-10-14T00:00:00Z); "
                                    "domyślnie: ostatni przebieg pobierania")
    ap.add_argument("--preview-dir", default=None, help="gdzie zapisać podgląd maili (dry-run)")
    ap.add_argument("--site-url", default=None)
    ap.add_argument("--test", action="store_true",
                    help="wyślij JEDEN przykładowy mail (5 najnowszych ofert kupna, temat z [TEST]) na GMAIL_USER")
    args = ap.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    if args.test:
        return run_test(args)
    try:
        return run(args)
    except Exception as e:  # nigdy nie psujemy przebiegu (i nie wypisujemy treści alertów)
        log.error("Powiadomienia: nieoczekiwany błąd (%s) — pomijam", type(e).__name__)
        if args.dry_run:
            raise
        return 0


def run(args) -> int:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    in_ci = bool(os.environ.get("GITHUB_ACTIONS"))
    site = args.site_url or site_url_default()
    repo = os.environ.get("GITHUB_REPOSITORY") or REPO
    raw_alerts = alerts_from_file(args.alerts_file) if args.alerts_file else alerts_from_env()
    if not raw_alerts:
        log.info("Brak alertów (sekretów ALERT_…) — nic do wysłania.")
        return 0
    user = (os.environ.get("GMAIL_USER") or "").strip()
    password = re.sub(r"\s+", "", os.environ.get("GMAIL_APP_PASSWORD") or "")
    if not args.dry_run and not (user and password):
        log.warning("Brak sekretów GMAIL_USER / GMAIL_APP_PASSWORD — nie wysyłam powiadomień (%d alertów).", len(raw_alerts))
        return 0
    since_override = parse_ts(args.since) if args.since else None
    if args.since and since_override is None:
        raise SystemExit("--since: nieprawidłowa data")

    state = load_state()
    state_changed = False
    cache: Dict[Tuple[str, str], Optional[Tuple[str, List[dict], List[dict], List[dict]]]] = {}
    preview_dir = Path(args.preview_dir) if args.preview_dir else None
    smtp = None
    sent_total = 0

    try:
        for idx, (secret_name, code) in enumerate(raw_alerts, 1):
            tag = "alert %d" % idx
            try:
                alert = decode_alert(code)
            except ValueError as e:
                log.warning("%s: nieprawidłowy kod (%s) — pomijam", tag, e)
                continue
            except Exception:
                log.warning("%s: nieprawidłowy kod — pomijam", tag)
                continue
            ck = (alert["city"], alert["deal"])
            if ck not in cache:
                cache[ck] = _prepare(alert["city"], alert["deal"], since_override, now, args.dry_run)
            prep = cache[ck]
            if prep is None:
                log.info("%s: brak świeżych danych dla tego rodzaju ofert — pomijam", tag)
                continue
            run_ts, listings, new, drops = prep
            h = alert_hash(alert, password)
            if not args.dry_run and not since_override and (state["sent"].get(h) or "") >= run_ts:
                log.info("%s: ten przebieg był już wysłany — pomijam", tag)
                continue
            n_list, d_list = pick_for_alert(alert, new, drops)
            if not n_list and not d_list:
                log.info("%s: brak nowych ofert", tag)
                continue
            subject, body, text = render_email(alert, n_list, d_list, listings, secret_name, site, repo)
            recipients = alert["emails"] or ([user] if user else [])
            counts = "%d %s%s" % (len(n_list), plural(len(n_list), "nowa oferta", "nowe oferty", "nowych ofert"),
                                  (", %d %s" % (len(d_list), plural(len(d_list), "obniżka", "obniżki", "obniżek"))) if d_list else "")
            if args.dry_run:
                _print_dry(tag, secret_name, alert, recipients, subject, n_list, d_list, in_ci)
                if preview_dir:
                    preview_dir.mkdir(parents=True, exist_ok=True)
                    base = preview_dir / ("alert_%d" % idx)
                    base.with_suffix(".html").write_text(body, encoding="utf-8")
                    base.with_suffix(".txt").write_text(text, encoding="utf-8")
                    print("  podgląd: %s.html / .txt" % base)
                continue
            if not recipients:
                log.warning("%s: %s, ale brak adresu odbiorcy — pomijam", tag, counts)
                continue
            ok = 0
            for to in recipients:
                try:
                    if smtp is None:
                        smtp = _connect(user, password)
                    smtp.send_message(build_message(user, to, subject, body, text))
                    ok += 1
                except smtplib.SMTPAuthenticationError:
                    log.error("Gmail odrzucił logowanie — sprawdź sekrety GMAIL_USER i GMAIL_APP_PASSWORD. Przerywam wysyłkę.")
                    return 0
                except (smtplib.SMTPServerDisconnected, smtplib.SMTPException, OSError) as e:
                    log.warning("%s: błąd wysyłki do jednego z adresów (%s)", tag, type(e).__name__)
                    try:
                        if smtp is not None:
                            smtp.quit()
                    except Exception:
                        pass
                    smtp = None
            if ok:
                state["sent"][h] = run_ts
                state_changed = True
                sent_total += 1
            log.info("%s: %s, wysłano (%d/%d %s)", tag, counts, ok, len(recipients),
                     plural(len(recipients), "adres", "adresy", "adresów"))
    finally:
        if smtp is not None:
            try:
                smtp.quit()
            except Exception:
                pass
        if state_changed:
            try:
                save_state(state, now)
            except OSError as e:
                log.warning("Nie udało się zapisać stanu alertów (%s)", type(e).__name__)
    if not args.dry_run:
        log.info("Powiadomienia: %d %s, wysłano %d %s.", len(raw_alerts),
                 plural(len(raw_alerts), "alert", "alerty", "alertów"), sent_total, plural(sent_total, "mail", "maile", "maili"))
    return 0


TEST_COUNT = 5


def run_test(args) -> int:
    """Mail testowy: prawdziwy szablon, 5 najnowszych aktywnych ofert kupna, na adres GMAIL_USER.
    W logach tylko wynik (bez adresów i haseł). Kod wyjścia 1 = nie udało się wysłać."""
    user = (os.environ.get("GMAIL_USER") or "").strip()
    password = re.sub(r"\s+", "", os.environ.get("GMAIL_APP_PASSWORD") or "")
    deal = "sprzedaz"
    listings = [l for l in load_listings(DEFAULT_CITY, deal) if l.get("active")]
    listings.sort(key=lambda l: (l.get("first_seen") or "", l.get("key") or ""), reverse=True)
    sample = listings[:TEST_COUNT]
    if not sample:
        log.error("Test: brak ofert w docs/data/%s/%s.json", DEFAULT_CITY, deal)
        return 1
    alert = normalize_alert({"v": ALERT_VERSION, "name": "Test powiadomień", "deal": deal,
                             "city": DEFAULT_CITY, "filters": {}, "price_drops": False})
    site = args.site_url or site_url_default()
    repo = os.environ.get("GITHUB_REPOSITORY") or REPO
    subject, body, text = render_email(alert, sample, [], listings, "", site, repo)
    subject = "[TEST] " + subject
    body = body.replace("<title>", "<title>[TEST] ", 1)
    text = "[TEST] " + text
    if args.dry_run:
        preview = Path(args.preview_dir or ".")
        preview.mkdir(parents=True, exist_ok=True)
        (preview / "test_mail.html").write_text(body, encoding="utf-8")
        print("Temat:", subject)
        print("Podgląd: %s" % (preview / "test_mail.html"))
        return 0
    if not (user and password):
        log.error("Test: brak sekretów GMAIL_USER / GMAIL_APP_PASSWORD — nie wysyłam.")
        return 1
    try:
        smtp = _connect(user, password)
        try:
            smtp.send_message(build_message(user, user, subject, body, text))
        finally:
            try:
                smtp.quit()
            except Exception:
                pass
    except smtplib.SMTPAuthenticationError:
        log.error("Test: Gmail odrzucił logowanie — sprawdź sekrety GMAIL_USER i GMAIL_APP_PASSWORD.")
        return 1
    except (smtplib.SMTPException, OSError) as e:
        log.error("Test: błąd wysyłki (%s).", type(e).__name__)
        return 1
    log.info("Test: wysłano 1 mail testowy (%d ofert) na adres z sekretu GMAIL_USER.", len(sample))
    return 0


def _connect(user: str, password: str) -> smtplib.SMTP_SSL:
    s = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=30)
    s.login(user, password)
    return s


def _prepare(city: str, deal: str, since_override: Optional[datetime], now: datetime, dry: bool):
    meta = load_meta(city)
    dm = (meta.get("deals") or {}).get(deal)
    if not isinstance(dm, dict):
        return None
    run_ts = meta.get("last_run") or dm.get("last_run")
    run_dt = parse_ts(run_ts)
    if since_override is None:
        # tylko oferty z OSTATNIEGO przebiegu i tylko gdy ten typ był w nim pobierany
        if run_dt is None or dm.get("last_run") != run_ts:
            return None
        if not dry and now - run_dt > timedelta(hours=STALE_RUN_HOURS):
            return None
        since = run_dt
    else:
        since = since_override
    listings = load_listings(city, deal)
    new, drops, stats = candidates(listings, dm, since, now)
    if any(stats.values()):
        log.info("[%s] pominięte jako nie-nowości: %s", deal, ", ".join("%s=%d" % kv for kv in sorted(stats.items()) if kv[1]))
    return run_ts, listings, new, drops


def _print_dry(tag: str, secret_name: str, alert: dict, recipients: List[str], subject: str,
               n_list: List[dict], d_list: List[dict], in_ci: bool) -> None:
    if in_ci:  # logi Actions są publiczne
        print("%s: %d nowych, %d obniżek (szczegóły ukryte w GitHub Actions)" % (tag, len(n_list), len(d_list)))
        return
    print("=" * 72)
    print("%s (%s) -> %s" % (tag, secret_name, ", ".join(recipients) or "(brak adresu — poszłoby na GMAIL_USER)"))
    print("Temat:", subject)
    for label, items in (("nowe", n_list), ("obniżki", d_list)):
        for l in items:
            print("  [%s] %s | %s | %s | %s" % (label, _price_text(l, alert["deal"]).replace(" ", " "),
                                              " · ".join(_listing_facts(l, alert["deal"])).replace(" ", " "),
                                              l.get("district") or "-", l.get("url")))
    extra = len(n_list) + len(d_list) - MAX_PER_EMAIL
    if extra > 0:  # (podgląd wypisuje wszystkie; mail pokazuje maks. MAX_PER_EMAIL)
        print("  …i %d więcej (link do strony)" % extra)


if __name__ == "__main__":
    sys.exit(main())
