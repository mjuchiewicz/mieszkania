"""Punkt wejścia: `python -m scraper.main`.

Uruchamia wszystkie włączone scrapery, scala wyniki z poprzednim stanem
(docs/data/listings.json), wykrywa duplikaty między portalami i zapisuje
docs/data/meta.json ze statusem każdego portalu.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
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

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
DATA_DIR = ROOT / "docs" / "data"
LISTINGS_PATH = DATA_DIR / "listings.json"
META_PATH = DATA_DIR / "meta.json"

MAX_IMAGES = 6
MAX_DESCRIPTION = 600
MAX_PRICE_HISTORY = 50

log = logging.getLogger("scraper")


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
    cfg.setdefault("prefilter", {})
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

def run_source(name: str, config: dict) -> Tuple[List[dict], Optional[str]]:
    """Zwraca (lista ofert jako dict, komunikat błędu albo None)."""
    try:
        module = importlib.import_module("scraper.sources.%s" % name)
        listings = module.scrape(copy.deepcopy(config))
        out = []
        for l in listings or []:
            try:
                d = l.to_dict() if hasattr(l, "to_dict") else dict(l)
            except Exception as e:  # pragma: no cover - defensywnie
                log.warning("[%s] pomijam błędną ofertę: %s", name, e)
                continue
            if not d.get("id") or not d.get("url"):
                continue
            d["source"] = d.get("source") or name
            d["key"] = "%s:%s" % (d["source"], d["id"])
            out.append(d)
        return out, None
    except Exception as e:
        log.error("[%s] BŁĄD: %s\n%s", name, e, traceback.format_exc())
        msg = "%s: %s" % (type(e).__name__, e)
        return [], msg[:500]


def clean_listing(d: dict) -> dict:
    """Przycina pola, żeby plik JSON był lekki."""
    imgs = [i for i in (d.get("images") or []) if isinstance(i, str) and i.startswith("http")]
    d["images"] = imgs[:MAX_IMAGES]
    desc = d.get("description")
    if desc:
        desc = re.sub(r"\s+", " ", str(desc)).strip()
        if len(desc) > MAX_DESCRIPTION:
            desc = desc[:MAX_DESCRIPTION].rsplit(" ", 1)[0] + "…"
        d["description"] = desc
    if d.get("price") is not None:
        try:
            d["price"] = int(round(float(d["price"])))
        except (TypeError, ValueError):
            d["price"] = None
        if d["price"] is not None and d["price"] <= 0:
            d["price"] = None
    if d.get("area") is not None:
        try:
            d["area"] = round(float(d["area"]), 2)
        except (TypeError, ValueError):
            d["area"] = None
        if d["area"] is not None and d["area"] <= 0:
            d["area"] = None
    if d.get("price") and d.get("area"):
        d["price_per_m2"] = round(d["price"] / d["area"])
    else:
        d["price_per_m2"] = None
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
) -> Dict[str, dict]:
    """Scala nowe wyniki z poprzednim stanem.

    ok_sources        – portale, które w tym przebiegu zadziałały poprawnie:
                        ich niewidziane oferty dostają active=False.
    untouched_sources – portale, które się nie powiodły / nie były uruchamiane
                        w tym przebiegu: ich oferty zostają bez zmian.
    Pozostałe niewidziane oferty (np. z portali wyłączonych w config.yaml)
    dostają active=False i po drop_after_days wypadają z listy.
    """
    now_s = iso(now)
    seen: Set[str] = set()

    for src, items in scraped.items():
        for d in items:
            key = d["key"]
            if key in seen:
                continue  # duplikat w obrębie jednego portalu/przebiegu
            seen.add(key)
            d = clean_listing(dict(d))
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


def pair_score(a: dict, b: dict) -> Optional[int]:
    """Zwraca liczbę „mocnych” zgodności albo None, gdy oferty na pewno różne."""
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

    # Każdy sygnał zgodności daje punkty; łączymy dopiero przy MIN_SCORE.
    score = 0
    if abs(aa - ab) <= 0.1:
        score += 2
    if pa == pb:
        score += 2
    elif abs(pa - pb) <= 0.005 * max(pa, pb):
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
    if a.get("lat") and b.get("lat") and a.get("lon") and b.get("lon"):
        dlat, dlon = abs(a["lat"] - b["lat"]), abs(a["lon"] - b["lon"])
        # ~1,5 km — współrzędne bywają przybliżone do środka dzielnicy
        if dlat > 0.015 or dlon > 0.022:
            return None
        if dlat < 0.0015 and dlon < 0.0022:  # ~150 m
            score += 1
    return score


MIN_SCORE = 6


def detect_duplicates(listings: List[dict]) -> None:
    """Ustawia rec['dup_group'] (str) dla grup ≥2 ofert z różnych portali, inaczej None.

    Konserwatywnie: dwie grupy łączymy tylko gdy KAŻDA para ofert z różnych portali
    między nimi pasuje (complete linkage), co zapobiega „łańcuszkom” fałszywych scaleń.
    """
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
                s = pair_score(l, items[j])
                if s is not None and s >= MIN_SCORE:
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


# ---------------------------------------------------------------- main

def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Scraper mieszkań na sprzedaż we Wrocławiu")
    ap.add_argument("--max-pages", type=int, default=None, help="nadpisuje max_pages z config.yaml")
    ap.add_argument("--sources", default=None,
                    help="lista portali po przecinku (np. otodom,olx); pozostałe nie są ruszane")
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
    enabled = [k for k, v in configured.items() if v]
    if args.sources:
        to_run = [s.strip() for s in args.sources.split(",") if s.strip()]
    else:
        to_run = enabled

    start = utc_now()
    prev_meta = load_json(META_PATH, {}) or {}
    prev_src_meta = prev_meta.get("sources") or {} if isinstance(prev_meta, dict) else {}

    raw_state = load_json(LISTINGS_PATH, [])
    if isinstance(raw_state, dict):
        raw_state = raw_state.get("listings") or []
    state: Dict[str, dict] = {}
    for rec in raw_state:
        if isinstance(rec, dict) and rec.get("key"):
            state[rec["key"]] = rec

    scraped: Dict[str, List[dict]] = {}
    src_meta: Dict[str, dict] = {}
    ok_sources: Set[str] = set()
    failed: Set[str] = set()

    # Portale to różne serwery, więc pobieramy je równolegle.
    def timed_run(name: str) -> Tuple[List[dict], Optional[str], float]:
        t0 = time.time()
        log.info("[%s] start", name)
        items, err = run_source(name, config)
        return items, err, round(time.time() - t0, 1)

    with ThreadPoolExecutor(max_workers=max(1, len(to_run))) as pool:
        results = dict(zip(to_run, pool.map(timed_run, to_run)))

    for name in to_run:
        items, err, dt = results[name]
        prev = prev_src_meta.get(name) or {}
        info = {"count": len(items), "duration_s": dt, "last_run": iso(utc_now())}
        if err:
            info.update(status="error", error=err, last_ok=prev.get("last_ok"))
            failed.add(name)
        elif not items:
            # 0 ofert bez wyjątku to prawie na pewno blokada / zmiana strony —
            # nie wygaszamy wtedy starych ofert.
            info.update(status="error", error="Portal nie zwrócił żadnych ofert (możliwa blokada lub zmiana strony).",
                        last_ok=prev.get("last_ok"))
            failed.add(name)
        else:
            info.update(status="ok", error=None, last_ok=info["last_run"])
            ok_sources.add(name)
            ok_sources.update(d["source"] for d in items)
            scraped[name] = items
        src_meta[name] = info
        log.info("[%s] %s: %d ofert w %.1fs", name, info["status"], len(items), dt)

    # portale nieuruchamiane w tym przebiegu (np. --sources) — zostawiamy w meta poprzedni stan
    if args.sources:
        for name, prev in prev_src_meta.items():
            if name not in src_meta:
                src_meta[name] = prev

    known_sources = {r.get("source") for r in state.values()}
    if args.sources:
        # ręczny wybór portali: wszystkie pozostałe zostają nietknięte
        untouched = (known_sources - ok_sources) | failed
    else:
        untouched = set(failed)

    merged = merge(state, scraped, ok_sources, untouched, start,
                   int(config.get("drop_after_days") or 14))

    listings = list(merged.values())
    detect_duplicates(listings)
    listings.sort(key=lambda r: (r.get("first_seen") or "", r.get("key")), reverse=True)

    # stała kolejność pól w rekordzie = mniejsze diffy w git
    for r in listings:
        for f in STATE_FIELDS:
            r.setdefault(f, None)

    atomic_write_json(LISTINGS_PATH, listings, compact=True)

    n_active = sum(1 for r in listings if r.get("active"))
    n_groups = len({r["dup_group"] for r in listings if r.get("dup_group")})
    meta = {
        "last_run": iso(start),
        "first_run": (prev_meta.get("first_run") if isinstance(prev_meta, dict) else None) or iso(start),
        "finished": iso(utc_now()),
        "total": len(listings),
        "active": n_active,
        "dup_groups": n_groups,
        "drop_after_days": config.get("drop_after_days"),
        "sources": dict(sorted(src_meta.items())),
    }
    atomic_write_json(META_PATH, meta)
    log.info("Zapisano %d ofert (%d aktywnych, %d grup duplikatów). Błędy: %s",
             len(listings), n_active, n_groups, ", ".join(sorted(failed)) or "brak")
    return 0


if __name__ == "__main__":
    sys.exit(main())
