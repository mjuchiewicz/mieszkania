"""Testy scraper/alerts.py — dopasowanie filtrów (1:1 ze stroną), wybór nowości, kod alertu, e-mail.

Uruchomienie:  .venv/bin/python -m pytest tests/test_alerts.py
         albo  .venv/bin/python tests/test_alerts.py
"""
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from scraper import alerts as A  # noqa: E402
from scraper.alerts import matches  # noqa: E402


def L(**kw):
    d = {"key": "otodom:1", "source": "otodom", "id": "1", "deal": "sprzedaz", "url": "https://x.pl/1",
         "title": "Mieszkanie", "price": 600000, "area": 50.0, "rooms": 2, "active": True}
    d.update(kw)
    return d


def AL(deal="sprzedaz", area=None, drops=False, **filters):
    a = {"v": 1, "deal": deal, "filters": filters, "price_drops": drops}
    if area:
        a["area"] = area
    return A.normalize_alert(a)


# ----------------------------------------------------------------- podstawy

def test_no_filters_matches_active_only():
    assert matches(L(), AL())
    assert not matches(L(active=False), AL())


def test_deal_must_match():
    assert not matches(L(deal="wynajem"), AL("sprzedaz"))
    assert matches(L(deal="wynajem"), AL("wynajem"))


# ----------------------------------------------------------------- cena / metraż / zł za m²

def test_price_range_and_unknown_price():
    a = AL(price_min=500000, price_max=700000)
    assert matches(L(price=500000), a)
    assert matches(L(price=700000), a)
    assert not matches(L(price=499999), a)
    assert not matches(L(price=700001), a)
    assert not matches(L(price=None), AL(price_min=1))
    assert not matches(L(price=None), AL(price_max=900000))  # „do X” wymaga znanej ceny


def test_price_given_as_text_like_site_input():
    a = A.normalize_alert({"deal": "sprzedaz", "filters": {"price_max": "700 000"}})
    assert a["filters"]["price_max"] == 700000
    assert matches(L(price=650000), a) and not matches(L(price=750000), a)


def test_area_range_and_unknown_area():
    a = AL(area_min=40, area_max=60)
    assert matches(L(area=40), a) and matches(L(area=60), a)
    assert not matches(L(area=39.9), a) and not matches(L(area=60.5), a)
    assert not matches(L(area=None), AL(area_min=10))
    assert not matches(L(area=None), AL(area_max=100))


def test_ppm_only_for_sale_and_rounding_like_js():
    a = AL(ppm_max=12000)
    assert matches(L(price=600000, area=50), a)          # 12000
    assert matches(L(price=600024, area=50), a)          # 12000.48 -> 12000
    assert not matches(L(price=600025, area=50), a)      # 12000.5 -> 12001 (Math.round)
    assert not matches(L(price=None), a)                 # nieznana cena za m² nie przechodzi
    # przy wynajmie filtr zł/m² nie istnieje na stronie — ignorowany
    r = AL("wynajem", ppm_max=10)
    assert matches(L(deal="wynajem", price=3000, area=50), r)


# ----------------------------------------------------------------- pokoje

def test_rooms_including_5_plus_and_unknown():
    a = AL(rooms=["2", "5+"])
    assert matches(L(rooms=2), a)
    assert matches(L(rooms=6), a)
    assert not matches(L(rooms=3), a)
    assert not matches(L(rooms=None), a)


def test_rooms_ignored_for_rooms_deal():
    a = AL("pokoj", rooms=["3"])
    assert matches(L(deal="pokoj", rooms=1), a)


def test_rooms_normalization():
    a = A.normalize_alert({"deal": "sprzedaz", "filters": {"rooms": [2, "7", "x"]}})
    assert a["filters"]["rooms"] == ["2", "5+"]


# ----------------------------------------------------------------- dzielnice, rynek, ogłoszeniodawca, portal

def test_districts_exact_after_trim():
    a = AL(districts=["Krzyki", "Nadodrze"])
    assert matches(L(district=" Krzyki "), a)
    assert not matches(L(district="Krzyki-Partynice"), a)
    assert not matches(L(district=None), a)


