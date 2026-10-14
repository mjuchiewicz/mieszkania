"""OLX z komputera: `python -m scraper.feed …`.

OLX blokuje (HTTP 403) serwery GitHub Actions, ale działa z domowego internetu.
Dlatego komputer (macOS) co godzinę, gdy jest włączony, pobiera OLX i wysyła wyniki
do repozytorium jako pliki feeds/olx/<miejscowość>/<typ>.json. Przebieg w chmurze
(scraper/main.py, `feeds:` w config.yaml) czyta te pliki zamiast łączyć się z OLX.

Polecenia:
  install    – instaluje zadanie w tle (LaunchAgent pl.mieszkania.olx) i od razu je uruchamia
  uninstall  – wyłącza zadanie i usuwa jego pliki (kopię repozytorium, środowisko Pythona)
  status     – czy zadanie działa, kiedy ostatnio wysłało dane, ostatnie linie logu
  now        – uruchom zadanie teraz (także na baterii)
  run        – jeden przebieg (to wywołuje launchd); z --no-git tylko zapisuje pliki lokalnie

Bezpieczeństwo: zadanie w tle działa na OSOBNEJ kopii repozytorium w
~/Library/Application Support/mieszkania-olx/repo (nie dotyka folderu, w którym pracujesz),
commituje wyłącznie pliki feeds/<portal>/, robi fetch + rebase na origin/main i nigdy
nie używa force-push. Log: ~/Library/Logs/mieszkania-olx.log.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from scraper.main import (
    FEEDS_DIR,
    FIELD_ORDER,
    PAUSE_BETWEEN_DEALS,
    ROOT,
    feed_sources,
    iso,
    load_config,
    load_module,
    module_deals,
    parse_iso,
    prepare_listing,
    run_source,
    utc_now,
)
from scraper.models import DEALS

REPO_URL = "https://github.com/mjuchiewicz/mieszkania.git"
BRANCH = "main"
LABEL = "pl.mieszkania.olx"
HOME = Path.home()
APP_DIR = HOME / "Library" / "Application Support" / "mieszkania-olx"
REPO_DIR = APP_DIR / "repo"
VENV_DIR = APP_DIR / "venv"
LOCK_PATH = APP_DIR / "run.lock"
STATE_PATH = APP_DIR / "state.json"
FORCE_FLAG = APP_DIR / "force-next-run"
PLIST_PATH = HOME / "Library" / "LaunchAgents" / ("%s.plist" % LABEL)
LOG_PATH = HOME / "Library" / "Logs" / "mieszkania-olx.log"
PATH_ENV = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

INTERVAL_S = 3600
MIN_GAP_S = 40 * 60          # nie częściej (np. RunAtLoad przy logowaniu tuż po przebiegu)
LOG_MAX_BYTES = 2_000_000
PUSH_TRIES = 4

log = logging.getLogger("feed")


class GitError(RuntimeError):
    pass


# ---------------------------------------------------------------- pliki feed

def feed_record(d: dict) -> dict:
    """Stała kolejność pól, bez pustych (mniejszy plik i czytelne diffy)."""
    out = {}
    for k in FIELD_ORDER:
        v = d.get(k)
        if v is None or v == "" or v == []:
            continue
        out[k] = v
    for k in sorted(d):
        if k in out or k in FIELD_ORDER or k.startswith("_"):
            continue
        v = d[k]
        if v is None or v == "" or v == []:
            continue
        out[k] = v
    out.pop("active", None)
    return out


def write_feed(path: Path, header: dict, listings: List[dict]) -> None:
    """JSON z jedną ofertą na linię (atomowo)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    head = json.dumps(header, ensure_ascii=False, separators=(",", ":"))
    lines = [json.dumps(r, ensure_ascii=False, separators=(",", ":")) for r in listings]
    text = head[:-1] + ',"listings":[' + ("\n" + ",\n".join(lines) + "\n" if lines else "") + "]}\n"
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o644)
    os.replace(str(tmp), str(path))


