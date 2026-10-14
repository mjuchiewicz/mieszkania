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
- dostawać **maile o nowych ofertach** pasujących do filtrów (dzwonek w prawym górnym rogu — zob. „Powiadomienia e-mail” niżej).

Ta sama oferta wystawiona na kilku portalach pokazuje się jako jedna karta z dopiskiem „też na …”.
Ulubione, ukryte i obejrzane zapisują się tylko w Twojej przeglądarce (na telefonie i komputerze osobno).
Każdy rodzaj ofert pamięta swoje własne filtry.

## Jak włączyć stronę (jednorazowo)

Strona stoi na GitHub Pages (za darmo) i publikuje się sama po każdej aktualizacji ofert:
**https://mjuchiewicz.github.io/mieszkania/**

Gdyby trzeba było włączyć ją od nowa: **Settings → Pages → Deploy from a branch → `main` / `/docs` → Save**.

## Ręczne pobranie ofert

Zakładka **Actions** → po lewej **Pobieranie ofert** → przycisk **Run workflow** → **Run workflow**.
Jedno uruchomienie pobiera wszystko: kupno, wynajem i pokoje. Pola w okienku można zostawić puste;
żeby pobrać np. tylko wynajem, wpisz w polu typów `wynajem`.
Po kilkudziesięciu minutach nowe dane pojawią się na stronie (czasem trzeba odświeżyć).

Automatycznie dzieje się to co godzinę.
Jeśli któryś portal nie zadziała, przy dacie aktualizacji na stronie pojawi się pomarańczowa kropka
i napis „portal nie działa” (po kliknięciu widać szczegóły), a jego wcześniejsze oferty zostaną na liście.

## Powiadomienia e-mail

Strona może wysyłać maila, gdy pojawią się **nowe** oferty pasujące do Twoich filtrów
(np. „wynajem na Krzykach do 3500 zł z balkonem” albo „do 2 km od wybranego miejsca”).
Mail przychodzi po pobieraniu ofert (co godzinę) i tylko wtedy, gdy jest coś nowego.
Można mieć wiele alertów, a każdy może iść na kilka adresów.

Repozytorium jest publiczne, dlatego alerty i adresy e-mail trzymamy w **sekretach** GitHuba —
nikt poza Tobą ich nie zobaczy (nie widać ich też w logach).

### Krok 1 (jednorazowo): hasło aplikacji Gmaila

Maile wysyła Twoje konto Gmail. Gmail nie pozwala użyć zwykłego hasła, potrzebne jest „hasło aplikacji”:

1. Włącz **weryfikację dwuetapową** na koncie Google (bez niej hasła aplikacji nie działają):
   https://myaccount.google.com/signinoptions/twosv
2. Wejdź na **https://myaccount.google.com/apppasswords**, wpisz nazwę, np. `Mieszkania`, i kliknij **Utwórz**.
3. Google pokaże 16 liter (np. `abcd efgh ijkl mnop`) — skopiuj je. Spacje nie mają znaczenia.

### Krok 2 (jednorazowo): dane Gmaila w sekretach

Wejdź na https://github.com/mjuchiewicz/mieszkania/settings/secrets/actions/new
(albo: **Settings → Secrets and variables → Actions → New repository secret**) i dodaj dwa sekrety:

| Name | Secret |
|---|---|
| `GMAIL_USER` | Twój adres Gmail, np. `imie.nazwisko@gmail.com` |
| `GMAIL_APP_PASSWORD` | 16 liter z kroku 1 |

Na ten adres (`GMAIL_USER`) idą też alerty, w których nie wpisano żadnego adresu.

### Dodanie alertu

1. Na stronie ustaw filtry (rodzaj ofert, cena, dzielnice, udogodnienia…).
2. Kliknij **dzwonek** (Powiadomienia) w prawym górnym rogu.
3. Opcjonalnie włącz **Okolica** i kliknij na mapie miejsce + ustaw promień (0,5–5 km).
4. Wpisz nazwę, adresy e-mail (Enter po każdym) i zdecyduj, czy chcesz też maile o **obniżkach cen**.
5. Kliknij **Utwórz alert** i zrób to, co pokaże strona: otwórz GitHub, wpisz nazwę `ALERT_…`,
   wklej kod alertu, kliknij **Add secret**.

### Wyłączenie / zmiana alertu

- **Wyłączenie:** **Settings → Secrets and variables → Actions** → ikona kosza przy `ALERT_…`.
- **Zmiana:** utwórz nowy kod na stronie, a na GitHubie kliknij ołówek przy sekrecie i wklej nowy kod.
  GitHub nie pokazuje zapisanej treści sekretu — żeby zobaczyć, co jest w alercie, wklej kod
  w zakładce **Wczytaj alert** (alerty utworzone w danej przeglądarce są tam na liście).

### Dobrze wiedzieć

