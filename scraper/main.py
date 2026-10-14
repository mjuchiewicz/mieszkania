"""Punkt wejścia: `python -m scraper.main`.

Dla każdego włączonego typu ogłoszeń (kupno / wynajem / pokoje) uruchamia
wszystkie włączone portale, które ten typ obsługują, scala wyniki z poprzednim
stanem (docs/data/<typ>.json), wykrywa duplikaty między portalami i zapisuje
docs/data/meta.json ze statusem każdego portalu dla każdego typu.

Różne portale pobieramy równolegle, ale typy ogłoszeń jednego portalu po kolei
(żeby nie zasypywać jednego serwera zapytaniami).
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import inspect
import json
import logging
import os
import re
import sys
import tempfile
import time
import traceback
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import yaml

from scraper.features import enrich
from scraper.geo import GEO_EXACT, geocode_listings
from scraper.models import BUILDING_TYPES, CONDITIONS, DEALS

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
DATA_DIR = ROOT / "docs" / "data"
LEGACY_LISTINGS_PATH = DATA_DIR / "listings.json"   # stary format (tylko sprzedaż)
META_PATH = DATA_DIR / "meta.json"

MAX_IMAGES = 1  # strona pokazuje tylko miniaturę
MAX_DESCRIPTION = 400  # opis służy tylko do wyszukiwania po słowach
MAX_PRICE_HISTORY = 50
PAUSE_BETWEEN_DEALS = 3.0  # s przerwy między typami ogłoszeń na tym samym portalu

DEAL_LABELS = {"sprzedaz": "Kupno", "wynajem": "Wynajem", "pokoj": "Pokoje"}
RENT_DEALS = ("wynajem", "pokoj")

log = logging.getLogger("scraper")


def deal_path(deal: str) -> Path:
    return DATA_DIR / ("%s.json" % deal)


# ---------------------------------------------------------------- helpers

def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        s2 = s.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def atomic_write_json(path: Path, data: Any, compact: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if compact and isinstance(data, list):
                # jedna oferta na linię: mały plik, a jednocześnie czytelne diffy w git
                f.write("[\n")
                f.write(",\n".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in data))
                f.write("\n]" if data else "]")
            elif compact:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            else:
                json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_json(path: Path, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data
    except FileNotFoundError:
        return default
    except (ValueError, OSError) as e:
        log.warning("Nie udało się wczytać %s (%s) — zaczynam od pustego stanu", path, e)
        return default


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("sources", {})
    cfg.setdefault("max_pages", 30)
    cfg.setdefault("drop_after_days", 14)
    deals = cfg.get("deals")
    if not isinstance(deals, dict) or not deals:
        # stary config: jeden `prefilter` = tylko sprzedaż
        deals = {"sprzedaz": {"enabled": True, "prefilter": cfg.get("prefilter") or {}}}
    norm_deals = {}
    for name in DEALS:
        d = deals.get(name)
        if d is None:
            continue
        if not isinstance(d, dict):
            d = {"enabled": bool(d)}
        norm_deals[name] = {
            "enabled": bool(d.get("enabled", True)),
            "prefilter": dict(d.get("prefilter") or {}),
        }
    for name in deals:
        if name not in DEALS:
            log.warning("Nieznany typ ogłoszeń w config.yaml: %r (dozwolone: %s)", name, ", ".join(DEALS))
    cfg["deals"] = norm_deals
    return cfg


def norm_text(s: Optional[str]) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s).lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("ł", "l")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_street(s: Optional[str]) -> str:
    t = norm_text(s)
    t = re.sub(r"\b(ul|ulica|al|aleja|pl|plac|os|osiedle)\b", " ", t)
    t = re.sub(r"\b\d+[a-z]?\b", " ", t)  # numery domów
    return re.sub(r"\s+", " ", t).strip()


# ---------------------------------------------------------------- scraping

def load_module(name: str):
    return importlib.import_module("scraper.sources.%s" % name)


def module_deals(module) -> Tuple[str, ...]:
    """Typy ogłoszeń obsługiwane przez moduł. Stare moduły (bez `deal`) = tylko sprzedaż."""
    if not accepts_deal(module):
        return ("sprzedaz",)
    deals = getattr(module, "DEALS", ("sprzedaz",))
    if isinstance(deals, str):
        deals = (deals,)
    return tuple(d for d in deals if d in DEALS)


def accepts_deal(module) -> bool:
    fn = getattr(module, "scrape", None)
    if fn is None:
        return False
    try:
        inspect.signature(fn).bind({}, "sprzedaz")
        return True
    except TypeError:
        return False
    except ValueError:  # brak sygnatury (np. funkcja w C) — zakładamy nowy kontrakt
        return True


def run_source(name: str, module, config: dict, deal: str) -> Tuple[List[dict], Optional[str]]:
    """Zwraca (lista ofert jako dict, komunikat błędu albo None)."""
    try:
        cfg = copy.deepcopy(config)
        cfg["prefilter"] = dict(config["deals"][deal]["prefilter"])
        cfg["deal"] = deal
        if accepts_deal(module):
            listings = module.scrape(cfg, deal)
        else:
            listings = module.scrape(cfg)  # stary kontrakt: tylko sprzedaż
        out = []
        for l in listings or []:
            try:
                d = l.to_dict() if hasattr(l, "to_dict") else dict(l)
            except Exception as e:  # pragma: no cover - defensywnie
                log.warning("[%s/%s] pomijam błędną ofertę: %s", name, deal, e)
                continue
            if not d.get("id") or not d.get("url"):
                continue
            d["id"] = str(d["id"])
            d["source"] = d.get("source") or name
            d["key"] = "%s:%s" % (d["source"], d["id"])
            d["deal"] = deal
            out.append(d)
        return out, None
    except Exception as e:
        log.error("[%s/%s] BŁĄD: %s\n%s", name, deal, e, traceback.format_exc())
        msg = "%s: %s" % (type(e).__name__, e)
        return [], msg[:500]


def _to_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def trim_description(desc: Any) -> Optional[str]:
    if not desc:
        return None
    desc = re.sub(r"\s+", " ", str(desc)).strip()
    if len(desc) > MAX_DESCRIPTION:
        desc = desc[:MAX_DESCRIPTION].rsplit(" ", 1)[0] + "…"
    return desc or None


def enrich_listing(d: dict) -> dict:
    """Uzupełnia cechy mieszkania z tekstu ogłoszenia (piętro, umeblowanie, stan,
    typ budynku, udogodnienia). Wołane PRZED przycięciem opisu, żeby widzieć cały tekst.
    Błąd w heurystyce nie może zepsuć przebiegu — wtedy zostaje oferta bez zmian."""
    try:
        return enrich(d)
    except Exception as e:  # pragma: no cover - defensywnie
        log.warning("cechy: nie udało się przeanalizować %s: %s", d.get("key"), e)
        return d


def _clean_features(d: dict) -> None:
    """Normalizuje pola cech: złe typy / nieznane wartości -> None, cechy -> krótka lista."""
    for k in ("floor_num", "floors_total"):
        v = d.get(k)
        if v is None:
            continue
        try:
            d[k] = int(v)
        except (TypeError, ValueError):
            d[k] = None
    if d.get("floors_total") is not None and d["floors_total"] < 0:
        d["floors_total"] = None
    if not isinstance(d.get("furnished"), bool):
        d["furnished"] = None
    if d.get("condition") not in CONDITIONS:
        d["condition"] = None
    if d.get("building_type") not in BUILDING_TYPES:
        d["building_type"] = None
    feats = d.get("features") or []
    seen: List[str] = []
    for f in feats if isinstance(feats, (list, tuple)) else []:
        if isinstance(f, str) and f and f not in seen:
            seen.append(f)
    d["features"] = seen


def clean_listing(d: dict) -> dict:
    """Przycina pola, żeby plik JSON był lekki."""
    imgs = [i for i in (d.get("images") or []) if isinstance(i, str) and i.startswith("http")]
    d["images"] = imgs[:MAX_IMAGES]
    d["description"] = trim_description(d.get("description"))
    _clean_features(d)
    for k in ("price", "rent_extra", "deposit"):
        if k in d:
            d[k] = _to_int(d.get(k))
    if d.get("area") is not None:
        try:
            d["area"] = round(float(d["area"]), 2)
        except (TypeError, ValueError):
            d["area"] = None
        if d["area"] is not None and d["area"] <= 0:
            d["area"] = None
    d.pop("price_per_m2", None)  # strona liczy sama
    # współrzędne z portalu = dokładne; bez nich pole geo nie nadpisuje stanu
    # (przybliżenia liczy geocode_listings po scaleniu)
    if d.get("lat") is not None and d.get("lon") is not None:
        d["geo"] = GEO_EXACT
    else:
        d.pop("geo", None)
    return d


# ---------------------------------------------------------------- merging

STATE_FIELDS = ("first_seen", "last_seen", "price_history", "active", "dup_group")


def merge(
    state: Dict[str, dict],
    scraped: Dict[str, List[dict]],
    ok_sources: Set[str],
    untouched_sources: Set[str],
    now: datetime,
    drop_after_days: int,
    partial_sources: Optional[Dict[str, int]] = None,
) -> Dict[str, dict]:
    """Scala nowe wyniki z poprzednim stanem (jednego typu ogłoszeń).

    ok_sources        – portale, które w tym przebiegu zadziałały poprawnie:
                        ich niewidziane oferty dostają active=False.
    untouched_sources – portale, które się nie powiodły / nie były uruchamiane
                        w tym przebiegu: ich oferty zostają bez zmian.
    Pozostałe niewidziane oferty (np. z portali wyłączonych w config.yaml)
    dostają active=False i po drop_after_days wypadają z listy.
    partial_sources   – {portal: dni} dla portali pobieranych tylko częściowo
                        (najnowsze strony): niewidziana oferta nie jest jeszcze
                        dowodem, że zniknęła, więc zostaje aktywna przez tyle dni
                        od ostatniego zobaczenia.
    """
    partial_sources = partial_sources or {}
    now_s = iso(now)
    seen: Set[str] = set()

    for src, items in scraped.items():
        for d in items:
            key = d["key"]
            if key in seen:
                continue  # duplikat w obrębie jednego portalu/przebiegu
            seen.add(key)
            d = clean_listing(enrich_listing(dict(d)))  # enrich widzi pełny opis
            prev = state.get(key)
            if prev is None:
                rec = d
                rec["first_seen"] = now_s
                rec["price_history"] = [{"date": now_s, "price": d["price"]}] if d.get("price") else []
            else:
                rec = dict(prev)
                for k, v in d.items():
                    # nowe niepuste wartości nadpisują stare; puste nie kasują starych
                    if v is None or v == "" or v == []:
                        continue
                    rec[k] = v
                hist = list(prev.get("price_history") or [])
                if d.get("price"):
                    last_price = hist[-1]["price"] if hist else None
                    if last_price != d["price"]:
                        hist.append({"date": now_s, "price": d["price"]})
                rec["price_history"] = hist[-MAX_PRICE_HISTORY:]
                rec["first_seen"] = prev.get("first_seen") or now_s
            rec["last_seen"] = now_s
            rec["active"] = True
            state[key] = rec

    cutoff = now - timedelta(days=drop_after_days)
    out: Dict[str, dict] = {}
    for key, rec in state.items():
        src = rec.get("source") or key.split(":", 1)[0]
        if key not in seen:
            if src in untouched_sources:
                out[key] = rec  # portal padł albo nie był uruchamiany — nic nie zmieniamy
                continue
            if src in partial_sources:
                last = parse_iso(rec.get("last_seen")) or now
                if last >= now - timedelta(days=int(partial_sources[src])):
                    out[key] = rec
                continue
            rec["active"] = False
        last = parse_iso(rec.get("last_seen")) or now
        if not rec.get("active") and last < cutoff:
            continue  # wypada z listy
        out[key] = rec
    return out


# ---------------------------------------------------------------- duplicates

def _title_tokens(t: Optional[str]) -> Set[str]:
    return {w for w in norm_text(t).split() if len(w) > 2}


def _floor_key(f: Optional[str]) -> str:
    """'3/5' -> '3', 'parter' -> '0', 'piętro 2' -> '2'."""
    t = norm_text(f)
    if not t:
        return ""
    if "parter" in t:
        return "0"
    m = re.search(r"-?\d+", t)
    return m.group(0).lstrip("0") or "0" if m else ""


def _district_compatible(a: str, b: str) -> Optional[bool]:
    """True = zgodne, False = sprzeczne, None = brak danych."""
    if not a or not b:
        return None
    return a == b or a in b or b in a


def pair_score(a: dict, b: dict, rent: bool = False) -> Optional[int]:
    """Zwraca liczbę „mocnych” zgodności albo None, gdy oferty na pewno różne.

    rent=True (wynajem/pokój): czynsze są okrągłe i często się powtarzają
    (np. 2500 zł), więc równa cena waży mniej, a różne opłaty dodatkowe wykluczają.
    """
    if a["source"] == b["source"]:
        return None
    pa, pb, aa, ab = a.get("price"), b.get("price"), a.get("area"), b.get("area")
    if not (pa and pb and aa and ab):
        return None
    if abs(aa - ab) > 0.5:
        return None
    if abs(pa - pb) > 0.01 * max(pa, pb):
        return None
    ra, rb = a.get("rooms"), b.get("rooms")
    if ra and rb and ra != rb:
        return None
    dc = _district_compatible(a["_district"], b["_district"])
    if dc is False:
        return None
    sa, sb = a["_street"], b["_street"]
    if sa and sb and not (sa == sb or sa in sb or sb in sa):
        return None
    if a.get("market") and b.get("market") and a["market"] != b["market"]:
        return None
    if rent:
        ea, eb = a.get("rent_extra"), b.get("rent_extra")
        if ea and eb and abs(ea - eb) > 0.05 * max(ea, eb):
            return None

    # Każdy sygnał zgodności daje punkty; łączymy dopiero przy MIN_SCORE.
    score = 0
    near = False
    if abs(aa - ab) <= 0.1:
        score += 2
    if pa == pb:
        score += 1 if rent else 2
    elif abs(pa - pb) <= 0.005 * max(pa, pb) and not rent:
        score += 1
    if ra and rb:
        score += 1
    if dc is True:
        score += 1
    if sa and sb:
        score += 2
    ta, tb = a["_tokens"], b["_tokens"]
    if ta and tb and len(ta & tb) / float(len(ta | tb)) >= 0.5:
        score += 1
    fa, fb = a["_floor"], b["_floor"]
    if fa and fb:
        if fa == fb:
            score += 1
        else:
            return None
    if a.get("build_year") and b.get("build_year"):
        if a["build_year"] == b["build_year"]:
            score += 1
        else:
            return None
    if (a.get("lat") and b.get("lat") and a.get("lon") and b.get("lon")
            and a.get("geo", GEO_EXACT) == GEO_EXACT and b.get("geo", GEO_EXACT) == GEO_EXACT):
        # tylko prawdziwe współrzędne z portali — przybliżone (ulica/osiedle) nic nie mówią
        dlat, dlon = abs(a["lat"] - b["lat"]), abs(a["lon"] - b["lon"])
        # ~1,5 km — współrzędne bywają przybliżone do środka dzielnicy
        if dlat > 0.015 or dlon > 0.022:
            return None
        if dlat < 0.0015 and dlon < 0.0022:  # ~150 m
            score += 1
            near = True
    if rent and not (near or (sa and sb)):
        # przy wynajmie same parametry (metraż, czynsz, pokoje) często się powtarzają —
        # bez zgodnej ulicy lub bliskich współrzędnych wymagamy dodatkowego punktu
        score -= 1
    return score


MIN_SCORE = 6
MIN_SCORE_RENT = 7  # wynajem: ostrożniej, dane są bardziej „szumiące”


def detect_duplicates(listings: List[dict], deal: str = "sprzedaz") -> None:
    """Ustawia rec['dup_group'] (str) dla grup ≥2 ofert z różnych portali, inaczej None.

    Konserwatywnie: dwie grupy łączymy tylko gdy KAŻDA para ofert z różnych portali
    między nimi pasuje (complete linkage), co zapobiega „łańcuszkom” fałszywych scaleń.
    """
    rent = deal in RENT_DEALS
    min_score = MIN_SCORE_RENT if rent else MIN_SCORE
    items = [l for l in listings if l.get("price") and l.get("area")]
    for l in listings:
        l["dup_group"] = None
    for l in items:
        l["_district"] = norm_text(l.get("district"))
        l["_street"] = norm_street(l.get("street"))
        l["_tokens"] = _title_tokens(l.get("title"))
        l["_floor"] = _floor_key(l.get("floor"))

    buckets: Dict[int, List[int]] = {}
    for i, l in enumerate(items):
        buckets.setdefault(int(l["area"]), []).append(i)

    scores: Dict[Tuple[int, int], int] = {}
    for i, l in enumerate(items):
        b = int(l["area"])
        for nb in (b - 1, b, b + 1):
            for j in buckets.get(nb, ()):
                if j <= i:
                    continue
                s = pair_score(l, items[j], rent)
                if s is not None and s >= min_score:
                    scores[(i, j)] = s

    group_of = list(range(len(items)))
    members: Dict[int, List[int]] = {i: [i] for i in range(len(items))}

    def ok_pair(i: int, j: int) -> bool:
        if items[i]["source"] == items[j]["source"]:
            return True  # ten sam portal — nie porównujemy (może być repost)
        return (min(i, j), max(i, j)) in scores

    for (i, j), _s in sorted(scores.items(), key=lambda kv: -kv[1]):
        gi, gj = group_of[i], group_of[j]
        if gi == gj:
            continue
        if not all(ok_pair(x, y) for x in members[gi] for y in members[gj]):
            continue
        # nie dopuszczamy dwóch ofert z tego samego portalu w jednej grupie
        src_i = {items[x]["source"] for x in members[gi]}
        src_j = {items[y]["source"] for y in members[gj]}
        if src_i & src_j:
            continue
        for y in members[gj]:
            group_of[y] = gi
        members[gi].extend(members.pop(gj))

    for gid, mem in members.items():
        if len(mem) < 2:
            continue
        keys = sorted(items[m]["key"] for m in mem)
        gname = "g" + hashlib.sha1(keys[0].encode("utf-8")).hexdigest()[:10]
        for m in mem:
            items[m]["dup_group"] = gname

    for l in items:
        for k in ("_district", "_street", "_tokens", "_floor"):
            l.pop(k, None)


# ---------------------------------------------------------------- state files

def load_state(deal: str) -> Dict[str, dict]:
    raw = load_json(deal_path(deal), None)
    if raw is None and deal == "sprzedaz":
        raw = load_json(LEGACY_LISTINGS_PATH, [])
    if isinstance(raw, dict):
        raw = raw.get("listings") or []
    state: Dict[str, dict] = {}
    for rec in raw or []:
        if isinstance(rec, dict) and rec.get("key"):
            rec["deal"] = deal
            if rec.get("description"):
                rec["description"] = trim_description(rec["description"])
            rec.pop("price_per_m2", None)
            state[rec["key"]] = rec
    return state


def migrate_legacy() -> None:
    """docs/data/listings.json (stary format, tylko sprzedaż) → docs/data/sprzedaz.json."""
    if not LEGACY_LISTINGS_PATH.exists():
        return
    if not deal_path("sprzedaz").exists():
        state = load_state("sprzedaz")
        save_state("sprzedaz", list(state.values()))
        log.info("Przeniesiono %d ofert z listings.json do sprzedaz.json", len(state))
    try:
        LEGACY_LISTINGS_PATH.unlink()
    except OSError as e:
        log.warning("Nie udało się usunąć %s: %s", LEGACY_LISTINGS_PATH, e)


FIELD_ORDER = (
    "key", "source", "id", "deal", "url", "title", "price", "rent_extra", "deposit", "available_from",
    "area", "rooms", "floor", "floor_num", "floors_total", "furnished", "condition",
    "building_type", "features", "district", "street", "market", "seller", "build_year",
    "images", "description", "lat", "lon", "geo", "created_at",
) + STATE_FIELDS


def compact_record(r: dict) -> dict:
    """Stała kolejność pól (mniejsze diffy w git), bez pustych pól (mniejszy plik)."""
    out = {}
    for k in FIELD_ORDER:
        v = r.get(k)
        if v is None or v == "" or (v == [] and k != "price_history"):
            continue
        out[k] = v
    for k in sorted(r):
        if k not in out and k not in FIELD_ORDER and not k.startswith("_"):
            v = r[k]
            if v is None or v == "" or v == []:
                continue
            out[k] = v
    out["active"] = bool(r.get("active"))
    return out


def save_state(deal: str, listings: List[dict]) -> None:
    listings = sorted(listings, key=lambda r: (r.get("first_seen") or "", r.get("key")), reverse=True)
    atomic_write_json(deal_path(deal), [compact_record(r) for r in listings], compact=True)


# ---------------------------------------------------------------- main

def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Scraper ofert mieszkań we Wrocławiu (kupno, wynajem, pokoje)")
    ap.add_argument("--max-pages", type=int, default=None, help="nadpisuje max_pages z config.yaml")
    ap.add_argument("--sources", default=None,
                    help="lista portali po przecinku (np. otodom,olx); pozostałe nie są ruszane")
    ap.add_argument("--deals", default=None,
                    help="typy ogłoszeń po przecinku (%s); pozostałe nie są ruszane" % ",".join(DEALS))
    ap.add_argument("--config", default=str(CONFIG_PATH))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    config = load_config(Path(args.config))
    if args.max_pages is not None:
        config["max_pages"] = args.max_pages

    configured = {k: bool(v) for k, v in (config.get("sources") or {}).items()}
    if args.sources:
        to_run = [s.strip() for s in args.sources.split(",") if s.strip()]
    else:
        to_run = [k for k, v in configured.items() if v]

    enabled_deals = [d for d in DEALS if d in config["deals"] and config["deals"][d]["enabled"]]
    if args.deals:
        wanted = [d.strip() for d in args.deals.split(",") if d.strip()]
        bad = [d for d in wanted if d not in DEALS]
        if bad:
            ap.error("nieznany typ ogłoszeń: %s (dozwolone: %s)" % (", ".join(bad), ", ".join(DEALS)))
        for d in wanted:
            config["deals"].setdefault(d, {"enabled": True, "prefilter": {}})
        deals_to_run = [d for d in DEALS if d in wanted]
    else:
        deals_to_run = enabled_deals

    migrate_legacy()

    start = utc_now()
    prev_meta = load_json(META_PATH, {}) or {}
    if not isinstance(prev_meta, dict):
        prev_meta = {}
    prev_deals_meta = prev_meta.get("deals") or {}
    if not prev_deals_meta and prev_meta.get("sources"):
        # stary meta.json: statusy dotyczyły sprzedaży
        prev_deals_meta = {"sprzedaz": {"sources": prev_meta.get("sources") or {},
                                        "first_run": prev_meta.get("first_run")}}

    # --- które (portal, typ) uruchamiamy
    modules: Dict[str, Any] = {}
    import_errors: Dict[str, str] = {}
    plan: Dict[str, List[str]] = {}
    for name in to_run:
        try:
            modules[name] = load_module(name)
        except Exception as e:
            log.error("[%s] nie można wczytać modułu: %s", name, e)
            import_errors[name] = ("%s: %s" % (type(e).__name__, e))[:500]
            plan[name] = list(deals_to_run)  # oznaczymy jako błąd w każdym typie
            continue
        supported = module_deals(modules[name])
        plan[name] = [d for d in deals_to_run if d in supported]
        skipped = [d for d in deals_to_run if d not in supported]
        if skipped:
            log.info("[%s] nie obsługuje: %s", name, ", ".join(skipped))

    # --- pobieranie: portale równolegle, typy jednego portalu po kolei
    results: Dict[Tuple[str, str], Tuple[List[dict], Optional[str], float, str]] = {}

    def run_all_deals(name: str) -> None:
        for i, deal in enumerate(plan[name]):
            if name in import_errors:
                results[(name, deal)] = ([], import_errors[name], 0.0, iso(utc_now()))
                continue
            if i:
                time.sleep(PAUSE_BETWEEN_DEALS)
            t0 = time.time()
            log.info("[%s/%s] start", name, deal)
            items, err = run_source(name, modules[name], config, deal)
            dt = round(time.time() - t0, 1)
            results[(name, deal)] = (items, err, dt, iso(utc_now()))
            log.info("[%s/%s] %s: %d ofert w %.1fs", name, deal, "BŁĄD" if err else "ok", len(items), dt)

    names = [n for n in to_run if plan.get(n)]
    if names:
        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            list(pool.map(run_all_deals, names))

    # --- scalanie, osobno dla każdego typu
    deals_meta: Dict[str, dict] = {}
    for deal, prev in prev_deals_meta.items():
        if deal in DEALS and isinstance(prev, dict):
            deals_meta[deal] = prev
    all_failed: List[str] = []

    for deal in deals_to_run:
        prev_dm = prev_deals_meta.get(deal) or {}
        prev_src_meta = prev_dm.get("sources") or {}
        state = load_state(deal)

        scraped: Dict[str, List[dict]] = {}
        src_meta: Dict[str, dict] = {}
        ok_sources: Set[str] = set()
        failed: Set[str] = set()

        for name in to_run:
            if (name, deal) not in results:
                continue
            items, err, dt, finished = results[(name, deal)]
            prev = prev_src_meta.get(name) or {}
            info = {"count": len(items), "duration_s": dt, "last_run": finished}
            if err:
                info.update(status="error", error=err, last_ok=prev.get("last_ok"))
                failed.add(name)
            elif not items:
                # 0 ofert bez wyjątku to prawie na pewno blokada / zmiana strony —
                # nie wygaszamy wtedy starych ofert.
                info.update(status="error",
                            error="Portal nie zwrócił żadnych ofert (możliwa blokada lub zmiana strony).",
                            last_ok=prev.get("last_ok"))
                failed.add(name)
            else:
                info.update(status="ok", error=None, last_ok=finished)
                ok_sources.add(name)
                ok_sources.update(d["source"] for d in items)
                scraped[name] = items
            # pierwszy udany przebieg: oferty z niego to „stan wyjściowy”, nie nowości
            info["first_ok"] = (prev.get("first_ok")
                                or (prev_dm.get("first_run") if prev.get("last_ok") else None)
                                or (iso(start) if info["status"] == "ok" else None))
            src_meta[name] = info

        # portale nieuruchamiane w tym przebiegu — w meta zostaje poprzedni stan
        selective = bool(args.sources)
        for name, prev in prev_src_meta.items():
            if name not in src_meta and (selective or configured.get(name)):
                src_meta[name] = prev

        known_sources = {r.get("source") for r in state.values()}
        if selective:
            untouched = (known_sources - ok_sources) | failed
        else:
            untouched = set(failed)

        merged = merge(state, scraped, ok_sources, untouched, start,
                       int(config.get("drop_after_days") or 14),
                       config.get("partial_sources") or {})
        listings = list(merged.values())
        for r in listings:
            r["deal"] = deal
        detect_duplicates(listings, deal)
        try:
            geo_stats = geocode_listings(listings)
            log.info("[%s] lokalizacje: %s", deal,
                     ", ".join("%s=%d" % kv for kv in sorted(geo_stats.items())))
        except Exception as e:  # geokodowanie jest dodatkiem — nie może zepsuć przebiegu
            log.warning("[%s] geokodowanie nie powiodło się: %s", deal, e)
            geo_stats = {}
        save_state(deal, listings)

        n_active = sum(1 for r in listings if r.get("active"))
        n_groups = len({r["dup_group"] for r in listings if r.get("dup_group")})
        deals_meta[deal] = {
            "label": DEAL_LABELS[deal],
            "file": deal_path(deal).name,
            "first_run": prev_dm.get("first_run") or iso(start),
            "last_run": iso(start),
            "total": len(listings),
            "active": n_active,
            "dup_groups": n_groups,
            "geo": dict(sorted(geo_stats.items())),
            "sources": dict(sorted(src_meta.items())),
        }
        all_failed.extend("%s/%s" % (n, deal) for n in sorted(failed))
        log.info("[%s] zapisano %d ofert (%d aktywnych, %d grup duplikatów)",
                 deal, len(listings), n_active, n_groups)

    # typy wyłączone w config.yaml nie pokazują się na stronie
    shown_deals = [d for d in DEALS if d in enabled_deals or d in deals_to_run]
    meta = {
        "last_run": iso(start),
        "first_run": prev_meta.get("first_run") or iso(start),
        "finished": iso(utc_now()),
        "drop_after_days": config.get("drop_after_days"),
        "enabled_deals": shown_deals,
        "deals": {d: deals_meta[d] for d in DEALS if d in deals_meta},
    }
    atomic_write_json(META_PATH, meta)
    log.info("Gotowe. Błędy: %s", ", ".join(all_failed) or "brak")
    return 0


if __name__ == "__main__":
    sys.exit(main())