def scrape_to_feeds(sources: Sequence[str], out_dir: Path, config: dict,
                    cities_filter: Optional[List[str]] = None,
                    deals_filter: Optional[List[str]] = None) -> Dict[str, int]:
    """Pobiera portale dla wszystkich miejscowości × typów z config.yaml i zapisuje
    <out_dir>/<portal>/<miejscowość>/<typ>.json. Zwraca {"ok": n, "error": n}."""
    from scraper.cities import ensure_cities, portal_location

    # bez ustalania nowych miejscowości (to robi chmura — tu niczego poza feeds/ nie zmieniamy)
    cities = ensure_cities(config, resolve_missing=False, build_geo=False)
    if cities_filter:
        cities = [c for c in cities if c["slug"] in cities_filter]
    deals = [d for d in DEALS if d in config["deals"] and config["deals"][d]["enabled"]]
    if deals_filter:
        deals = [d for d in deals if d in deals_filter]
    stats = {"ok": 0, "error": 0}
    first = True
    for name in sources:
        module = load_module(name)
        supported = module_deals(module)
        for city in cities:
            loc = portal_location(city, name)
            if loc is None:
                log.info("[%s/%s] portal nie zna tej miejscowości — pomijam", name, city["slug"])
                continue
            for deal in deals:
                if deal not in supported:
                    continue
                if not first:
                    time.sleep(PAUSE_BETWEEN_DEALS)
                first = False
                t0 = time.time()
                items, err = run_source(name, module, config, deal, loc)
                listings: List[dict] = []
                seen = set()
                for d in items:
                    if d["key"] in seen:
                        continue
                    seen.add(d["key"])
                    listings.append(feed_record(prepare_listing(d)))
                listings.sort(key=lambda r: r["key"])
                header = {
                    "source": name, "city": city["slug"], "deal": deal,
                    "scraped_at": iso(utc_now()), "duration_s": round(time.time() - t0, 1),
                    "status": "error" if err else "ok", "error": err, "count": len(listings),
                }
                write_feed(out_dir / name / city["slug"] / ("%s.json" % deal), header, listings)
                stats["error" if err else "ok"] += 1
                log.info("[%s/%s/%s] %s: %d ofert w %.0fs", name, city["slug"], deal,
                         "BŁĄD" if err else "ok", len(listings), header["duration_s"])
    return stats


# ---------------------------------------------------------------- warunki przebiegu

def on_battery_or_low_power() -> Optional[str]:
    try:
        batt = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=10).stdout
        if "Battery Power" in batt:
            return "zasilanie z baterii"
        pm = subprocess.run(["pmset", "-g"], capture_output=True, text=True, timeout=10).stdout
        for line in pm.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "lowpowermode" and parts[1] == "1":
                return "tryb niskiego zużycia energii"
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def online(hosts=("github.com", "www.olx.pl")) -> bool:
    for h in hosts:
        try:
            with socket.create_connection((h, 443), timeout=8):
                pass
        except OSError:
            return False
    return True


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(str(tmp), str(STATE_PATH))


