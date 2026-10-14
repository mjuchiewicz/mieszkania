# Mieszkania Wrocław

Strona, która co godzinę sama zbiera oferty mieszkań we Wrocławiu z kilku portali
(Otodom, OLX, Gratka, Nieruchomosci-online, Domiporta) i pokazuje je w jednym miejscu.
U góry strony przełączasz się między trzema rodzajami ofert:

- **Kupno** – mieszkania na sprzedaż,
- **Wynajem** – mieszkania do wynajęcia (czynsz miesięczny, opłaty, kaucja),
- **Pokoje** – pokoje do wynajęcia.

Na stronie możesz:

- filtrować oferty: cena, pokoje, metraż, dzielnice, a w „Więcej filtrów”: cena za m², data dodania, rynek, ogłoszeniodawca, portal; do tego wyszukiwanie po słowach (np. „balkon”, nazwa ulicy),
- usuwać pojedyncze filtry jednym kliknięciem (pokazują się pod paskiem filtrów),
- sortować (najnowsze, najtańsze, najtańsze za m², największe),
- zapisywać ulubione (serduszko na zdjęciu) i ukrywać niepasujące oferty; oferty, które już otwierałaś, są przyciemnione,
- widzieć oznaczenia **Nowe** i **Obniżka** (ze starą, przekreśloną ceną),
- oglądać oferty na mapie (pinezki grupują się przy oddaleniu; puste kółko = położenie przybliżone do osiedla/dzielnicy, bo większość portali nie podaje współrzędnych — wtedy pinezkę stawiamy na podanej ulicy albo w środku osiedla),
- wysłać komuś link z ustawionymi filtrami (filtry zapisują się w adresie strony).

Ta sama oferta wystawiona na kilku portalach pokazuje się jako jedna karta z dopiskiem „też na …”.
Ulubione, ukryte i obejrzane zapisują się tylko w Twojej przeglądarce (na telefonie i komputerze osobno).
Każdy rodzaj ofert pamięta swoje własne filtry.

## Jak włączyć stronę (jednorazowo)

Strona stoi na GitHub Pages (za darmo) i publikuje się sama po każdej aktualizacji ofert:
**https://mjuchiewicz.github.io/flat-scrapper/**

Gdyby trzeba było włączyć ją od nowa: **Settings → Pages → Deploy from a branch → `main` / `/docs` → Save**.

## Ręczne pobranie ofert

Zakładka **Actions** → po lewej **Pobieranie ofert** → przycisk **Run workflow** → **Run workflow**.
Jedno uruchomienie pobiera wszystko: kupno, wynajem i pokoje. Pola w okienku można zostawić puste;
żeby pobrać np. tylko wynajem, wpisz w polu typów `wynajem`.
Po kilkudziesięciu minutach nowe dane pojawią się na stronie (czasem trzeba odświeżyć).

Automatycznie dzieje się to co godzinę.
Jeśli któryś portal nie zadziała, przy dacie aktualizacji na stronie pojawi się pomarańczowa kropka
i napis „portal nie działa” (po kliknięciu widać szczegóły), a jego wcześniejsze oferty zostaną na liście.

## Zmiana ustawień

Plik **`config.yaml`** — otwórz go na GitHubie, kliknij ikonę ołówka, zmień i kliknij **Commit changes**.

- `deals` – rodzaje ofert: `sprzedaz` (kupno), `wynajem`, `pokoj`.
  - `enabled: true/false` – czy w ogóle pobierać dany rodzaj (wyłączony znika też ze strony),
  - `prefilter` – wstępne ograniczenie tego, co pobieramy (cena od/do, minimalny metraż).
    Przy wynajmie i pokojach cena to czynsz miesięczny. Zwykłe filtrowanie rób na stronie — tu ustaw tylko szerokie widełki.
- `sources` – które portale są włączone (`true` / `false`).
- `max_pages` – bezpiecznik: maks. liczba stron wyników na portal.
- `drop_after_days` – po ilu dniach oferta, która zniknęła z portali, znika też ze strony.

Zmiany zadziałają przy następnym pobieraniu (albo uruchom je ręcznie, jak wyżej).

## Uruchomienie na własnym komputerze (dla chętnych)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m scraper.main                                   # wszystko
.venv/bin/python -m scraper.main --deals wynajem --sources olx     # szybki test jednego portalu
.venv/bin/python -m http.server -d docs 8000                       # podgląd: http://localhost:8000
```

Dane trafiają do `docs/data/sprzedaz.json`, `wynajem.json`, `pokoj.json` (oferty)
i `docs/data/meta.json` (czas i status pobierania każdego portalu).

Przybliżone położenie ofert bez współrzędnych liczone jest offline ze słownika ulic
i osiedli `scraper/geo/wroclaw.json` (dane © OpenStreetMap). Słownik buduje się raz
(np. raz w roku, gdy przybędzie nowych ulic): `.venv/bin/python -m scraper.geo.build_gazetteer`.
