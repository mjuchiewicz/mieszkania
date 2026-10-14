# Mieszkania Wrocław 🏠

Strona, która co 4 godziny sama zbiera oferty **sprzedaży mieszkań we Wrocławiu** z kilku portali
(Otodom, OLX, Gratka, Nieruchomosci-online, Domiporta) i pokazuje je w jednym miejscu.

Na stronie możesz:

- filtrować oferty: cena, metraż, pokoje, cena za m², dzielnice, rynek, sprzedający, portal, „tylko nowe”, wyszukiwanie po słowach,
- sortować (najnowsze, najtańsze, najtańsze za m², największe),
- oznaczać ulubione ★, ukrywać niepasujące ✕; oferty, które już otwierałaś, są lekko przyciemnione,
- widzieć znaczki **NOWE** i **OBNIŻKA** (ze starą, przekreśloną ceną),
- oglądać oferty na mapie,
- wysłać komuś link z ustawionymi filtrami (filtry zapisują się w adresie strony).

Ta sama oferta wystawiona na kilku portalach pokazuje się jako jedna karta z dopiskiem „też na: …”.
Ulubione, ukryte i obejrzane zapisują się tylko w Twojej przeglądarce (na telefonie i komputerze osobno).

## Jak włączyć stronę (jednorazowo)

Repozytorium jest prywatne, a strona stoi na [Vercel](https://vercel.com) (darmowy plan):

1. Zaloguj się na vercel.com przez GitHuba → **Add New… → Project**.
2. Przy **flat-scrapper** kliknij **Import** (jeśli go nie widać: „Adjust GitHub App Permissions” i dodaj to repo).
3. **Root Directory** → **Edit** → wybierz `docs`. Framework Preset: **Other**. → **Deploy**.
4. Adres strony znajdziesz w Vercel na górze projektu (np. `flat-scrapper-xyz.vercel.app`).

Vercel sam publikuje nową wersję strony po każdej aktualizacji ofert.

## Ręczne pobranie ofert

Zakładka **Actions** → po lewej **Pobieranie ofert** → przycisk **Run workflow** → **Run workflow**.
Po kilku–kilkunastu minutach nowe dane pojawią się na stronie (czasem trzeba odświeżyć).

Automatycznie dzieje się to co 4 godziny (tak, żeby zmieścić się w darmowym limicie minut GitHuba dla prywatnych repozytoriów). Jeśli któryś portal nie zadziała, na stronie pojawi się
żółta informacja „⚠ portal nie działa”, a jego wcześniejsze oferty zostaną na liście.

## Zmiana ustawień

Plik **`config.yaml`** — otwórz go na GitHubie, kliknij ikonę ołówka ✏️, zmień i kliknij **Commit changes**.

- `prefilter` – wstępne ograniczenie tego, co w ogóle pobieramy (cena od/do, minimalny metraż).
  Zwykłe filtrowanie rób na stronie — tu ustaw tylko szerokie widełki.
- `sources` – które portale są włączone (`true` / `false`).
- `max_pages` – ile stron wyników przeglądać na każdym portalu (więcej = dłużej trwa).
- `drop_after_days` – po ilu dniach oferta, która zniknęła z portali, znika też ze strony.

Zmiany zadziałają przy następnym pobieraniu (albo uruchom je ręcznie, jak wyżej).

## Uruchomienie na własnym komputerze (dla chętnych)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m scraper.main                       # wszystkie portale
.venv/bin/python -m scraper.main --sources otodom,olx --max-pages 2   # szybki test
.venv/bin/python -m http.server -d docs 8000           # podgląd: http://localhost:8000
```

Dane trafiają do `docs/data/listings.json` (oferty) i `docs/data/meta.json` (czas i status pobierania).