def trim_log() -> None:
    try:
        if LOG_PATH.stat().st_size > LOG_MAX_BYTES:
            with open(LOG_PATH, "rb") as f:
                f.seek(-LOG_MAX_BYTES // 4, os.SEEK_END)
                tail = f.read()
            with open(LOG_PATH, "r+b") as f:
                f.seek(0)
                f.write(tail)
                f.truncate()
    except OSError:
        pass


# ---------------------------------------------------------------- git (tylko osobna kopia repozytorium)

def git(*args: str, check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    p = subprocess.run([shutil.which("git", path=PATH_ENV) or "git", "-C", str(REPO_DIR)] + list(args),
                       capture_output=True, text=True, timeout=timeout, env=env)
    if check and p.returncode != 0:
        raise GitError("git %s: %s" % (" ".join(args[:2]), (p.stderr or p.stdout).strip()[-400:]))
    return p


def is_dedicated_clone() -> bool:
    try:
        return ROOT.resolve() == REPO_DIR.resolve() and (REPO_DIR / ".git").exists()
    except OSError:
        return False


def sync_with_origin() -> None:
    """Kopia robocza = origin/main (+ ewentualny niewypchnięty commit z feeds/)."""
    if (REPO_DIR / ".git" / "rebase-merge").exists() or (REPO_DIR / ".git" / "rebase-apply").exists():
        git("rebase", "--abort", check=False)
    if git("status", "--porcelain").stdout.strip():
        # pozostałość po przerwanym przebiegu — to nasza prywatna kopia, więc ją sprzątamy
        log.info("git: sprzątam niezatwierdzone zmiany w kopii zadania")
        git("reset", "--hard", "-q", "HEAD")
        git("clean", "-fdq", "--", "feeds")
    if git("symbolic-ref", "--short", "HEAD", check=False).stdout.strip() != BRANCH:
        git("checkout", "-q", BRANCH)
    git("fetch", "-q", "origin", BRANCH)
    if git("merge-base", "--is-ancestor", "HEAD", "origin/%s" % BRANCH, check=False).returncode == 0:
        git("merge", "-q", "--ff-only", "origin/%s" % BRANCH)
        return
    if git("rebase", "-q", "origin/%s" % BRANCH, check=False).returncode != 0:
        git("rebase", "--abort", check=False)
        # niewypchnięty stary feed i tak zastąpimy świeżym — wracamy do stanu z GitHuba
        log.warning("git: rebase się nie udał — wracam do origin/%s (stary niewysłany feed pominięty)", BRANCH)
        git("reset", "--hard", "-q", "origin/%s" % BRANCH)


def commit_and_push(sources: Sequence[str]) -> bool:
    paths = ["feeds/%s" % s for s in sources]
    git("add", "-A", "--sparse", "--", *paths)
    staged = git("diff", "--cached", "--name-only").stdout.split()
    stray = [p for p in staged if not any(p.startswith(x + "/") for x in paths)]
    if stray:
        raise GitError("w commicie znalazłyby się inne pliki niż feeds/: %s" % ", ".join(stray[:5]))
    if not staged:
        log.info("git: brak zmian w danych")
        return False
    names = ", ".join(s.upper() if s == "olx" else s for s in sources)
    git("commit", "-q", "-m", "%s z komputera: aktualizacja ofert %s UTC"
        % (names, utc_now().strftime("%Y-%m-%d %H:%M")), "--", *paths)
    for i in range(PUSH_TRIES):
        p = git("push", "-q", "origin", "HEAD:refs/heads/%s" % BRANCH, check=False, timeout=180)
        if p.returncode == 0:
            log.info("git: wysłano %s", git("rev-parse", "--short", "HEAD").stdout.strip())
            return True
        log.info("git: push odrzucony (%s) — fetch + rebase, próba %d/%d",
                 (p.stderr or "").strip().splitlines()[-1:] or "?", i + 2, PUSH_TRIES)
        time.sleep(5 * (i + 1))
        git("fetch", "-q", "origin", BRANCH)
        if git("rebase", "-q", "origin/%s" % BRANCH, check=False).returncode != 0:
            git("rebase", "--abort", check=False)
            raise GitError("rebase na origin/%s się nie udał" % BRANCH)
    raise GitError("nie udało się wypchnąć zmian po %d próbach" % PUSH_TRIES)


# ---------------------------------------------------------------- run

def cmd_run(args) -> int:
    use_git = not args.no_git
    if use_git and not is_dedicated_clone():
        log.error("Tryb z wysyłaniem działa tylko w kopii zadania (%s). Zainstaluj: python -m scraper.feed install; "
                  "lokalnie bez wysyłania: python -m scraper.feed run --no-git", REPO_DIR)
        return 2
    APP_DIR.mkdir(parents=True, exist_ok=True)
    trim_log()
    lock_f = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log.info("Poprzedni przebieg jeszcze trwa — pomijam")
        return 0

    force = args.force or FORCE_FLAG.exists()
    if FORCE_FLAG.exists():
        try:
            FORCE_FLAG.unlink()
        except OSError:
            pass
    state = load_state()
    cfg_path = ROOT / "config.yaml"
    config = load_config(cfg_path)
    fed = feed_sources(config)
    sources = args.sources.split(",") if args.sources else (sorted(fed) or ["olx"])
    opts = fed.get(sources[0]) or {}

    if not force:
        if not opts.get("on_battery", False):
            why = on_battery_or_low_power()
            if why:
                log.info("Pomijam: %s", why)
                return 0
        last = parse_iso(state.get("last_ok"))
        if last is not None and (utc_now() - last).total_seconds() < MIN_GAP_S:
            log.info("Pomijam: ostatnie dane wysłane %s", state.get("last_ok"))
            return 0
    if not online():
        log.info("Pomijam: brak internetu")
        return 0

    t0 = time.time()
    try:
        if use_git:
            sync_with_origin()
            config = load_config(cfg_path)  # po aktualizacji z GitHuba
        if args.max_pages is not None:
            config["max_pages"] = args.max_pages
        out_dir = Path(args.out) if args.out else FEEDS_DIR
        stats = scrape_to_feeds(sources, out_dir, config,
                                args.cities.split(",") if args.cities else None,
                                args.deals.split(",") if args.deals else None)
        if stats["error"] and not stats["ok"] and not online():
            log.info("Internet zniknął w trakcie — nic nie wysyłam")
            if use_git:
                git("checkout", "-q", "--", "feeds")
            return 0
        pushed = commit_and_push(sources) if use_git else False
    except (GitError, subprocess.SubprocessError, OSError) as e:
        log.error("Przerwano: %s", e)
        if use_git:
            git("rebase", "--abort", check=False)
        if use_git:
            state.update(last_error=str(e)[:300], last_error_at=iso(utc_now()))
            save_state(state)
        return 1
    if use_git:
        state.update(last_ok=iso(utc_now()), last_stats=stats, last_pushed=pushed, last_error=None)
        save_state(state)
    log.info("Gotowe w %.0fs: %d ok, %d błędów%s", time.time() - t0, stats["ok"], stats["error"],
             ", wysłano" if pushed else "")
    return 0


# ---------------------------------------------------------------- install / uninstall

def _sh(cmd: List[str], check: bool = True, **kw) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if check and p.returncode != 0:
        raise SystemExit("Błąd: %s\n%s" % (" ".join(cmd[:3]), (p.stderr or p.stdout).strip()[-800:]))
    return p


def _domain() -> str:
    return "gui/%d" % os.getuid()


def _base_python() -> str:
    for cand in (Path(sys.base_prefix) / "bin" / "python3", Path("/usr/bin/python3")):
        if cand.exists():
            return str(cand)
    return sys.executable


def cmd_install(args) -> int:
    git_bin = shutil.which("git", path=PATH_ENV) or "git"
    gh = shutil.which("gh", path=PATH_ENV)
    APP_DIR.mkdir(parents=True, exist_ok=True)
    if not (REPO_DIR / ".git").exists():
        print("Pobieram kopię repozytorium do %s …" % REPO_DIR)
        # częściowa kopia: bez docs/ (duże dane) — zadanie potrzebuje tylko kodu i feeds/
        _sh([git_bin, "clone", "-q", "--filter=blob:none", "--sparse", "--branch", BRANCH, REPO_URL, str(REPO_DIR)])
        _sh([git_bin, "-C", str(REPO_DIR), "sparse-checkout", "set", "scraper", "feeds"])
    else:
        _sh([git_bin, "-C", str(REPO_DIR), "pull", "-q", "--ff-only", "origin", BRANCH], check=False)
    if gh:
        # logowanie przez `gh auth login` (token w pęku kluczy) — tylko w tej kopii
        _sh([git_bin, "-C", str(REPO_DIR), "config", "--replace-all", "credential.https://github.com.helper", ""])
        _sh([git_bin, "-C", str(REPO_DIR), "config", "--add", "credential.https://github.com.helper",
             "!%s auth git-credential" % gh])
    py = VENV_DIR / "bin" / "python"
    if not py.exists():
        print("Tworzę środowisko Pythona w %s …" % VENV_DIR)
        _sh([_base_python(), "-m", "venv", str(VENV_DIR)])
    _sh([str(py), "-m", "pip", "install", "-q", "--disable-pip-version-check", "-r",
         str(REPO_DIR / "requirements.txt")])
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [str(py), "-m", "scraper.feed", "run"],
        "WorkingDirectory": str(REPO_DIR),
        "StartInterval": INTERVAL_S,
        "RunAtLoad": True,
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
        "EnvironmentVariables": {"PATH": PATH_ENV, "PYTHONUNBUFFERED": "1", "LANG": "pl_PL.UTF-8"},
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
    }
    with open(PLIST_PATH, "wb") as f:
        plistlib.dump(plist, f)
    _sh(["launchctl", "bootout", "%s/%s" % (_domain(), LABEL)], check=False)
    time.sleep(1)
    FORCE_FLAG.touch()  # pierwszy przebieg od razu, nawet na baterii
    _sh(["launchctl", "bootstrap", _domain(), str(PLIST_PATH)])
    print("Zainstalowano: %s\nPierwszy przebieg właśnie ruszył — log: %s" % (PLIST_PATH, LOG_PATH))
    print("Wyłączenie: python -m scraper.feed uninstall")
    return 0


def cmd_uninstall(args) -> int:
    _sh(["launchctl", "bootout", "%s/%s" % (_domain(), LABEL)], check=False)
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
    if APP_DIR.exists() and not args.keep_files:
        shutil.rmtree(APP_DIR)
    print("Wyłączono OLX z komputera (usunięto %s%s)." % (
        PLIST_PATH, "" if args.keep_files else " i %s" % APP_DIR))
    return 0


def cmd_now(args) -> int:
    if not PLIST_PATH.exists():
        raise SystemExit("Zadanie nie jest zainstalowane: python -m scraper.feed install")
    APP_DIR.mkdir(parents=True, exist_ok=True)
    FORCE_FLAG.touch()
    _sh(["launchctl", "kickstart", "%s/%s" % (_domain(), LABEL)])
    print("Uruchomiono — log: %s" % LOG_PATH)
    return 0


def cmd_status(args) -> int:
    p = _sh(["launchctl", "print", "%s/%s" % (_domain(), LABEL)], check=False)
    print("Zadanie w tle:", "włączone" if p.returncode == 0 else "WYŁĄCZONE")
    if p.returncode == 0:
        for line in p.stdout.splitlines():
            if line.strip().startswith(("state =", "last exit code =", "runs =")):
                print("  " + line.strip())
    st = load_state()
    if st:
        print("Ostatnie wysłanie danych:", st.get("last_ok") or "—")
        if st.get("last_error"):
            print("Ostatni błąd (%s): %s" % (st.get("last_error_at"), st["last_error"]))
    if LOG_PATH.exists():
        print("--- %s (koniec)" % LOG_PATH)
        print("\n".join(LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="OLX z komputera: pobieranie w tle i wysyłanie do repozytorium")
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("run", help="jeden przebieg (wywołuje go launchd)")
    r.add_argument("--no-git", action="store_true", help="tylko zapisz pliki (bez git), np. do testów")
    r.add_argument("--out", default=None, help="katalog wyjściowy zamiast feeds/ (z --no-git)")
    r.add_argument("--force", action="store_true", help="także na baterii i zaraz po poprzednim przebiegu")
    r.add_argument("--sources", default=None, help="portale (domyślnie `feeds:` z config.yaml)")
    r.add_argument("--cities", default=None)
    r.add_argument("--deals", default=None)
    r.add_argument("--max-pages", type=int, default=None)
    sub.add_parser("install", help="zainstaluj zadanie w tle (LaunchAgent) i uruchom je")
    u = sub.add_parser("uninstall", help="wyłącz zadanie i usuń jego pliki")
    u.add_argument("--keep-files", action="store_true", help="zostaw kopię repozytorium i środowisko")
    sub.add_parser("status", help="stan zadania i koniec logu")
    sub.add_parser("now", help="uruchom zadanie teraz")
    args = ap.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    cmd = args.cmd or "status"
    if cmd == "run" and args.out and not args.no_git:
        ap.error("--out tylko razem z --no-git")
    return {"run": cmd_run, "install": cmd_install, "uninstall": cmd_uninstall,
            "status": cmd_status, "now": cmd_now}[cmd](args)


if __name__ == "__main__":
    sys.exit(main())