- „Nowa” oferta to taka, której nie było przy poprzednim pobieraniu. Nie dostaniesz maila o ofercie,
  która już wcześniej była na innym portalu (ta sama oferta przeniesiona/powielona), ani o setkach
  starych ofert, gdy jakiś portal zostanie dopiero włączony.
- W jednym mailu jest do 25 ofert; resztę zobaczysz na stronie (link w mailu).
- Przy „Okolicy” oferty bez dokładnego adresu sprawdzamy po ulicy lub osiedlu — w mailu mają dopisek
  „lokalizacja przybliżona”. Oferty bez żadnej lokalizacji nie trafiają do alertów z okolicą.
- Sprawdzenie, czy maile w ogóle dochodzą: **Actions** → **Mail testowy** → **Run workflow**.
  Przyjdzie jeden przykładowy mail (temat zaczyna się od „[TEST]”) na adres z `GMAIL_USER`.
- Gdy coś nie działa: **Actions** → ostatnie uruchomienie → krok **Powiadomienia e-mail**.
  Komunikat „Gmail odrzucił logowanie” = popraw `GMAIL_USER` / `GMAIL_APP_PASSWORD`.
- Test bez wysyłania (na komputerze): zapisz kody w pliku (jeden na linię, `ALERT_NAZWA=kod`) i uruchom
  `.venv/bin/python -m scraper.alerts --dry-run --alerts-file plik.txt --since 2026-10-14T00:00:00Z --preview-dir podglad`
  — w folderze `podglad` będzie podgląd maili.

## OLX z komputera

OLX blokuje serwery GitHuba, więc oferty z OLX pobiera **Twój Mac**: co godzinę, w tle,
gdy jest włączony, podłączony do zasilacza i ma internet (na baterii przebieg jest pomijany).
Wyniki wysyła do repozytorium (folder `feeds/olx/`), a najbliższe automatyczne pobieranie
dołącza je do strony.

- **Mac musi być czasem włączony.** Gdy jest wyłączony dłużej niż 6 godzin, OLX na stronie
  jest oznaczony jako „nieaktualne” — wcześniejsze oferty z OLX zostają, nowe nie dochodzą.
- Zadanie działa na osobnej kopii repozytorium (`~/Library/Application Support/mieszkania-olx/`),
  więc nie przeszkadza w pracy na tym folderze. Log: `~/Library/Logs/mieszkania-olx.log`.
- Stan: `.venv/bin/python -m scraper.feed status`, uruchomienie od razu: `... now`.
- **Wyłączenie:** `.venv/bin/python -m scraper.feed uninstall`
  (albo usuń plik `~/Library/LaunchAgents/pl.mieszkania.olx.plist` i uruchom ponownie komputer).
  Ponowne włączenie: `.venv/bin/python -m scraper.feed install`.
- Także na baterii: w `config.yaml` ustaw `on_battery: true` przy `feeds: olx`.

## Zmiana ustawień

Plik **`config.yaml`** — otwórz go na GitHubie, kliknij ikonę ołówka, zmień i kliknij **Commit changes**.

- `cities` – miejscowości (nie tylko Wrocław — także małe miasta i wsie). Dopisz nazwę w nowej linii, np.
  ```yaml
  cities:
    - wroclaw
    - Kraków
    - Siechnice
    - {name: "Radwanice", county: "wrocławski"}   # gdy jest kilka miejscowości o tej nazwie
  ```
  Przy najbliższym pobieraniu scraper sam sprawdzi, jak ta miejscowość nazywa się w każdym portalu
  (zapisze to w `scraper/cities_cache.json`) i zbuduje słownik ulic do mapy (`scraper/geo/<miejscowość>.json`).
  Na stronie pojawi się wtedy wybór miasta obok logo. Portal, który danej miejscowości nie zna, jest dla niej
  pomijany (widać to po kliknięciu daty aktualizacji). Każde miasto wydłuża pobieranie — przy kilku dużych
  miastach może być potrzebne zwiększenie `timeout-minutes` w `.github/workflows/scrape.yml`.
  Na komputerze można to sprawdzić od razu: `.venv/bin/python -m scraper.cities add "Kraków"`.
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

Dane trafiają do `docs/data/<miejscowość>/sprzedaz.json`, `wynajem.json`, `pokoj.json` (oferty)
i `docs/data/<miejscowość>/meta.json` (czas i status pobierania każdego portalu), a lista miejscowości
do `docs/data/cities.json`. Tylko wybrane miasto: `--cities wroclaw`.

Miejscowości: `.venv/bin/python -m scraper.cities add "Kraków"` (ustala lokalizację w każdym portalu
i pokazuje wynik), `... show` (co jest zapisane), `... add "Kraków" --force --portals otodom` (ponów jeden portal).

Przybliżone położenie ofert bez współrzędnych liczone jest offline ze słownika ulic
i osiedli `scraper/geo/<miejscowość>.json` (dane © OpenStreetMap). Słownik buduje się raz
(np. raz w roku, gdy przybędzie nowych ulic): `.venv/bin/python -m scraper.geo.build_gazetteer [--city krakow]`.
