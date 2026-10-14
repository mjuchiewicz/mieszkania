"""Testy scraper/features.py.

Uruchomienie:  .venv/bin/python -m pytest tests/test_features.py
         albo  .venv/bin/python tests/test_features.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from scraper.features import enrich, parse_floor_field  # noqa: E402


def E(desc="", title="", deal="sprzedaz", **kw):
    d = {"title": title, "description": desc, "deal": deal, "floor": None,
         "floor_num": None, "floors_total": None, "furnished": None, "condition": None,
         "building_type": None, "features": []}
    d.update(kw)
    return enrich(d)


def F(desc, title="", deal="sprzedaz"):
    return E(desc, title, deal)["features"]


# ----------------------------------------------------------------- floor field

def test_floor_field_variants():
    assert parse_floor_field("3") == (3, None)
    assert parse_floor_field("parter") == (0, None)
    assert parse_floor_field("Parter") == (0, None)
    assert parse_floor_field("suterena") == (-1, None)
    assert parse_floor_field("Suterena") == (-1, None)
    assert parse_floor_field("3/5") == (3, 5)
    assert parse_floor_field("parter/4") == (0, 4)
    assert parse_floor_field(">10") == (11, None)
    assert parse_floor_field("Powyżej 10") == (11, None)
    assert parse_floor_field("poddasze") == (None, None)
    assert parse_floor_field("poddasze/4") == (4, 4)
    assert parse_floor_field("-") == (None, None)
    assert parse_floor_field(None) == (None, None)
    assert parse_floor_field("5/4") == (5, None)


def test_floor_poddasze_with_text_total():
    r = E("Mieszkanie na poddaszu w 3-piętrowej kamienicy.", floor="Poddasze")
    assert r["floor_num"] == 3 and r["floors_total"] == 3


def test_floor_field_wins_over_text():
    r = E("Mieszkanie na 2 piętrze.", floor="4/10")
    assert (r["floor_num"], r["floors_total"]) == (4, 10)


def test_floor_scraper_value_not_overwritten():
    r = E("na 2 piętrze", floor="4/10", floor_num=7)
    assert r["floor_num"] == 7 and r["floors_total"] == 10


def test_floor_total_from_text_when_field_has_only_number():
    r = E("Mieszkanie na 4 piętrze w 4-piętrowym bloku.", floor="4")
    assert (r["floor_num"], r["floors_total"]) == (4, 4)


# ----------------------------------------------------------------- floor text

def test_floor_text_of():
    r = E("Oferuję mieszkanie, 3 piętro z 5, blisko tramwaju.")
    assert (r["floor_num"], r["floors_total"]) == (3, 5)


def test_floor_text_roman():
    r = E("NAJWAŻNIEJSZE: - powierzchnia 39 m² - IV piętro z windą")
    assert r["floor_num"] == 4


def test_floor_text_x_placeholder_ignored():
    assert E("Mieszkanie na X piętrze, blok")["floor_num"] is None


def test_floor_text_ordinal_and_total_word():
    r = E("Usytuowane jest na drugim piętrze czteropiętrowego budynku z windą.")
    assert (r["floor_num"], r["floors_total"]) == (2, 4)


def test_floor_text_parter():
    assert E("Pokój w mieszkaniu na parterze, blisko PWr.", deal="pokoj")["floor_num"] == 0


def test_floor_text_parter_shops_ignored():
    assert E("Na parterze budynku znajdują się lokale usługowe.")["floor_num"] is None


def test_floor_text_last_floor():
    r = E("Mieszkanie na ostatnim piętrze w 4-piętrowym bloku.")
    assert (r["floor_num"], r["floors_total"]) == (4, 4)


def test_floor_text_numeric_dot():
    r = E("Mieszkanie znajduje się na 9. piętrze w 10-piętrowym budynku z windą.")
    assert (r["floor_num"], r["floors_total"]) == (9, 10)


def test_floor_text_flat_number_not_floor():
    assert E("ul. Drabika 95/22, Wrocław")["floor_num"] is None


# ----------------------------------------------------------------- furnished

def test_furnished_pos():
    assert E("Mieszkanie jest w pełni umeblowane i wyposażone.")["furnished"] is True
    assert E("", title="Kawalerka umeblowana, ul. Lubuska")["furnished"] is True
    assert E("Sprzedam mieszkanie z meblami.")["furnished"] is True
    assert E("Mieszkanie częściowo umeblowane.")["furnished"] is True


def test_furnished_neg():
    assert E("Mieszkanie nieumeblowane, gotowe do wprowadzenia.")["furnished"] is False
    assert E("Pokój bez mebli, idealny dla osoby z własnymi meblami.", deal="pokoj")["furnished"] is False
    assert E("Mieszkanie jest puste, do własnej aranżacji.")["furnished"] is False
    assert E("Mieszkanie nie umeblowane.")["furnished"] is False


def test_furnished_unknown():
    assert E("Możliwość wynajęcia z meblami lub bez.")["furnished"] is None
    assert E("Kuchnia z meblami i sprzętem AGD.")["furnished"] is None
    assert E("Ładny układ, łatwe do umeblowania.")["furnished"] is None


def test_furnished_scraper_value_kept():
    assert E("Mieszkanie nieumeblowane.", furnished=True)["furnished"] is True


# ----------------------------------------------------------------- condition

def test_condition():
    assert E("Mieszkanie w stanie deweloperskim, oddanie 2027.")["condition"] == "stan_deweloperski"
    assert E("Lokal do wykończenia według własnego gustu.")["condition"] == "stan_deweloperski"
    assert E("Mieszkanie do remontu, cena do negocjacji.")["condition"] == "do_remontu"
    assert E("Mieszkanie wymaga odświeżenia.")["condition"] == "do_remontu"
    assert E("Po generalnym remoncie, gotowe do zamieszkania.")["condition"] == "do_zamieszkania"
    assert E("Mieszkanie nie wymaga żadnego remontu.")["condition"] == "do_zamieszkania"
    assert E("Mieszkanie gotowe do wprowadzenia od zaraz.")["condition"] == "do_zamieszkania"


def test_condition_building_renovation_ignored():
    assert E("Kamienica po remoncie elewacji, mieszkanie do remontu.")["condition"] == "do_remontu"
    assert E("Mieszkanie w odnowionej kamienicy.")["condition"] is None


def test_condition_conflict_none():
    assert E("Stan deweloperski. Możliwe także mieszkanie do zamieszkania po remoncie.")["condition"] is None


def test_condition_kept():
    assert E("do remontu", condition="do_wykonczenia")["condition"] == "do_wykonczenia"


# ----------------------------------------------------------------- building type

def test_building_type():
    assert E("Mieszkanie w zabytkowej kamienicy na Nadodrzu.")["building_type"] == "kamienica"
    assert E("1 piętro w bloku z windą.")["building_type"] == "blok"
    assert E("Nowoczesny apartamentowiec z recepcją.")["building_type"] == "apartamentowiec"
    assert E("Pokój w domu jednorodzinnym z ogrodem.", deal="pokoj")["building_type"] == "dom"
    assert E("Mieszkanie w zabudowie szeregowej, osobne wejście.")["building_type"] == "szeregowiec"
    assert E("", title="Nowoczesny loft 25,71 m² we Wrocławiu")["building_type"] == "loft"


def test_building_type_negative():
    assert E("Pokój w domu studenckim, blisko uczelni.", deal="pokoj")["building_type"] is None
    assert E("Poczuj się jak w domu! Mieszkanie w centrum.")["building_type"] is None
    assert E("Mieszkanie w stylu loft, nowe budownictwo.")["building_type"] is None
    assert E("Pod domem Żabka i piekarnia.")["building_type"] is None
    assert E("Okolica pełna zabytkowych kamienic.")["building_type"] is None
    assert E("Aneks z blokiem kuchennym.")["building_type"] is None


# ----------------------------------------------------------------- features

def test_features_basic():
    assert F("Mieszkanie z balkonem i piwnicą.") == ["balkon", "piwnica"]
    assert F("Duży taras 20 m2, miejsce postojowe w hali garażowej.") == ["taras", "parking", "garaz"]
    assert F("Mieszkanie z ogródkiem na parterze.") == ["ogrodek"]
    assert F("Do mieszkania przynależy komórka lokatorska.") == ["komorka"]
    assert F("Przestronna loggia od strony podwórza.") == ["loggia"]
    assert F("Kawalerka z klimatyzacją i balkonem.") == ["balkon", "klimatyzacja"]
    assert F("Budynek z windą, garaż podziemny.") == ["winda", "garaz"]


def test_features_no_diacritics():
    assert F("Blok z winda, garaz w cenie, komorka lokatorska") == ["winda", "garaz", "komorka"]


def test_features_negation():
    assert F("Brak balkonu, ale jest piwnica.") == ["piwnica"]
    assert F("Budynek bez windy.") == []
    assert F("W budynku nie ma windy.") == []
    assert F("3 piętro (bez windy) kamienicy z 1905 roku.") == []
    assert F("Winda: brak. Balkon: tak.") == ["balkon"]
    assert F("Bez windy, z balkonem.") == ["balkon"]
    assert F("Mieszkanie nie posiada balkonu ani piwnicy.") == []


def test_features_investment_alternatives_skipped():
    assert F("Przestronne balkony lub wybrukowane kostką tarasy.") == []
    assert F("Funkcjonalne mieszkania z ogródkami lub balkonami.") == []


def test_features_plural_with_number():
    assert F("Mieszkanie posiada dwa balkony.") == ["balkon"]


def test_features_klima_preparation_not_counted():
    assert F("Przygotowanie pod klimatyzację w salonie.") == []
    assert F("Możliwość montażu klimatyzacji.") == []


def test_features_ogrod_false_positives():
    assert F("Widok na Ogród Botaniczny.") == []
    assert F("Blisko ogródków działkowych.") == []


def test_features_parking_false_positives():
    assert F("Parking rowerowy w budynku.") == []
    assert F("Strefa płatnego parkowania.") == []


def test_features_pets():
    assert "zwierzeta" in F("Zwierzęta mile widziane.", deal="wynajem")
    assert "zwierzeta" in F("Mieszkanie przyjazne zwierzętom.", deal="wynajem")
    assert "zwierzeta" in F("Akceptowane zwierzęta: kot, mały piesek.", deal="wynajem")
    assert "zwierzeta" not in F("Szukam osoby niepalącej, bez zwierząt.", deal="pokoj")
    assert "zwierzeta" not in F("Nie akceptujemy zwierząt.", deal="wynajem")
    assert "zwierzeta" not in F("Zwierzęta nie są akceptowane.", deal="wynajem")


def test_features_union_with_scraper():
    r = E("Mieszkanie z balkonem.", features=["winda"])
    assert r["features"] == ["balkon", "winda"]


def test_does_not_mutate_input_and_missing_keys():
    d = {"title": "Kawalerka z balkonem", "description": None, "deal": "wynajem"}
    r = enrich(d)
    assert "features" not in d
    assert r["features"] == ["balkon"]
    for k in ("floor_num", "floors_total", "furnished", "condition", "building_type"):
        assert k in r


# ----------------------------------------------------------------- regresje z prawdziwych danych

def test_real_data_regressions():
    assert E("Na górze znajduje się nieumeblowana sypialnia z garderobą.")["furnished"] is None
    assert E("Kuchnia umeblowana, sprzęt AGD.")["furnished"] is None
    assert E("Nie chcesz płacić za pusty pokój w wakacje?", deal="pokoj")["furnished"] is None
    assert E("Mieszkanie umeblowane, kuchnia z meblami.")["furnished"] is True
    assert E("Kawalerka na parterze w domu wielorodzinnym.", deal="wynajem")["building_type"] is None
    assert E("Pokój w szeregówce na Wojszycach. W domu mieszkają spokojne osoby.",
             deal="pokoj")["building_type"] == "szeregowiec"
    assert "winda" not in F("3 piętro w bloku bez windy. Blok po remoncie elewacji, nowa winda.")
    assert E("Łazienka odnowiona, reszta w standardzie z lat 90.")["condition"] is None
    assert E("To urocze miejsce do zamieszkania: zieleń i cisza.")["condition"] is None
    assert E("Lokuj, wynajmuj, zarabiaj - pod klucz.")["condition"] is None
    assert F("Mieszkania z balkonami, loggiami, ogródkami i tarasami.") == []
    assert F("4 pokoje, 2 łazienki i 2 loggie.") == ["loggia"]
    assert E("2 pokoje | 46 m | Do zamieszkania od zaraz")["condition"] == "do_zamieszkania"


if __name__ == "__main__":
    fails = 0
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        try:
            fn()
            print("ok   ", name)
        except AssertionError as e:
            fails += 1
            import traceback
            print("FAIL ", name)
            traceback.print_exc()
    print(f"{len(tests) - fails}/{len(tests)} passed")
    sys.exit(1 if fails else 0)
