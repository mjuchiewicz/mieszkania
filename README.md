# Mieszkania Wrocław 🏠

Strona, która co 2 godziny sama zbiera oferty **sprzedaży mieszkań we Wrocławiu** z kilku portali
(Otodom, OLX, Gratka, Morizon, Nieruchomosci-online, Domiporta) i pokazuje je w jednym miejscu.

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

1. Na GitHubie wejdź w repozytorium → **Settings** → **Pages**.
2. W „Build and deployment” wybierz **Source: Deploy from a branch**, gałąź **main** i folder **/docs** → **Save**.
3. Po minucie–dwóch strona będzie pod adresem `https://<twoja-nazwa>.github.io/<nazwa-repozytorium>/`.
4. Sprawdź też **Settings → Actions → General → Workflow permissions**: ma być zaznaczone **Read and write permissions**
   (żeby automat mógł zapisywać nowe oferty).

Osobna akcja do publikacji nie jest potrzebna — GitHub Pages sam serwuje folder `docs`.

## Ręczne pobranie ofert

Zakładka **Actions** → po lewej **Pobieranie ofert** → przycisk **Run workflow** → **Run workflow**.
Po kilku–kilkunastu minutach nowe dane pojawią się na stronie (czasem trzeba odświeżyć).

Automatycznie dzieje się to co 2 godziny. Jeśli któryś portal nie zadziała, na stronie pojawi się
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