def test_market_only_for_sale():
    a = AL(market=["pierwotny"])
    assert matches(L(market="pierwotny"), a)
    assert not matches(L(market="wtorny"), a)
    assert not matches(L(market=None), a)
    assert matches(L(deal="wynajem", market=None), AL("wynajem", market=["pierwotny"]))


def test_seller_and_sources():
    assert matches(L(seller="prywatny"), AL(seller=["prywatny"]))
    assert not matches(L(seller=None), AL(seller=["prywatny"]))
    assert matches(L(source="olx"), AL(sources=["olx", "gratka"]))
    assert not matches(L(source="otodom"), AL(sources=["olx"]))


# ----------------------------------------------------------------- wyszukiwanie po słowach

def test_text_all_words_diacritics_insensitive():
    l = L(title="Słoneczne mieszkanie z BALKONEM", street="ul. Łąkowa", district="Krzyki", description=None)
    assert matches(l, AL(text="balkon lakowa"))
    assert matches(l, AL(text="SŁONECZNE"))
    assert not matches(l, AL(text="balkon ogród"))
    assert matches(L(description="blisko parku"), AL(text="park"))


# ----------------------------------------------------------------- piętro

def test_floor_range_requires_known_floor():
    a = AL(floor_min=1, floor_max=3)
    assert matches(L(floor_num=1), a) and matches(L(floor_num=3), a)
    assert not matches(L(floor_num=0), a) and not matches(L(floor_num=4), a)
    assert not matches(L(floor_num=None), a)
    assert not matches(L(floor_num=None), AL(floor_max=10))


def test_floor_parter_text_is_zero():
    a = A.normalize_alert({"deal": "sprzedaz", "filters": {"floor_min": "parter", "floor_max": "parter"}})
    assert a["filters"] == {"floor_min": 0, "floor_max": 0}
    assert matches(L(floor_num=0), a) and not matches(L(floor_num=1), a)


def test_no_ground_only_excludes_known():
    a = AL(no_ground=True)
    assert not matches(L(floor_num=0), a)
    assert not matches(L(floor_num=-1), a)   # suterena
    assert matches(L(floor_num=1), a)
    assert matches(L(floor_num=None), a)     # nie wiadomo — zostaje


def test_no_top_only_excludes_known_top():
    a = AL(no_top=True)
    assert not matches(L(floor_num=4, floors_total=4), a)
    assert matches(L(floor_num=3, floors_total=4), a)
    assert matches(L(floor_num=4, floors_total=None), a)   # nie wiemy, ile pięter
    assert matches(L(floor_num=None, floors_total=4), a)
    assert matches(L(floor_num=4, floors_total=0), a)      # 0 = brak danych (jak na stronie)


# ----------------------------------------------------------------- cechy

def test_furnished_requires_known_value():
    assert matches(L(furnished=True), AL(furnished=True))
    assert not matches(L(furnished=None), AL(furnished=True))
    assert not matches(L(furnished=True), AL(furnished=False))
    assert matches(L(furnished=False), AL(furnished=False))
    assert not matches(L(furnished=None), AL(furnished=False))
    # strona zapisuje „tak” / „nie”
    assert A.normalize_alert({"filters": {"furnished": "nie"}})["filters"]["furnished"] is False


def test_conditions_grouped():
    a = AL(conditions=["dew"])
    assert matches(L(condition="stan_deweloperski"), a)
    assert matches(L(condition="do_wykonczenia"), a)
    assert not matches(L(condition="do_remontu"), a)
    assert not matches(L(condition=None), a)
    # surowe nazwy w kodzie alertu też działają
    assert A.normalize_alert({"filters": {"conditions": ["do_wykonczenia", "zam"]}})["filters"]["conditions"] == ["dew", "zam"]


def test_building_types_grouped():
    a = AL(building_types=["dom"])
    assert matches(L(building_type="szeregowiec"), a)
    assert matches(L(building_type="dom"), a)
    assert not matches(L(building_type="blok"), a)
    assert not matches(L(building_type=None), a)


def test_feature_groups_all_required():
    a = AL(features=["balkon", "parking", "piwnica"])
    assert matches(L(features=["taras", "garaz", "komorka"]), a)
    assert matches(L(features=["loggia", "parking", "piwnica", "winda"]), a)
    assert not matches(L(features=["balkon", "parking"]), a)
    assert not matches(L(features=[]), a)
    assert not matches(L(features=None), a)


def test_feature_klima_and_raw_names():
    a = A.normalize_alert({"filters": {"features": ["klimatyzacja", "taras", "nieznane"]}})
    assert a["filters"]["features"] == ["klima", "balkon"]
    assert matches(L(features=["klimatyzacja", "balkon"]), a)


def test_pets_only_for_rent():
    # na stronie „Zwierzęta OK” nie istnieje przy kupnie — ignorowane
    assert matches(L(features=[]), AL(features=["zwierzeta"]))
    assert not matches(L(deal="wynajem", features=[]), AL("wynajem", features=["zwierzeta"]))
    assert matches(L(deal="wynajem", features=["zwierzeta"]), AL("wynajem", features=["zwierzeta"]))


# ----------------------------------------------------------------- okolica

def test_area_radius_haversine():
    center = {"lat": 51.1, "lon": 17.03, "radius_m": 1000}
    a = AL(area=center)
    assert matches(L(lat=51.1, lon=17.03), a)
    assert matches(L(lat=51.1085, lon=17.03), a)     # ~945 m na północ
    assert not matches(L(lat=51.1095, lon=17.03), a)  # ~1056 m
    assert not matches(L(lat=None, lon=None), a)      # brak lokalizacji
    assert matches(L(lat=51.1, lon=17.03, geo="osiedle"), a)  # przybliżona też się liczy


def test_haversine_known_distance():
    # Rynek we Wrocławiu -> Dworzec Główny ≈ 1,6 km
    d = A.haversine_m(51.1100, 17.0320, 51.0983, 17.0367)
    assert 1250 < d < 1450


# ----------------------------------------------------------------- kod alertu

def test_code_roundtrip_unicode():
    a = AL("wynajem", area={"lat": 51.1, "lon": 17.03, "radius_m": 1500}, drops=True,
           price_max=3500, districts=["Krzyki", "Śródmieście"], features=["balkon"])
    a["name"] = "Krzyki — balkon"
    a["emails"] = ["ktos@example.com", "druga@example.org"]
    code = A.encode_alert(a)
    assert "=" not in code and "+" not in code and "/" not in code
    assert A.decode_alert(code) == A.normalize_alert(a)
    assert A.decode_alert("  " + code[:10] + "\n" + code[10:] + "  ") == A.normalize_alert(a)  # z odstępami


def test_decode_errors_and_raw_json():
    for bad in ("", "nie-kod!!", "e30"):  # e30 = "{}" -> brak błędu? {} to pusty alert = kupno, bez filtrów
        try:
            A.decode_alert(bad)
            ok = bad == "e30"
        except ValueError:
            ok = bad != "e30"
        assert ok, bad
    assert A.decode_alert('{"v":1,"deal":"pokoj"}')["deal"] == "pokoj"
    try:
        A.decode_alert(A.encode_alert({"v": 99}))
        assert False
    except ValueError:
        pass


def test_invalid_emails_dropped_and_defaults():
    a = A.normalize_alert({"emails": ["ok@example.com", "zly", "x@y", "a b@c.pl"]})
    assert a["emails"] == ["ok@example.com"]
    assert a["city"] == "wroclaw" and a["deal"] == "sprzedaz" and a["price_drops"] is False


# ----------------------------------------------------------------- wybór nowości

RUN = "2026-10-14T12:00:00Z"
PREV = "2026-10-14T11:00:00Z"
FIRST = "2026-10-13T21:00:00Z"
META = {"first_run": FIRST, "last_run": RUN,
        "sources": {"otodom": {"first_ok": FIRST}, "olx": {"first_ok": FIRST}, "gratka": {"first_ok": RUN}}}
SINCE = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 14, 12, 30, tzinfo=timezone.utc)


def N(key, **kw):
    src = key.split(":")[0]
    d = L(key=key, source=src, id=key.split(":")[1], first_seen=RUN)
    d.update(kw)
    return d


def keys(items):
    return sorted(l["key"] for l in items)


def test_candidates_basic_new_and_old():
    ls = [N("otodom:1"), N("otodom:2", first_seen=PREV), N("olx:3", active=False)]
    new, drops, _ = A.candidates(ls, META, SINCE, NOW)
    assert keys(new) == ["otodom:1"]
    assert drops == []


def test_candidates_skip_first_successful_run_of_source():
    ls = [N("gratka:1"), N("otodom:1")]
    new, _, st = A.candidates(ls, META, SINCE, NOW)
    assert keys(new) == ["otodom:1"] and st["baseline"] == 1
    # brak first_ok (portal nigdy nie działał / stary meta) = też stan wyjściowy
    new, _, _ = A.candidates([N("domiporta:1")], META, SINCE, NOW)
    assert new == []


def test_candidates_skip_reposts_of_known_flat():
    ls = [N("otodom:1", dup_group="g1"), N("olx:9", dup_group="g1", first_seen=PREV, active=False)]
    new, _, st = A.candidates(ls, META, SINCE, NOW)
    assert new == [] and st["repost"] == 1
    # dwa portale naraz w tym samym przebiegu — to nowość (w mailu jedna pozycja)
    ls = [N("otodom:1", dup_group="g2", price=500000), N("olx:2", dup_group="g2", price=505000)]
    new, _, _ = A.candidates(ls, META, SINCE, NOW)
    assert keys(new) == ["olx:2", "otodom:1"]
    n, d = A.pick_for_alert(AL(), new, [])
    assert keys(n) == ["otodom:1"]  # jedna na grupę, tańsza


def test_candidates_skip_old_publication_date():
    ls = [N("otodom:1", created_at="2026-09-01T10:00:00Z"), N("otodom:2", created_at="2026-10-14T09:00:00Z"),
          N("otodom:3", created_at="1999-02-29 00:00:01"), N("otodom:4", created_at="2026-10-10T09:00:00+02:00")]
    new, _, st = A.candidates(ls, META, SINCE, NOW)
    assert keys(new) == ["otodom:2", "otodom:3", "otodom:4"] and st["old"] == 1


def test_candidates_bulk_discovery_skipped():
    ls = [N("olx:%d" % i, first_seen=PREV) for i in range(500)]
    ls += [N("olx:n%d" % i) for i in range(200)]       # 200 nowych przy 500 znanych = doładowanie
    ls += [N("otodom:n%d" % i) for i in range(5)]
    new, _, st = A.candidates(ls, META, SINCE, NOW)
    assert len(new) == 5 and st["bulk"] == 200


def test_candidates_price_drops():
    hist = [{"date": PREV, "price": 650000}, {"date": RUN, "price": 600000}]
    up = [{"date": PREV, "price": 550000}, {"date": RUN, "price": 600000}]
    older = [{"date": FIRST, "price": 650000}, {"date": PREV, "price": 600000}]
    ls = [N("otodom:1", first_seen=PREV, price_history=hist), N("otodom:2", first_seen=PREV, price_history=up),
          N("otodom:3", first_seen=PREV, price_history=older),
          N("otodom:4", first_seen=PREV, price_history=hist, active=False)]
    new, drops, _ = A.candidates(ls, META, SINCE, NOW)
    assert new == [] and keys(drops) == ["otodom:1"]
    assert A.pick_for_alert(AL(), new, drops) == ([], [])          # alert bez obniżek
    assert keys(A.pick_for_alert(AL(drops=True), new, drops)[1]) == ["otodom:1"]
    assert A.pick_for_alert(AL(drops=True, price_max=550000), new, drops) == ([], [])  # filtr po nowej cenie


# ----------------------------------------------------------------- e-mail, link, sekrety, stan

def test_subject_and_plurals():
    a = AL("wynajem", districts=["Krzyki"], price_max=3500)
    assert A.subject_for(a, 3, 0) == "3 nowe mieszkania: Krzyki, do 3 500 zł"
    assert A.subject_for(a, 1, 0).startswith("1 nowe mieszkanie")
    assert A.subject_for(a, 5, 2).startswith("5 nowych mieszkań i 2 obniżki cen")
    a["name"] = "Moje Krzyki"
    assert A.subject_for(a, 22, 0) == "22 nowe mieszkania: Moje Krzyki"
    assert A.subject_for(AL("pokoj"), 2, 0).startswith("2 nowe pokoje")


def test_site_link_matches_site_hash_format():
    a = AL("wynajem", price_max=3500, rooms=["2", "3"], districts=["Stare Miasto"], features=["balkon"],
           furnished=True, no_ground=True, floor_min=1)
    link = A.site_link(a, "https://ex.pl/")
    assert link.startswith("https://ex.pl/#deal=wynajem&")
    assert "pmax=3500" in link and "rooms=2%7C3" in link and "districts=Stare%20Miasto" in link
    assert "feat=balkon" in link and "furn=tak" in link and "noGround=1" in link and "fmin=1" in link


def test_render_email_caps_and_escapes():
    a = AL("wynajem", area={"lat": 51.1, "lon": 17.03, "radius_m": 1500})
    items = [N("otodom:%d" % i, deal="wynajem", price=2000 + i, lat=51.1, lon=17.03, geo="osiedle",
               title="<b>Tytuł</b> %d" % i, images=["https://img.example/%d.jpg" % i]) for i in range(30)]
    subj, html_body, text = A.render_email(a, items, [], items, "ALERT_TEST", "https://ex.pl/")
    assert subj.startswith("30 nowych mieszkań")
    assert html_body.count("https://img.example/") == 25
    assert "i 5 więcej" in html_body and "i 5 więcej" in text
    assert "<b>Tytuł</b>" not in html_body and "&lt;b&gt;Tytuł" in html_body
    assert "lokalizacja przybliżona (osiedle)" in html_body
    assert "ALERT_TEST" in html_body and "/ mies." in html_body


def test_build_message_has_text_and_html():
    m = A.build_message("me@gmail.com", "you@example.com", "Temat ż", "<p>x</p>", "x")
    assert m.is_multipart()
    types = [p.get_content_type() for p in m.iter_parts()]
    assert types == ["text/plain", "text/html"]


def test_alerts_from_env_reads_all_secrets_json():
    env = {"ALL_SECRETS": json.dumps({"github_token": "t", "ALERT_B": "kod2", "ALERT_A": "kod1", "GMAIL_USER": "u"})}
    assert A.alerts_from_env(env) == [("ALERT_A", "kod1"), ("ALERT_B", "kod2")]
    assert A.alerts_from_env({}) == []


def test_alert_hash_is_keyed_and_stable():
    a = AL(price_max=1)
    assert A.alert_hash(a, "k") == A.alert_hash(dict(reversed(list(a.items()))), "k")
    assert A.alert_hash(a, "k") != A.alert_hash(a, "inny")
    assert len(A.alert_hash(a)) == 16


def test_data_file_prefers_city_dir(tmp_path=None):
    import tempfile
    from pathlib import Path
    d = Path(tempfile.mkdtemp())
    old = A.DATA_DIR
    try:
        A.DATA_DIR = d
        (d / "wynajem.json").write_text("[]")
        assert A.data_file("wroclaw", "wynajem.json") == d / "wynajem.json"
        assert A.data_file("krakow", "wynajem.json") is None   # stary układ tylko dla Wrocławia
        (d / "wroclaw").mkdir()
        (d / "wroclaw" / "wynajem.json").write_text("[]")
        assert A.data_file("wroclaw", "wynajem.json") == d / "wroclaw" / "wynajem.json"
    finally:
        A.DATA_DIR = old


if __name__ == "__main__":
    import inspect
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and inspect.isfunction(fn):
            try:
                fn()
                print("ok  ", name)
            except Exception as e:  # pragma: no cover
                fails += 1
                print("FAIL", name, repr(e))
    sys.exit(1 if fails else 0)
