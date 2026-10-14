/* Mieszkania (Wrocław i inne miejscowości z config.yaml) — statyczny frontend (bez budowania, bez backendu). */
(function () {
  "use strict";

  // ------------------------------------------------------------------ stałe
  var PAGE = 48;
  var DAY = 86400000;
  var WARMUP = DAY; // oferty bez daty publikacji z pierwszej doby pobierania portalu = „stan wyjściowy”, nie nowości
  var LS_PREFIX = "mwro_";
  var LEAFLET = "https://unpkg.com/leaflet@1.9.4/dist/";
  var CLUSTER = "https://unpkg.com/leaflet.markercluster@1.5.3/dist/";
  // dokładność położenia (pole `geo` z scrapera); brak pola przy współrzędnych = z portalu
  var GEO_APPROX = { osiedle: "osiedle", dzielnica: "dzielnica" };
  var SOURCE_NAMES = {
    otodom: "Otodom", olx: "OLX", gratka: "Gratka", morizon: "Morizon",
    nieruchomosci_online: "Nieruchomosci-online", domiporta: "Domiporta"
  };
  var DEAL_ORDER = ["sprzedaz", "wynajem", "pokoj"];
  // Nawigacja: górny przełącznik to Kupno / Wynajem; pod Wynajmem wybór „Całe mieszkanie / Pokój”.
  // Pliki danych i adresy (#deal=pokoj) zostają po staremu — to tylko inny układ przycisków.
  var RENT_SUBS = ["wynajem", "pokoj"];
  function isRent(deal) { return RENT_SUBS.indexOf(deal) >= 0; }
  var DEALS = {
    sprzedaz: {
      label: "Kupno", monthly: false, ppm: true, market: true, rooms: true,
      sellers: [["prywatny", "Osoba prywatna"], ["agencja", "Biuro"], ["deweloper", "Deweloper"]],
      pricePresets: [400000, 500000, 600000, 700000, 800000, 1000000],
      areaPresets: [30, 40, 50, 60, 75],
      what: "ofert sprzedaży", noun: "Mieszkanie"
    },
    wynajem: {
      label: "Wynajem", monthly: true, ppm: false, market: false, rooms: true,
      sellers: [["prywatny", "Osoba prywatna"], ["agencja", "Biuro"]],
      pricePresets: [2000, 2500, 3000, 3500, 4000, 5000],
      areaPresets: [25, 35, 45, 60],
      what: "mieszkań na wynajem", noun: "Mieszkanie"
    },
    pokoj: {
      label: "Pokoje", monthly: true, ppm: false, market: false, rooms: false,
      sellers: [["prywatny", "Osoba prywatna"], ["agencja", "Biuro"]],
      pricePresets: [700, 900, 1100, 1300, 1600],
      areaPresets: [8, 10, 12, 15],
      what: "pokoi do wynajęcia", noun: "Pokój"
    }
  };
  var MARKETS = [["pierwotny", "Pierwotny"], ["wtorny", "Wtórny"]];
  var ROOMS = [["1", "1"], ["2", "2"], ["3", "3"], ["4", "4"], ["5+", "5+"]];
  var DAYS = [["0", "Kiedykolwiek"], ["1", "24 godz."], ["3", "3 dni"], ["7", "Tydzień"], ["30", "Miesiąc"]];

  // Cechy mieszkania (pola floor_num, furnished, condition, building_type, features ze scrapera).
  // W filtrach część wartości łączymy w grupy, które dla szukającego znaczą to samo.
  var FURN = [["", "Obojętne"], ["tak", "Tak"], ["nie", "Nie"]];
  var CONDS = [["zam", "Do zamieszkania"], ["dew", "Stan deweloperski"], ["rem", "Do remontu"]];
  var COND_OF = { do_zamieszkania: "zam", stan_deweloperski: "dew", do_wykonczenia: "dew", do_remontu: "rem" };
  var BTYPES = [["blok", "Blok"], ["kamienica", "Kamienica"], ["apartamentowiec", "Apartamentowiec"], ["dom", "Dom"], ["loft", "Loft"]];
  var BT_OF = { blok: "blok", kamienica: "kamienica", apartamentowiec: "apartamentowiec", dom: "dom", szeregowiec: "dom", loft: "loft" };
  var FEATS = [["balkon", "Balkon / taras / loggia"], ["ogrodek", "Ogródek"], ["winda", "Winda"], ["parking", "Parking / garaż"],
    ["piwnica", "Piwnica / komórka"], ["klima", "Klimatyzacja"], ["zwierzeta", "Zwierzęta OK"]];
  var FEAT_OF = { balkon: "balkon", taras: "balkon", loggia: "balkon", ogrodek: "ogrodek", winda: "winda", parking: "parking",
    garaz: "parking", piwnica: "piwnica", komorka: "piwnica", klimatyzacja: "klima", zwierzeta: "zwierzeta" };
  var RENT_ONLY_FEATS = { zwierzeta: 1 };

  var DEFAULTS = {
    q: "", pmin: "", pmax: "", amin: "", amax: "", ppm: "", fmin: "", fmax: "",
    rooms: [], districts: [], market: [], seller: [], sources: [],
    cond: [], btype: [], feat: [], furn: "",
    days: 0, hideDups: true, showHidden: false, showInactive: false, noGround: false, noTop: false,
    sort: "newest", tab: "all", view: "list"
  };
  var TEXT_KEYS = ["q", "pmin", "pmax", "amin", "amax", "ppm", "fmin", "fmax"];
  var ARRAY_KEYS = ["rooms", "districts", "market", "seller", "sources", "cond", "btype", "feat"];
  var BOOL_KEYS = ["hideDups", "showHidden", "showInactive", "noGround", "noTop"];
  var ALLOWED = { cond: CONDS, btype: BTYPES, feat: FEATS, furn: FURN };

  // ------------------------------------------------------------------ narzędzia
  function $(id) { return document.getElementById(id); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  function lsGet(key, fallback) {
    try {
      var v = window.localStorage.getItem(LS_PREFIX + key);
      return v == null ? fallback : JSON.parse(v);
    } catch (e) { return fallback; }
  }
  function lsSet(key, value) {
    try { window.localStorage.setItem(LS_PREFIX + key, JSON.stringify(value)); } catch (e) { /* brak miejsca / tryb prywatny */ }
  }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function safeUrl(u) { return /^https?:\/\//i.test(u || "") ? u : "#"; }
  function norm(s) {
    return String(s || "").toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "").replace(/ł/g, "l");
  }
  function num(s) {
    if (s == null || s === "") return null;
    var t = String(s).replace(/\s/g, "").replace(",", ".").replace(/[^0-9.]/g, "");
    if (!t) return null;
    var n = parseFloat(t);
    return isFinite(n) ? n : null;
  }
  function icon(name) { return '<svg aria-hidden="true"><use href="#i-' + name + '"/></svg>'; }
  var nf = new Intl.NumberFormat("pl-PL", { maximumFractionDigits: 0 });
  var nf1 = new Intl.NumberFormat("pl-PL", { maximumFractionDigits: 1 });
  function zl(n) { return n == null ? "—" : nf.format(n) + " zł"; }
  function shortZl(n) {
    if (n == null) return "";
    if (n >= 1000000) return nf1.format(n / 1000000) + " mln";
    if (n >= 10000) return nf1.format(n / 1000) + " tys.";
    return nf.format(n) + " zł";
  }
  function parseTs(s) {
    if (!s) return 0;
    var t = Date.parse(s);
    return isNaN(t) ? 0 : t;
  }
  function ago(ts, short) {
    if (!ts) return "";
    var d = Date.now() - ts;
    if (d < 0) d = 0;
    var m = Math.round(d / 60000);
    if (m < 1) return "przed chwilą";
    if (m < 60) return m + " min temu";
    var h = Math.round(m / 60);
    if (h < 24) return h + (short ? " godz." : " godz. temu");
    var days = Math.round(h / 24);
    if (days === 1) return "wczoraj";
    if (days < 30) return days + " dni temu";
    return new Date(ts).toLocaleDateString("pl-PL", { day: "numeric", month: "short", year: "numeric" });
  }
  function fmtDate(ts) {
    try {
      return new Date(ts).toLocaleString("pl-PL", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });
    } catch (e) { return new Date(ts).toISOString(); }
  }
  function fmtDay(s) {
    var t = parseTs(s);
    if (!t) return "";
    if (t <= Date.now()) return "od zaraz";
    return "od " + new Date(t).toLocaleDateString("pl-PL", { day: "numeric", month: "long" });
  }
  function srcName(s) { return SOURCE_NAMES[s] || s; }
  function plural(n, one, few, many) {
    var n10 = n % 10, n100 = n % 100;
    if (n === 1) return one;
    if (n10 >= 2 && n10 <= 4 && (n100 < 12 || n100 > 14)) return few;
    return many;
  }
  function isDesktop() { return window.matchMedia("(min-width: 900px)").matches; }
  function cssEsc(s) { return window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/["\\]/g, "\\$&"); }

  // ------------------------------------------------------------------ stan
  var META = {};
  var metaReady = null;
  // Miejscowości z data/cities.json. CITIES = null → stary układ danych (data/<typ>.json, tylko Wrocław).
  var CITIES = null;
  var CITY = null;         // slug wybranej miejscowości, np. "wroclaw"
  var DEFAULT_CITY = "wroclaw";
  var STORE = {};          // deal -> { status, ALL, BY_KEY, GROUPS, districts }
  var DEAL = "sprzedaz";
  var D = null;            // STORE[DEAL]
  var F = null;            // filtry bieżącego typu
  var filtered = [];
  var shown = 0;
  var fav = toSet(lsGet("fav", []));
  var hidden = toSet(lsGet("hidden", []));
  var seen = toSet(lsGet("seen", []));
  var mapState = { map: null, layer: null, tiles: null, loading: false };

  function toSet(arr) {
    var o = Object.create(null);
    if (Array.isArray(arr)) arr.forEach(function (k) { o[k] = 1; });
    return o;
  }
  function saveSet(name, set) { lsSet(name, Object.keys(set)); }
  function cloneDefaults() { return JSON.parse(JSON.stringify(DEFAULTS)); }
  function cfg() { return DEALS[DEAL]; }

  // wartości spoza listy (stary link, ręcznie zmieniony adres) odrzucamy
  function keepAllowed(f) {
    Object.keys(ALLOWED).forEach(function (k) {
      var ok = ALLOWED[k].map(function (x) { return x[0]; });
      if (Array.isArray(f[k])) f[k] = f[k].filter(function (v, i, a) { return ok.indexOf(v) >= 0 && a.indexOf(v) === i; });
      else if (ok.indexOf(f[k]) < 0) f[k] = "";
    });
    return f;
  }
  function fromParams(p) {
    var f = cloneDefaults();
    Object.keys(DEFAULTS).forEach(function (k) {
      if (!p.has(k)) return;
      var v = p.get(k);
      if (ARRAY_KEYS.indexOf(k) >= 0) f[k] = v ? v.split("|").filter(Boolean) : [];
      else if (BOOL_KEYS.indexOf(k) >= 0) f[k] = v === "1";
      else if (k === "days") f[k] = parseInt(v, 10) || 0;
      else f[k] = v;
    });
    return keepAllowed(f);
  }
  function toParams(f) {
    var p = new URLSearchParams();
    if (multiCity()) p.set("city", CITY);
    p.set("deal", DEAL);
    Object.keys(DEFAULTS).forEach(function (k) {
      var v = f[k], d = DEFAULTS[k];
      if (ARRAY_KEYS.indexOf(k) >= 0) { if (v.length) p.set(k, v.join("|")); }
      else if (BOOL_KEYS.indexOf(k) >= 0) { if (v !== d) p.set(k, v ? "1" : "0"); }
      else if (String(v) !== String(d) && v !== "") p.set(k, String(v));
    });
    return p;
  }
  function sanitize(saved) {
    var f = cloneDefaults();
    if (saved && typeof saved === "object") {
      Object.keys(DEFAULTS).forEach(function (k) {
        if (saved[k] !== undefined && typeof saved[k] === typeof DEFAULTS[k]) f[k] = saved[k];
        if (ARRAY_KEYS.indexOf(k) >= 0 && !Array.isArray(f[k])) f[k] = [];
      });
    }
    return keepAllowed(f);
  }
  // filtry (m.in. dzielnice) pamiętamy osobno dla każdej miejscowości; Wrocław pod starym kluczem
  function filtersKey(deal) { return CITY && CITY !== DEFAULT_CITY ? "filters_" + CITY + "_" + deal : "filters_" + deal; }
  function savedFilters(deal) {
    var s = lsGet(filtersKey(deal), null);
    if (!s && deal === "sprzedaz" && (!CITY || CITY === DEFAULT_CITY)) s = lsGet("filters", null); // poprzednia wersja strony
    return sanitize(s);
  }
  // Odczyt stanu z adresu (#deal=wynajem&pmax=3000…) albo z pamięci przeglądarki.
  function readLocation() {
    var h = (location.hash || "").replace(/^#/, "");
    var p = null;
    try { p = h ? new URLSearchParams(h) : null; } catch (e) { p = null; }
    var deal = p && p.get("deal");
    if (!DEALS[deal]) deal = p && Array.from(p.keys()).length ? "sprzedaz" : lsGet("deal", "sprzedaz");
    if (!DEALS[deal]) deal = "sprzedaz";
    var onlyDeal = p && Array.from(p.keys()).every(function (k) { return k === "deal" || k === "city"; });
    return { deal: deal, F: p && !onlyDeal ? fromParams(p) : savedFilters(deal) };
  }
  // Miejscowość z adresu (#city=krakow), z pamięci przeglądarki albo pierwsza z listy.
  function readCity() {
    if (!CITIES) return null;
    var p = null;
    try { p = new URLSearchParams((location.hash || "").replace(/^#/, "")); } catch (e) { p = null; }
    var want = [p && p.get("city"), lsGet("city", null), CITIES.default];
    for (var i = 0; i < want.length; i++) if (cityInfo(want[i])) return want[i];
    return CITIES.cities[0].slug;
  }
  function cityInfo(slug) {
    if (!CITIES || !slug) return null;
    for (var i = 0; i < CITIES.cities.length; i++) if (CITIES.cities[i].slug === slug) return CITIES.cities[i];
    return null;
  }
  function multiCity() { return !!(CITIES && CITIES.cities.length > 1); }
  function cityCenter() {
    var c = cityInfo(CITY);
    return c && c.lat && c.lon ? [c.lat, c.lon] : [51.11, 17.03];
  }
  function persistFilters() {
    lsSet(filtersKey(DEAL), F);
    lsSet("deal", DEAL);
    if (CITY) lsSet("city", CITY);
    if (isRent(DEAL)) lsSet("rentSub", DEAL); // Wynajem pamięta ostatni wybór: mieszkanie czy pokój
    var url = location.pathname + location.search + "#" + toParams(F).toString();
    try { history.replaceState(null, "", url); } catch (e) { /* np. file:// */ }
  }

  // ------------------------------------------------------------------ dane
  function dealMeta(deal) {
    var dm = META.deals && META.deals[deal];
    if (dm) return dm;
    if (deal === "sprzedaz" && META.sources) return { sources: META.sources, last_run: META.last_run, first_run: META.first_run };
    return null;
  }

  function prepare(store, list, deal) {
    var dm = dealMeta(deal) || {};
    var srcMeta = dm.sources || {};
    var base = Object.create(null);
    // pierwszy udany przebieg każdego portalu — oferty z niego nie są „nowe”
    list.forEach(function (l) {
      var t = parseTs(l.first_seen);
      if (t && (!base[l.source] || t < base[l.source])) base[l.source] = t;
    });
    Object.keys(srcMeta).forEach(function (s) {
      var t = parseTs(srcMeta[s] && srcMeta[s].first_ok);
      if (t && (!base[s] || t < base[s])) base[s] = t;
    });
    var now = Date.now();
    var BY_KEY = store.BY_KEY, GROUPS = store.GROUPS;
    list.forEach(function (l) {
      l._first = parseTs(l.first_seen);
      var created = parseTs(l.created_at);
      if (created > now + DAY || created < Date.UTC(2000, 0, 1)) created = 0;
      // Data oferty: publikacja wg portalu, a gdy portal jej nie podaje — moment, w którym
      // pierwszy raz ją zobaczyliśmy. Wyjątek: oferty z pierwszej doby pobierania danego
      // portalu nie mają wiarygodnej daty (były tam już wcześniej), więc idą na koniec
      // sortowania „najnowsze” i nie dostają znaczka NOWE.
      var warm = l._first && base[l.source] && l._first - base[l.source] < WARMUP;
      l._date = created || (warm ? 0 : l._first);
      l._ppm = deal === "sprzedaz" && l.price && l.area ? Math.round(l.price / l.area) : null;
      l._search = norm([l.title, l.street, l.district, l.description].join(" "));
      l._dist = l.district ? String(l.district).trim() : "";
      l._fl = typeof l.floor_num === "number" ? l.floor_num : null;
      l._flTot = typeof l.floors_total === "number" && l.floors_total > 0 ? l.floors_total : null;
      l._cond = COND_OF[l.condition] || "";
      l._bt = BT_OF[l.building_type] || "";
      l._feat = Object.create(null);
      (Array.isArray(l.features) ? l.features : []).forEach(function (f) { if (FEAT_OF[f]) l._feat[FEAT_OF[f]] = 1; });
      l._oldPrice = null;
      var h = l.price_history || [];
      if (h.length >= 2 && l.price) {
        var prev = h[h.length - 2].price;
        if (prev && prev > l.price) l._oldPrice = prev;
      }
      BY_KEY[l.key] = l;
      if (l.dup_group) (GROUPS[l.dup_group] = GROUPS[l.dup_group] || []).push(l);
    });
    var counts = Object.create(null);
    list.forEach(function (l) { if (l.active && l._dist) counts[l._dist] = (counts[l._dist] || 0) + 1; });
    store.distCounts = counts;
    var bt = Object.create(null);
    list.forEach(function (l) { if (l.active && l._bt) bt[l._bt] = (bt[l._bt] || 0) + 1; });
    store.btCounts = bt; // typy budynków / stany bez ani jednej oferty nie pokazują się w filtrach
    var cd = Object.create(null);
    list.forEach(function (l) { if (l.active && l._cond) cd[l._cond] = (cd[l._cond] || 0) + 1; });
    store.condCounts = cd;
    store.optCounts = null;
    var srcs = Object.create(null);
    list.forEach(function (l) { srcs[l.source] = 1; });
    store.sources = Object.keys(srcs).sort(function (a, b) { return srcName(a).localeCompare(srcName(b), "pl"); });
  }

  // Jedyne miejsce, które buduje adresy plików z danymi (meta.json, <typ>.json):
  // data/<miejscowość>/<plik>, a przy starym układzie (brak data/cities.json) — data/<plik>.
  function dataUrl(name) { return CITIES && CITY ? "data/" + CITY + "/" + name : "data/" + name; }

  function fetchJson(url) {
    return fetch(url + "?t=" + Math.floor(Date.now() / 60000), { cache: "no-cache" })
      .then(function (r) {
        if (r.status === 404) return { missing: true };
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json().then(function (j) { return { data: j }; });
      });
  }

  function loadDeal(deal) {
    if (STORE[deal]) return STORE[deal].promise;
    var store = STORE[deal] = { status: "loading", ALL: [], BY_KEY: Object.create(null), GROUPS: Object.create(null), distCounts: {}, sources: [] };
    store.promise = fetchJson(dataUrl(deal + ".json"))
      .then(function (res) {
        if (res.missing && deal === "sprzedaz") return fetchJson(dataUrl("listings.json")); // stary format
        return res;
      })
      .then(function (res) {
        return metaReady.then(function () { return res; });
      })
      .then(function (res) {
        var list = res.data;
        if (list && !Array.isArray(list) && Array.isArray(list.listings)) list = list.listings;
        if (!Array.isArray(list)) list = [];
        store.ALL = list.filter(function (l) { return l && l.key; });
        prepare(store, store.ALL, deal);
        store.status = "ready";
      })
      .catch(function (e) {
        if (window.console) console.warn("Nie udało się wczytać ofert", deal, e);
        store.status = "error";
      });
    return store.promise;
  }

  function groupOf(l) { return l.dup_group && D.GROUPS[l.dup_group] ? D.GROUPS[l.dup_group] : [l]; }
  function anyIn(set, l) {
    var g = groupOf(l);
    for (var i = 0; i < g.length; i++) if (set[g[i].key]) return true;
    return false;
  }
  function isFav(l) { return anyIn(fav, l); }
  function isHidden(l) { return anyIn(hidden, l); }
  function isSeen(l) { return anyIn(seen, l); }

  // ------------------------------------------------------------------ filtrowanie
  // piętro wpisane przez użytkownika: liczba albo „parter” (= 0)
  function floorNum(s) {
    if (s == null || String(s).trim() === "") return null;
    if (/^\s*p/i.test(String(s))) return 0;
    var n = num(s);
    return n == null ? null : Math.round(n);
  }
  function featOptions() {
    return FEATS.filter(function (f) { return cfg().monthly || !RENT_ONLY_FEATS[f[0]]; });
  }
  function activeFeats() {
    var ok = featOptions().map(function (f) { return f[0]; });
    return F.feat.filter(function (f) { return ok.indexOf(f) >= 0; });
  }

  function applyFilters() {
    var c = cfg();
    var pmin = num(F.pmin), pmax = num(F.pmax), amin = num(F.amin), amax = num(F.amax);
    var ppm = c.ppm ? num(F.ppm) : null;
    var q = norm(F.q).trim();
    var words = q ? q.split(/\s+/) : [];
    var rooms = c.rooms ? F.rooms : [], dists = toSet(F.districts), market = c.market ? F.market : [];
    var seller = F.seller, srcs = F.sources;
    var minDate = F.days ? Date.now() - F.days * DAY : 0;
    var favTab = F.tab === "fav";
    var ALL = D.ALL;
    var fmin = floorNum(F.fmin), fmax = floorNum(F.fmax);
    var furn = F.furn, conds = F.cond, bts = F.btype, feats = activeFeats();

    // Liczniki przy opcjach cech: ile ofert byłoby po zaznaczeniu danej opcji przy pozostałych
    // aktywnych filtrach. Oferta z kilku portali liczy się raz (tak jak na liście).
    var cnt = { furn: {}, cond: {}, btype: {}, feat: {} };
    function bump(group, opt, gk) { var o = cnt[group][opt] || (cnt[group][opt] = Object.create(null)); o[gk] = 1; }

    var out = [];
    for (var i = 0; i < ALL.length; i++) {
      var l = ALL[i];
      if (favTab) {
        if (!isFav(l)) continue;
      } else {
        if (!F.showInactive && !l.active) continue;
        if (!F.showHidden && isHidden(l)) continue;
      }
      if (pmin != null && !(l.price >= pmin)) continue;
      if (pmax != null && !(l.price && l.price <= pmax)) continue;
      if (amin != null && !(l.area >= amin)) continue;
      if (amax != null && !(l.area && l.area <= amax)) continue;
      if (ppm != null && !(l._ppm && l._ppm <= ppm)) continue;
      if (rooms.length) {
        var r = l.rooms;
        var rk = r == null ? "" : (r >= 5 ? "5+" : String(r));
        if (rooms.indexOf(rk) < 0) continue;
      }
      if (F.districts.length && !dists[l._dist]) continue;
      if (market.length && market.indexOf(l.market) < 0) continue;
      if (seller.length && seller.indexOf(l.seller) < 0) continue;
      if (srcs.length && srcs.indexOf(l.source) < 0) continue;
      if (minDate && !(l._date >= minDate)) continue;
      if (words.length) {
        var ok = true;
        for (var w = 0; w < words.length; w++) if (l._search.indexOf(words[w]) < 0) { ok = false; break; }
        if (!ok) continue;
      }
      // piętro: zakres wymaga znanego piętra; „bez parteru / ostatniego” odrzuca tylko pewne przypadki
      if ((fmin != null || fmax != null) && (l._fl == null || (fmin != null && l._fl < fmin) || (fmax != null && l._fl > fmax))) continue;
      if (F.noGround && l._fl != null && l._fl <= 0) continue;
      if (F.noTop && l._fl != null && l._flTot != null && l._fl >= l._flTot) continue;

      var okFurn = !furn || (furn === "tak" ? l.furnished === true : l.furnished === false);
      var okCond = !conds.length || conds.indexOf(l._cond) >= 0;
      var okBt = !bts.length || bts.indexOf(l._bt) >= 0;
      var okFeat = true;
      for (var fi = 0; fi < feats.length; fi++) if (!l._feat[feats[fi]]) { okFeat = false; break; }
      var gk = F.hideDups && l.dup_group ? "g" + l.dup_group : l.key;
      if (okCond && okBt && okFeat && typeof l.furnished === "boolean") bump("furn", l.furnished ? "tak" : "nie", gk);
      if (okFurn && okBt && okFeat && l._cond) bump("cond", l._cond, gk);
      if (okFurn && okCond && okFeat && l._bt) bump("btype", l._bt, gk);
      if (okFurn && okCond && okBt && okFeat) for (var fk in l._feat) bump("feat", fk, gk);
      if (!(okFurn && okCond && okBt && okFeat)) continue;
      out.push(l);
    }

    var oc = {};
    Object.keys(cnt).forEach(function (g) {
      oc[g] = {};
      Object.keys(cnt[g]).forEach(function (o) { oc[g][o] = Object.keys(cnt[g][o]).length; });
    });
    D.optCounts = oc;

    sortList(out);

    if (F.hideDups) {
      var used = Object.create(null), dedup = [];
      for (var j = 0; j < out.length; j++) {
        var g = out[j].dup_group;
        if (g) {
          if (used[g]) continue;
          used[g] = 1;
        }
        dedup.push(out[j]);
      }
      out = dedup;
    }
    return out;
  }

  function cmpNum(a, b, asc) {
    if (a == null && b == null) return 0;
    if (a == null) return 1;
    if (b == null) return -1;
    return asc ? a - b : b - a;
  }
  function sortOptions() {
    var c = cfg();
    var o = [["newest", "Najnowsze"], ["price_asc", "Najtańsze"], ["price_desc", "Najdroższe"]];
    if (c.ppm) o.push(["ppm_asc", "Najtańsze za m²"]);
    o.push(["area_desc", "Największe"], ["area_asc", "Najmniejsze"]);
    return o;
  }
  function sortList(arr) {
    var s = F.sort;
    arr.sort(function (a, b) {
      var c = 0;
      if (s === "price_asc") c = cmpNum(a.price, b.price, true);
      else if (s === "price_desc") c = cmpNum(a.price, b.price, false);
      else if (s === "ppm_asc") c = cmpNum(a._ppm, b._ppm, true);
      else if (s === "area_desc") c = cmpNum(a.area, b.area, false);
      else if (s === "area_asc") c = cmpNum(a.area, b.area, true);
      return c || (b._date - a._date) || (b._first - a._first) || (a.key < b.key ? -1 : 1);
    });
  }

  // ------------------------------------------------------------------ karty
  function priceHtml(l, cls) {
    var c = cfg();
    if (!l.price) return '<span class="' + cls + '">Cena do negocjacji</span>';
    return '<span class="' + cls + '">' + nf.format(l.price) + " zł" + (c.monthly ? "<small> / mies.</small>" : "") + "</span>";
  }

  function factsList(l) {
    var c = cfg(), facts = [];
    if (l.area) facts.push(nf1.format(l.area) + " m²");
    if (c.rooms && l.rooms) facts.push(l.rooms + " " + plural(l.rooms, "pokój", "pokoje", "pokoi"));
    if (l._ppm) facts.push(nf.format(l._ppm) + " zł/m²");
    return facts;
  }

  // krótka linia cech na karcie, np. „piętro 3/5 · balkon · winda · umeblowane” (maks. 4 pozycje)
  var FEAT_WORD = { balkon: "balkon", taras: "taras", loggia: "loggia", ogrodek: "ogródek", winda: "winda",
    garaz: "garaż", parking: "parking", piwnica: "piwnica", komorka: "komórka", klimatyzacja: "klimatyzacja",
    zwierzeta: "zwierzęta OK" };
  function floorText(l) {
    if (l._fl != null) {
      if (l._fl < 0) return "suterena";
      if (l._fl === 0) return "parter";
      return "piętro " + l._fl + (l._flTot && l._fl <= l._flTot ? "/" + l._flTot : "");
    }
    if (l.floor != null && l.floor !== "") {
      var fl = String(l.floor).trim();
      return /^(0|parter)$/i.test(fl) ? "parter" : (/^\d/.test(fl) ? "piętro " + fl : fl.toLowerCase());
    }
    return "";
  }
  function featLine(l) {
    var c = cfg(), out = [];
    var f = toSet(Array.isArray(l.features) ? l.features : []);
    function pick() { for (var i = 0; i < arguments.length; i++) if (f[arguments[i]]) return FEAT_WORD[arguments[i]]; return ""; }
    var fl = floorText(l);
    if (fl) out.push(fl);
    var furn = l.furnished === true ? "umeblowane" : l.furnished === false ? "bez mebli" : "";
    var cond = l.condition === "do_remontu" ? "do remontu"
      : (l.condition === "stan_deweloperski" || l.condition === "do_wykonczenia") ? "stan deweloperski" : "";
    var cand = c.monthly
      ? [furn, pick("balkon", "taras", "loggia"), pick("winda"), pick("zwierzeta"), pick("ogrodek"), pick("garaz", "parking"), pick("klimatyzacja")]
      : [cond, pick("balkon", "taras", "loggia"), pick("winda"), pick("ogrodek"), pick("garaz", "parking"), pick("piwnica", "komorka"), furn];
    cand.forEach(function (x) { if (x && out.length < 4) out.push(x); });
    return out;
  }

  function cardHtml(l) {
    var c = cfg();
    var favOn = isFav(l), hid = isHidden(l), seenOn = isSeen(l);
    var img = l.images && l.images[0];
    var url = safeUrl(l.url);
    var cls = "card" + (seenOn ? " seen" : "") + (hid ? " is-hidden" : "") + (l.active ? "" : " inactive");
    var isNew = l._date && Date.now() - l._date < DAY;

    var badges = "";
    if (!l.active) badges += '<span class="badge gone">Nieaktualna</span>';
    else if (isNew) badges += '<span class="badge new">Nowe</span>';
    if (l._oldPrice && l.active) badges += '<span class="badge">Obniżka</span>';
    if (seenOn) badges += '<span class="badge">Oglądane</span>';

    var price = priceHtml(l, "price");
    if (l._oldPrice) {
      var pct = Math.round((1 - l.price / l._oldPrice) * 100);
      price += '<span class="old-price">' + nf.format(l._oldPrice) + " zł</span>" + (pct > 0 ? '<span class="drop">−' + pct + "%</span>" : "");
    }

    var facts = factsList(l);
    var extra = [];
    if (c.monthly) {
      if (l.rent_extra) extra.push("+ " + zl(l.rent_extra) + " opłaty");
      if (l.deposit) extra.push("kaucja " + zl(l.deposit));
      if (l.available_from) { var af = fmtDay(l.available_from); if (af) extra.push(af); }
    }

    var loc = [l.district, l.street].filter(Boolean).map(esc).join(", ");
    var feats = featLine(l);
    var meta = [];
    if (c.market && l.market) meta.push(l.market === "wtorny" ? "rynek wtórny" : "rynek " + esc(l.market));
    if (l.seller) meta.push(l.seller === "prywatny" ? "prywatnie" : esc(l.seller));
    if (l.build_year) meta.push(esc(l.build_year) + " r.");

    var group = groupOf(l);
    var srcHtml = '<a class="src" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer" data-open="' + esc(l.key) + '">' + esc(srcName(l.source)) + "</a>";
    var others = group.filter(function (o) { return o.key !== l.key && (o.active || !l.active); });
    if (others.length) {
      srcHtml += '<span class="also">też na</span>' + others.map(function (o) {
        var t = o.price && o.price !== l.price ? " — " + zl(o.price) : "";
        return '<a class="src other" href="' + esc(safeUrl(o.url)) + '" target="_blank" rel="noopener noreferrer" data-open="' + esc(o.key) +
          '" title="' + esc(srcName(o.source) + t) + '">' + esc(srcName(o.source)) + "</a>";
      }).join('<span class="also">·</span>');
    }

    return '<article class="' + cls + '" data-key="' + esc(l.key) + '">' +
      '<a class="thumb" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer" data-open="' + esc(l.key) + '" tabindex="-1" aria-hidden="true">' +
        (img
          ? '<img src="' + esc(safeUrl(img)) + '" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">'
          : '<span class="noimg">' + icon("photo") + "</span>") +
        (badges ? '<span class="badges">' + badges + "</span>" : "") +
      "</a>" +
      '<div class="acts">' +
        '<button type="button" class="act hide" data-act="hide" title="' + (hid ? "Przywróć ofertę" : "Ukryj — nie interesuje mnie") + '" aria-label="' + (hid ? "Przywróć" : "Ukryj") + '">' + icon(hid ? "undo" : "eye-off") + "</button>" +
        '<button type="button" class="act fav' + (favOn ? " on" : "") + '" data-act="fav" title="' + (favOn ? "Usuń z ulubionych" : "Dodaj do ulubionych") + '" aria-pressed="' + favOn + '" aria-label="Ulubione">' + icon("heart") + "</button>" +
      "</div>" +
      '<div class="card-body">' +
        '<div class="price-row">' + price + "</div>" +
        (facts.length ? '<div class="facts">' + facts.join('<span class="sep">·</span>') + "</div>" : "") +
        (feats.length ? '<div class="feats">' + feats.map(esc).join('<span class="sep">·</span>') + "</div>" : "") +
        (extra.length ? '<div class="rent-extra">' + extra.join(" · ") + "</div>" : "") +
        (loc ? '<div class="loc">' + loc + "</div>" : "") +
        '<a class="title" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer" data-open="' + esc(l.key) + '">' + esc(l.title || c.noun) + "</a>" +
        (meta.length ? '<div class="meta">' + meta.join(" · ") + "</div>" : "") +
        '<div class="card-foot"><div class="srcs">' + srcHtml + "</div>" +
          '<span class="age">' + esc(l._date ? ago(l._date) : "") + "</span></div>" +
      "</div>" +
    "</article>";
  }

  function skeleton(n) {
    var s = "";
    for (var i = 0; i < n; i++) {
      s += '<div class="card skel" aria-hidden="true"><div class="thumb"></div><div class="sk" style="width:45%;height:18px"></div>' +
        '<div class="sk" style="width:70%"></div><div class="sk" style="width:55%"></div></div>';
    }
    return s;
  }

  // ------------------------------------------------------------------ renderowanie
  var grid = $("grid");

  function render() {
    syncControls();
    updatePills();
    if (!D || D.status === "loading") {
      grid.innerHTML = skeleton(8);
      $("empty").hidden = true;
      $("moreBtn").hidden = true;
      $("resultCount").innerHTML = "Wczytywanie ofert…";
      document.querySelector(".result-bar").hidden = false;
      return;
    }
    filtered = D.status === "ready" ? applyFilters() : [];
    renderOptionChips();
    shown = 0;
    document.querySelector(".result-bar").hidden = !D.ALL.length;
    grid.innerHTML = "";
    renderCount();
    renderEmpty();
    renderChips();
    updatePills();
    updateFavCount();
    if (F.view === "map") renderMap();
    else renderMore();
  }

  function renderMore() {
    if (F.view !== "list") return;
    var next = filtered.slice(shown, shown + PAGE);
    if (next.length) grid.insertAdjacentHTML("beforeend", next.map(cardHtml).join(""));
    shown += next.length;
    $("moreBtn").hidden = shown >= filtered.length;
    if (!$("moreBtn").hidden) $("moreBtn").textContent = "Pokaż więcej (" + nf.format(filtered.length - shown) + ")";
  }

  function rerenderCard(key) {
    var el = grid.querySelector('[data-key="' + cssEsc(key) + '"]');
    var l = D.BY_KEY[key];
    if (!el || !l) return;
    el.outerHTML = cardHtml(l);
  }
  function rerenderGroup(l) { groupOf(l).forEach(function (o) { rerenderCard(o.key); }); }

  function renderCount() {
    var n = filtered.length;
    $("resultCount").innerHTML = "<b>" + nf.format(n) + "</b> " + plural(n, "oferta", "oferty", "ofert");
    var label = n ? "Pokaż " + nf.format(n) + " " + plural(n, "ofertę", "oferty", "ofert") : "Brak ofert";
    $$("[data-showbtn]").forEach(function (b) { b.textContent = label; });
  }

  function emptyBox(ico, title, text, buttons) {
    return '<div class="ico">' + icon(ico) + "</div><h3>" + title + "</h3><p>" + text + "</p>" + (buttons || "");
  }
  function renderEmpty() {
    var box = $("empty");
    if (filtered.length) { box.hidden = true; return; }
    box.hidden = false;
    var c = cfg();
    if (D.status === "error") {
      box.innerHTML = emptyBox("alert", "Nie udało się wczytać ofert",
        "Sprawdź połączenie z internetem i odśwież stronę." +
        (location.protocol === "file:" ? " Stronę trzeba otworzyć przez serwer (np. Vercel), a nie bezpośrednio z pliku." : ""),
        '<button type="button" class="btn-outline" data-reload>Odśwież</button>');
    } else if (!D.ALL.length) {
      box.innerHTML = emptyBox("home", "Jeszcze nie ma " + c.what,
        "Oferty pobierają się automatycznie kilka razy dziennie — pojawią się tu po najbliższej aktualizacji.");
    } else if (F.tab === "fav") {
      box.innerHTML = emptyBox("heart", "Nie masz jeszcze ulubionych",
        "Kliknij serduszko na zdjęciu oferty, żeby ją tu zapisać." + (activeCount() ? " Możliwe też, że ukrywają je filtry." : ""),
        (activeCount() ? '<button type="button" class="btn-outline" data-clear>Wyczyść filtry</button>' : "") +
        '<button type="button" class="btn-outline" data-allbtn>Wszystkie oferty</button>');
    } else {
      box.innerHTML = emptyBox("search", "Nic nie pasuje do filtrów",
        "Spróbuj poszerzyć widełki ceny lub metrażu albo wybrać więcej dzielnic.",
        '<button type="button" class="btn-outline" data-clear>Wyczyść filtry</button>');
    }
  }

  function updateFavCount() {
    var n = 0, groupsSeen = Object.create(null);
    Object.keys(fav).forEach(function (k) {
      var l = D.BY_KEY[k];
      if (!l) return;
      if (l.dup_group) { if (groupsSeen[l.dup_group]) return; groupsSeen[l.dup_group] = 1; }
      n++;
    });
    $("favCount").textContent = n;
    var h = 0;
    Object.keys(hidden).forEach(function (k) { if (D.BY_KEY[k]) h++; });
    $("hiddenCount").textContent = h ? "(" + h + ")" : "";
  }

  // ------------------------------------------------------------------ opis aktywnych filtrów
  function rangeText(a, b, fmt) {
    var x = num(a), y = num(b);
    if (x != null && y != null) return fmt(x) + " – " + fmt(y);
    if (x != null) return "od " + fmt(x);
    if (y != null) return "do " + fmt(y);
    return "";
  }
  function priceFmt(n) { return cfg().monthly ? nf.format(n) + " zł" : shortZl(n); }
  function areaFmt(n) { return nf1.format(n) + " m²"; }
  function roomsText() { return F.rooms.slice().sort().join(", ") + " pok."; }
  function label(list, k) { return (list.filter(function (x) { return x[0] === k; })[0] || [k, k])[1]; }
  function floorRangeText() {
    var a = floorNum(F.fmin), b = floorNum(F.fmax);
    function f(n) { return n === 0 ? "parter" : String(n); }
    if (a != null && b != null) return a === b ? (a === 0 ? "Parter" : "Piętro " + a) : "Piętro " + f(a) + "–" + f(b);
    if (a != null) return a === 0 ? "Znane piętro" : "Piętro od " + a;
    if (b != null) return b === 0 ? "Parter" : "Piętro do " + b;
    return "";
  }
  // pigułka „Piętro” jest na pasku tylko na szerokim ekranie; węższy komputer ma piętro w „Więcej filtrów”
  function floorInMore() { return window.matchMedia("(min-width: 900px) and (max-width: 1239px)").matches; }

  // lista aktywnych filtrów: [etykieta, funkcja-usuwająca, grupa („main” / „more”)]
  function activeList() {
    var c = cfg(), out = [];
    if (F.q.trim()) out.push(["„" + F.q.trim() + "”", function () { F.q = ""; }, "q"]);
    var pr = rangeText(F.pmin, F.pmax, priceFmt);
    if (pr) out.push([pr, function () { F.pmin = F.pmax = ""; }, "main"]);
    if (c.rooms && F.rooms.length) out.push([roomsText(), function () { F.rooms = []; }, "main"]);
    var ar = rangeText(F.amin, F.amax, areaFmt);
    if (ar) out.push([ar, function () { F.amin = F.amax = ""; }, "main"]);
    F.districts.forEach(function (d) {
      out.push([d, function () { F.districts = F.districts.filter(function (x) { return x !== d; }); }, "main"]);
    });
    var fr = floorRangeText();
    if (fr) out.push([fr, function () { F.fmin = F.fmax = ""; }, "floor"]);
    if (F.noGround) out.push(["Bez parteru", function () { F.noGround = false; }, "floor"]);
    if (F.noTop) out.push(["Bez ostatniego piętra", function () { F.noTop = false; }, "floor"]);
    activeFeats().forEach(function (k) {
      out.push([label(FEATS, k), function () { F.feat = F.feat.filter(function (x) { return x !== k; }); }, "more"]);
    });
    if (F.furn) out.push([F.furn === "tak" ? "Umeblowane" : "Nieumeblowane", function () { F.furn = ""; }, "more"]);
    F.cond.forEach(function (k) {
      out.push([label(CONDS, k), function () { F.cond = F.cond.filter(function (x) { return x !== k; }); }, "more"]);
    });
    F.btype.forEach(function (k) {
      out.push([label(BTYPES, k), function () { F.btype = F.btype.filter(function (x) { return x !== k; }); }, "more"]);
    });
    if (c.ppm && num(F.ppm) != null) out.push(["do " + nf.format(num(F.ppm)) + " zł/m²", function () { F.ppm = ""; }, "more"]);
    if (F.days) {
      var dl = DAYS.filter(function (d) { return d[0] === String(F.days); })[0];
      out.push(["Dodane: " + (dl ? dl[1].toLowerCase() : F.days + " dni"), function () { F.days = 0; }, "more"]);
    }
    if (c.market) F.market.forEach(function (m) {
      out.push(["Rynek " + (m === "wtorny" ? "wtórny" : m), function () { F.market = F.market.filter(function (x) { return x !== m; }); }, "more"]);
    });
    F.seller.forEach(function (s) {
      var lab = (c.sellers.filter(function (x) { return x[0] === s; })[0] || [s, s])[1];
      out.push([lab, function () { F.seller = F.seller.filter(function (x) { return x !== s; }); }, "more"]);
    });
    F.sources.forEach(function (s) {
      out.push([srcName(s), function () { F.sources = F.sources.filter(function (x) { return x !== s; }); }, "more"]);
    });
    return out;
  }
  function activeCount() { return activeList().length; }

  var chipActions = [];
  function renderChips() {
    var list = activeList();
    chipActions = list.map(function (x) { return x[1]; });
    var box = $("activeChips");
    box.hidden = !list.length;
    box.innerHTML = list.map(function (x, i) {
      return '<button type="button" class="fchip" data-chip="' + i + '" title="Usuń filtr"><span>' + esc(x[0]) + "</span>" + icon("x") + "</button>";
    }).join("") + (list.length > 1 ? '<button type="button" class="chip-clear" data-clear>Wyczyść wszystko</button>' : "");
  }

  function updatePills() {
    var c = cfg();
    function set(id, on, text, def) {
      var b = $(id);
      b.classList.toggle("on", !!on);
      b.querySelector(".pill-txt").textContent = on ? text : def;
    }
    var pr = rangeText(F.pmin, F.pmax, priceFmt);
    set("pillPrice", pr, pr, c.monthly ? "Czynsz" : "Cena");
    set("pillRooms", c.rooms && F.rooms.length, roomsText(), "Pokoje");
    var ar = rangeText(F.amin, F.amax, areaFmt);
    set("pillArea", ar, ar, "Metraż");
    var dn = F.districts.length;
    set("pillDist", dn, dn === 1 ? F.districts[0] : dn + " " + plural(dn, "dzielnica", "dzielnice", "dzielnic"), "Dzielnica");
    var list = activeList();
    var fl = list.filter(function (x) { return x[2] === "floor"; });
    set("pillFloor", fl.length, fl.length ? fl[0][0] + (fl.length > 1 ? " +" + (fl.length - 1) : "") : "", "Piętro");
    var inMore = floorInMore();
    var more = list.filter(function (x) { return x[2] === "more" || (inMore && x[2] === "floor"); }).length;
    var all = list.filter(function (x) { return x[2] !== "q"; }).length;
    $("moreCount").hidden = !more; $("moreCount").textContent = more;
    $("allCount").hidden = !all; $("allCount").textContent = all;
  }

  // ------------------------------------------------------------------ kontrolki filtrów
  function chipsHtml(items, key, multi) {
    return items.map(function (it) {
      var on = multi ? F[key].indexOf(it[0]) >= 0 : String(F[key]) === it[0];
      return '<button type="button" class="chip" data-key="' + key + '" data-val="' + esc(it[0]) + '" aria-pressed="' + on + '">' + esc(it[1]) + "</button>";
    }).join("");
  }
  // przyciski opcji cech z licznikiem ofert
  function optChipsHtml(items, key, counts, multi) {
    return items.map(function (it) {
      var on = multi ? F[key].indexOf(it[0]) >= 0 : F[key] === it[0];
      var n = it[0] && counts ? counts[it[0]] || 0 : null;
      return '<button type="button" class="chip' + (n === 0 && !on ? " zero" : "") + '" data-key="' + key + '" data-val="' + esc(it[0]) +
        '" aria-pressed="' + on + '">' + esc(it[1]) + (n != null ? '<span class="cnt">' + nf.format(n) + "</span>" : "") + "</button>";
    }).join("");
  }
  function renderOptionChips() {
    var oc = (D && D.optCounts) || null;
    var btAll = (D && D.btCounts) || {};
    var bts = BTYPES.filter(function (b) { return btAll[b[0]] || F.btype.indexOf(b[0]) >= 0; });
    var cdAll = (D && D.condCounts) || {};
    var conds = CONDS.filter(function (x) { return cdAll[x[0]] || F.cond.indexOf(x[0]) >= 0; });
    $$('[data-chips="feat"]').forEach(function (el) { el.innerHTML = optChipsHtml(featOptions(), "feat", oc && oc.feat, true); });
    $$('[data-chips="furn"]').forEach(function (el) { el.innerHTML = optChipsHtml(FURN, "furn", oc && oc.furn, false); });
    $$('[data-chips="cond"]').forEach(function (el) { el.innerHTML = optChipsHtml(conds, "cond", oc && oc.cond, true); });
    $$('[data-sec="cond"]').forEach(function (s) { s.hidden = !conds.length; });
    $$('[data-chips="btype"]').forEach(function (el) { el.innerHTML = optChipsHtml(bts, "btype", oc && oc.btype, true); });
    $$('[data-sec="btype"]').forEach(function (s) { s.hidden = !bts.length; });
  }

  function presetsHtml(key) {
    var c = cfg();
    var vals = key === "pmax" ? c.pricePresets : c.areaPresets;
    var pre = key === "pmax" ? "do " : "od ";
    var fmt = key === "pmax" ? priceFmt : areaFmt;
    return vals.map(function (v) {
      var on = num(F[key]) === v;
      return '<button type="button" class="preset" data-pf="' + key + '" data-val="' + v + '" aria-pressed="' + on + '">' + pre + esc(fmt(v)) + "</button>";
    }).join("");
  }

  function renderDistrictLists() {
    var counts = D ? D.distCounts : {};
    var all = Object.create(null);
    Object.keys(counts).forEach(function (d) { all[d] = counts[d]; });
    F.districts.forEach(function (d) { if (!(d in all)) all[d] = 0; });
    var sel = toSet(F.districts);
    var rows = Object.keys(all).sort(function (a, b) {
      return ((sel[b] ? 1 : 0) - (sel[a] ? 1 : 0)) || a.localeCompare(b, "pl");
    });
    $$(".dist-list").forEach(function (box) {
      var input = box.parentNode.querySelector(".dist-search");
      var q = norm(input ? input.value : "").trim();
      var r = rows.filter(function (d) { return !q || norm(d).indexOf(q) >= 0; });
      box.innerHTML = r.length ? r.map(function (d) {
        return '<label><input type="checkbox" data-dist="' + esc(d) + '"' + (sel[d] ? " checked" : "") + "> <span>" +
          esc(d) + '</span><span class="cnt">' + nf.format(all[d]) + "</span></label>";
      }).join("") : '<div class="none">' + (rows.length ? "Brak pasujących dzielnic" : "Brak danych o dzielnicach") + "</div>";
    });
  }

  // liczby w polach pokazujemy z odstępami (700 000), w pamięci trzymamy to, co wpisano
  function inputText(k) {
    var v = F[k];
    if (k === "q" || v === "" || v == null) return v || "";
    var n = num(v);
    return n == null ? v : (k === "amin" || k === "amax" ? nf1.format(n) : nf.format(n));
  }
  document.addEventListener("focusout", function (e) {
    var k = e.target.getAttribute && e.target.getAttribute("data-f");
    if (k && k !== "q") e.target.value = inputText(k);
  });

  // Ustawia wszystkie kontrolki zgodnie z F i bieżącym typem ogłoszeń.
  function syncControls() {
    var c = cfg();
    var active = document.activeElement;
    $$("[data-f]").forEach(function (el) {
      if (el !== active) el.value = inputText(el.getAttribute("data-f"));
    });
    $$("[data-b]").forEach(function (el) { el.checked = !!F[el.getAttribute("data-b")]; });
    $$('[data-chips="rooms"]').forEach(function (el) { el.innerHTML = chipsHtml(ROOMS, "rooms", true); });
    $$('[data-chips="days"]').forEach(function (el) { el.innerHTML = chipsHtml(DAYS, "days", false); });
    $$('[data-chips="market"]').forEach(function (el) { el.innerHTML = chipsHtml(MARKETS, "market", true); });
    $$('[data-chips="seller"]').forEach(function (el) { el.innerHTML = chipsHtml(c.sellers, "seller", true); });
    var srcs = (D && D.sources ? D.sources.slice() : []);
    F.sources.forEach(function (s) { if (srcs.indexOf(s) < 0) srcs.push(s); });
    $$('[data-chips="sources"]').forEach(function (el) {
      el.innerHTML = srcs.length ? chipsHtml(srcs.map(function (s) { return [s, srcName(s)]; }), "sources", true) : '<span class="muted">—</span>';
    });
    $$("[data-presets]").forEach(function (el) { el.innerHTML = presetsHtml(el.getAttribute("data-presets")); });
    renderDistrictLists();

    // elementy zależne od typu ogłoszeń
    $("pillRooms").hidden = !c.rooms;
    $$('[data-sec="rooms"]').forEach(function (s) { s.hidden = !c.rooms; });
    $$('[data-sec="ppm"]').forEach(function (s) { s.hidden = !c.ppm; });
    $$('[data-sec="market"]').forEach(function (s) { s.hidden = !c.market; });
    $$(".price-title").forEach(function (h) { h.textContent = c.monthly ? "Czynsz miesięcznie (zł)" : "Cena (zł)"; });
    renderOptionChips();
    // przy najmie „Umeblowane” jest ważniejsze niż udogodnienia — idzie wyżej
    var body = $("drawer").querySelector(".drawer-body");
    var secFeat = body.querySelector('[data-sec="feat"]'), secFurn = body.querySelector('[data-sec="furn"]');
    if (c.monthly) body.insertBefore(secFurn, secFeat); else body.insertBefore(secFurn, body.querySelector('[data-sec="days"]'));
    var lead = false;
    $$(".fsec", body).forEach(function (s) {
      var first = !lead && !s.hidden && !s.classList.contains("mobile-sec") && !s.classList.contains("floor-sec");
      s.classList.toggle("lead", first);
      if (first) lead = true;
    });

    var sel = $("sort"), opts = sortOptions();
    sel.innerHTML = opts.map(function (o) { return '<option value="' + o[0] + '">' + o[1] + "</option>"; }).join("");
    if (!opts.some(function (o) { return o[0] === F.sort; })) F.sort = "newest";
    sel.value = F.sort;

    $$("#dealSwitch [data-deal]").forEach(function (b) {
      var d = b.getAttribute("data-deal");
      b.setAttribute("aria-selected", String(d === "rent" ? isRent(DEAL) : d === DEAL));
    });
    $$(".sub-switch [data-sub]").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-sub") === DEAL));
    });
    $$(".sub-switch").forEach(function (el) { el.hidden = !isRent(DEAL) || rentSubs().length < 2; });
    $$(".view-switch [data-view]").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-view") === F.view));
    });
    $("favToggle").setAttribute("aria-pressed", String(F.tab === "fav"));
    $("favBanner").hidden = F.tab !== "fav";
    $("listView").hidden = F.view !== "list";
    $("mapView").hidden = F.view !== "map";
    renderStatus();
  }

  var renderTimer = null;
  function changed(debounce) {
    persistFilters();
    clearTimeout(renderTimer);
    if (debounce) renderTimer = setTimeout(render, 200);
    else render();
  }

  function clearFilters() {
    var keep = { tab: F.tab, view: F.view, sort: F.sort, hideDups: F.hideDups };
    F = cloneDefaults();
    Object.keys(keep).forEach(function (k) { F[k] = keep[k]; });
    $$(".dist-search").forEach(function (i) { i.value = ""; });
    changed();
  }

  // ------------------------------------------------------------------ panele filtrów
  var openName = null;
  function openPanel(name, anchor) {
    closePanel();
    openName = name;
    var main = ["price", "rooms", "area", "dist", "floor"].indexOf(name) >= 0;
    if (!main || !isDesktop()) {
      $("drawer").hidden = false;
      $("scrim").hidden = false;
      $("scrim").classList.remove("light");
      document.body.classList.add("lock");
      var body = $("drawer").querySelector(".drawer-body");
      var sec = main ? $("drawer").querySelector('[data-sec="' + name + '"]') : null;
      body.scrollTop = sec ? sec.offsetTop - body.offsetTop - 4 : 0;
    } else {
      var pop = $("pop-" + name);
      pop.hidden = false;
      var bar = $("filterbar").getBoundingClientRect();
      var r = anchor.getBoundingClientRect();
      var w = pop.offsetWidth;
      var left = Math.min(r.left - bar.left, window.innerWidth - w - 16 - bar.left);
      pop.style.left = Math.max(16, left) + "px";
      anchor.setAttribute("aria-expanded", "true");
      $("scrim").hidden = false;
      $("scrim").classList.add("light");
      var first = pop.querySelector("input");
      if (first && name !== "dist") first.focus();
    }
  }
  function closePanel() {
    if (!openName) return;
    openName = null;
    $$(".pop").forEach(function (p) { p.hidden = true; });
    $("drawer").hidden = true;
    $("scrim").hidden = true;
    document.body.classList.remove("lock");
    $$(".pill[data-open]").forEach(function (b) { b.setAttribute("aria-expanded", "false"); });
  }

  function bindFilters() {
    document.addEventListener("input", function (e) {
      var t = e.target, k = t.getAttribute && t.getAttribute("data-f");
      if (k) {
        F[k] = t.value;
        $$('[data-f="' + k + '"]').forEach(function (el) { if (el !== t) el.value = inputText(k); });
        persistFilters();
        clearTimeout(renderTimer);
        renderTimer = setTimeout(function () {
          render();
        }, 220);
      } else if (t.classList && t.classList.contains("dist-search")) {
        renderDistrictLists();
      }
    });
    document.addEventListener("change", function (e) {
      var t = e.target;
      var b = t.getAttribute && t.getAttribute("data-b");
      if (b) { F[b] = t.checked; changed(); return; }
      var d = t.getAttribute && t.getAttribute("data-dist");
      if (d != null) {
        var i = F.districts.indexOf(d);
        if (t.checked && i < 0) F.districts.push(d);
        if (!t.checked && i >= 0) F.districts.splice(i, 1);
        changed();
      }
    });
    $("sort").addEventListener("change", function (e) { F.sort = e.target.value; changed(); window.scrollTo(0, 0); });

    document.addEventListener("click", function (e) {
      var t = e.target;
      var chip = t.closest(".chip[data-key]");
      if (chip) {
        var key = chip.getAttribute("data-key"), val = chip.getAttribute("data-val");
        if (key === "days") F.days = parseInt(val, 10) || 0;
        else if (key === "furn") F.furn = F.furn === val ? "" : val;
        else {
          var arr = F[key], i = arr.indexOf(val);
          if (i >= 0) arr.splice(i, 1); else arr.push(val);
        }
        changed();
        return;
      }
      var pre = t.closest(".preset");
      if (pre) {
        var pf = pre.getAttribute("data-pf"), v = pre.getAttribute("data-val");
        F[pf] = num(F[pf]) === Number(v) ? "" : v;
        changed();
        return;
      }
      var rs = t.closest("[data-reset]");
      if (rs) {
        rs.getAttribute("data-reset").split(",").forEach(function (k) { F[k] = JSON.parse(JSON.stringify(DEFAULTS[k])); });
        changed();
        return;
      }
      var fc = t.closest("[data-chip]");
      if (fc) {
        var fn = chipActions[+fc.getAttribute("data-chip")];
        if (fn) { fn(); changed(); }
        return;
      }
      if (t.closest("[data-clear]")) { clearFilters(); return; }
      if (t.closest("[data-allbtn]")) { F.tab = "all"; changed(); return; }
      if (t.closest("[data-reload]")) { location.reload(); return; }
      var op = t.closest("[data-open].pill");
      if (op) {
        var name = op.getAttribute("data-open");
        if (openName === name) closePanel(); else openPanel(name, op);
        return;
      }
      if (t.closest("[data-close]")) { closePanel(); return; }
    });
    $("scrim").addEventListener("click", closePanel);
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") closePanel();
      if (e.key === "Enter" && openName && e.target.matches && e.target.matches(".pop input[data-f]")) closePanel();
    });
    $("clearAll").addEventListener("click", clearFilters);

    $$("#dealSwitch [data-deal]").forEach(function (b) {
      b.addEventListener("click", function () {
        var d = b.getAttribute("data-deal");
        if (d === "rent") {
          if (isRent(DEAL)) return;
          var subs = rentSubs();
          d = subs.indexOf(lsGet("rentSub", "wynajem")) >= 0 ? lsGet("rentSub", "wynajem") : subs[0];
        }
        switchDeal(d);
      });
    });
    $$(".sub-switch [data-sub]").forEach(function (b) {
      b.addEventListener("click", function () { switchDeal(b.getAttribute("data-sub")); });
    });
    $$(".view-switch [data-view]").forEach(function (b) {
      b.addEventListener("click", function () { F.view = b.getAttribute("data-view"); changed(); });
    });
    $("favToggle").addEventListener("click", function () {
      F.tab = F.tab === "fav" ? "all" : "fav"; changed(); window.scrollTo(0, 0);
    });
    $("favBack").addEventListener("click", function () { F.tab = "all"; changed(); });
    $("brand").addEventListener("click", function (e) { e.preventDefault(); F.tab = "all"; changed(); window.scrollTo(0, 0); });
    $("statusBtn").addEventListener("click", function () {
      var p = $("statusPanel");
      p.hidden = !p.hidden;
      $("statusBtn").setAttribute("aria-expanded", String(!p.hidden));
    });

    window.addEventListener("hashchange", function () {
      var cur = toParams(F).toString();
      if (location.hash.replace(/^#/, "") === cur) return;
      var hc = null;
      try { hc = new URLSearchParams(location.hash.replace(/^#/, "")).get("city"); } catch (e) { hc = null; }
      var cityChanged = hc && hc !== CITY && cityInfo(hc) && setCity(hc);
      if (cityChanged) loadMeta();
      var st = readLocation();
      if (st.deal !== DEAL) { DEAL = st.deal; }
      F = st.F;
      showDeal();
      if (cityChanged) metaReady.then(function () { applyEnabledDeals(); renderStatus(); });
    });

    var bar = $("filterbar");
    var onScroll = function () { bar.classList.toggle("stuck", bar.getBoundingClientRect().top <= 0 && window.scrollY > 10); };
    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", function () { if (openName) closePanel(); });
  }

  function switchDeal(deal) {
    if (!DEALS[deal] || deal === DEAL) return;
    var view = F.view;
    DEAL = deal;
    F = savedFilters(deal);
    F.view = view;
    F.tab = "all";
    closePanel();
    $$(".dist-search").forEach(function (i) { i.value = ""; });
    showDeal();
  }

  function showDeal() {
    persistFilters();
    loadDeal(DEAL); // dane każdego typu wczytujemy dopiero, gdy są potrzebne
    D = STORE[DEAL];
    render();
    if (D.status === "loading") {
      var deal = DEAL;
      D.promise.then(function () { if (DEAL === deal) render(); });
    }
  }

  // ------------------------------------------------------------------ akcje na kartach
  function markSeen(key) {
    if (seen[key]) return;
    seen[key] = 1;
    var keys = Object.keys(seen);
    if (keys.length > 8000) { keys.slice(0, keys.length - 6000).forEach(function (k) { delete seen[k]; }); }
    saveSet("seen", seen);
  }

  function bindCards() {
    grid.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-act]");
      var card = e.target.closest(".card");
      if (btn && card) {
        e.preventDefault();
        var l = D.BY_KEY[card.getAttribute("data-key")];
        if (!l) return;
        var g = groupOf(l);
        if (btn.getAttribute("data-act") === "fav") {
          if (isFav(l)) g.forEach(function (o) { delete fav[o.key]; });
          else fav[l.key] = 1;
          saveSet("fav", fav);
          if (F.tab === "fav" && !isFav(l)) { render(); return; }
          rerenderGroup(l);
          updateFavCount();
        } else {
          if (isHidden(l)) g.forEach(function (o) { delete hidden[o.key]; });
          else g.forEach(function (o) { hidden[o.key] = 1; });
          saveSet("hidden", hidden);
          if (!F.showHidden && F.tab !== "fav" && isHidden(l)) {
            g.forEach(function (o) {
              var el = grid.querySelector('[data-key="' + cssEsc(o.key) + '"]');
              if (el) el.remove();
            });
            filtered = filtered.filter(function (x) { return !isHidden(x); });
            shown = grid.children.length;
            $("moreBtn").hidden = shown >= filtered.length;
            renderCount();
            renderEmpty();
            if (shown < filtered.length && grid.children.length < PAGE / 2) renderMore();
          } else {
            rerenderGroup(l);
          }
          updateFavCount();
        }
        return;
      }
      var link = e.target.closest("[data-open]");
      if (link) {
        markSeen(link.getAttribute("data-open"));
        if (card) {
          var cl = D.BY_KEY[card.getAttribute("data-key")];
          if (cl) setTimeout(function () { rerenderGroup(cl); }, 400);
        }
      }
    });
    grid.addEventListener("auxclick", function (e) {
      var link = e.target.closest("[data-open]");
      if (link && e.button === 1) markSeen(link.getAttribute("data-open"));
    });
    grid.addEventListener("error", function (e) {
      var t = e.target;
      if (t && t.tagName === "IMG") {
        var span = document.createElement("span");
        span.className = "noimg";
        span.innerHTML = icon("photo");
        t.replaceWith(span);
      }
    }, true);

    $("moreBtn").addEventListener("click", renderMore);
    if ("IntersectionObserver" in window) {
      new IntersectionObserver(function (entries) {
        if (entries[0].isIntersecting && F.view === "list" && shown && shown < filtered.length) renderMore();
      }, { rootMargin: "1200px" }).observe($("sentinel"));
    }
  }

  // ------------------------------------------------------------------ mapa
  function loadAsset(tag, url, ok, fail) {
    var el = document.createElement(tag);
    if (tag === "link") { el.rel = "stylesheet"; el.href = url; } else { el.src = url; el.onload = ok; el.onerror = fail; }
    document.head.appendChild(el);
  }

  function loadLeaflet(cb) {
    if (window.L && mapState.ready) return cb();
    mapState.pending = cb;
    if (mapState.loading) return;
    mapState.loading = true;
    var done = function () { mapState.loading = false; mapState.ready = true; if (mapState.pending) mapState.pending(); };
    loadAsset("link", LEAFLET + "leaflet.css");
    loadAsset("script", LEAFLET + "leaflet.js", function () {
      // grupowanie pinezek (ok. 15 tys. ofert); bez niego mapa i tak działa
      loadAsset("link", CLUSTER + "MarkerCluster.css");
      loadAsset("script", CLUSTER + "leaflet.markercluster.js", done, done);
    }, function () {
      mapState.loading = false;
      $("mapNote").textContent = "Nie udało się wczytać mapy (brak internetu?).";
    });
  }

  function geoOf(l) { return l.geo || "dokładna"; }

  function clusterIcon(cluster) {
    var n = cluster.getChildCount();
    var size = n < 10 ? 30 : n < 100 ? 36 : n < 1000 ? 42 : 50;
    return window.L.divIcon({
      html: "<span>" + (n < 10000 ? nf.format(n) : Math.round(n / 1000) + " tys.") + "</span>",
      className: "mc-cluster", iconSize: [size, size]
    });
  }

  function popupHtml(l) {
    var img = l.images && l.images[0];
    var facts = factsList(l);
    return '<div class="popup">' +
      (img ? '<img src="' + esc(safeUrl(img)) + '" alt="" referrerpolicy="no-referrer">' : "") +
      '<div class="pp-body">' + priceHtml(l, "pp-price") +
      (facts.length ? "<div>" + facts.join(" · ") + "</div>" : "") +
      (featLine(l).length ? '<div class="pp-feats">' + esc(featLine(l).join(" · ")) + "</div>" : "") +
      "<div>" + esc([l.district, l.street].filter(Boolean).join(", ")) + "</div>" +
      (geoOf(l) !== "dokładna" ? '<div class="pp-geo">Lokalizacja przybliżona (' + esc(geoOf(l)) + ")</div>" : "") +
      '<div class="pp-links">' + groupOf(l).map(function (o) {
        return '<a href="' + esc(safeUrl(o.url)) + '" target="_blank" rel="noopener noreferrer" data-open="' + esc(o.key) + '">' + esc(srcName(o.source)) + " ↗</a>";
      }).join("") + "</div></div></div>";
  }

  function renderMap() {
    $("mapNote").textContent = "Wczytywanie mapy…";
    loadLeaflet(function () {
      var L = window.L;
      var dark = window.matchMedia("(prefers-color-scheme: dark)").matches;
      if (!mapState.map) {
        mapState.map = L.map("map", { preferCanvas: true, zoomControl: true }).setView(cityCenter(), 12);
        mapState.map.getContainer().addEventListener("click", function (e) {
          var a = e.target.closest && e.target.closest("[data-open]");
          if (a) markSeen(a.getAttribute("data-open"));
        });
      }
      if (!mapState.tiles) {
        // zwykłe kafelki OpenStreetMap; stonowany wygląd nadaje CSS (filtr na warstwie kafelków)
        mapState.tiles = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
          maxZoom: 19,
          attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
        }).addTo(mapState.map);
      }
      if (mapState.layer) mapState.layer.remove();
      var layer = L.markerClusterGroup
        ? L.markerClusterGroup({
            chunkedLoading: true, showCoverageOnHover: false, maxClusterRadius: 45,
            spiderfyOnMaxZoom: true, iconCreateFunction: clusterIcon
          })
        : L.layerGroup();
      var css = getComputedStyle(document.documentElement);
      var accent = css.getPropertyValue("--accent").trim() || "#0f766e";
      var edge = dark ? "#111113" : "#ffffff";
      var cnt = { exact: 0, street: 0, area: 0 }, markers = [];
      var minLat = 90, maxLat = -90, minLon = 180, maxLon = -180;
      filtered.forEach(function (l) {
        if (typeof l.lat !== "number" || typeof l.lon !== "number") return;
        var g = geoOf(l), f = isFav(l), seen = isSeen(l), approx = !!GEO_APPROX[g];
        if (approx) cnt.area++; else if (g === "ulica") cnt.street++; else cnt.exact++;
        var color = f ? "#e11d48" : accent;
        // przybliżone do osiedla/dzielnicy: puste kółko w kolorze akcentu
        var m = L.circleMarker([l.lat, l.lon], approx ? {
          radius: f ? 7 : 5.5, weight: 2, color: color, opacity: seen ? 0.45 : 0.9,
          fillColor: color, fillOpacity: seen ? 0.06 : 0.15
        } : {
          radius: f ? 8 : 6, weight: 1.5, color: edge,
          fillColor: color, fillOpacity: seen ? 0.4 : 0.9
        });
        m.bindPopup(function () { return popupHtml(l); }, { maxWidth: 240, minWidth: 240 });
        markers.push(m);
        if (l.lat < minLat) minLat = l.lat; if (l.lat > maxLat) maxLat = l.lat;
        if (l.lon < minLon) minLon = l.lon; if (l.lon > maxLon) maxLon = l.lon;
      });
      if (layer.addLayers) layer.addLayers(markers); else markers.forEach(function (m) { m.addTo(layer); });
      layer.addTo(mapState.map);
      mapState.layer = layer;
      setTimeout(function () {
        mapState.map.invalidateSize();
        if (markers.length) mapState.map.fitBounds([[minLat, minLon], [maxLat, maxLon]], { padding: [30, 30], maxZoom: 15 });
        else mapState.map.setView(cityCenter(), 12);
      }, 50);
      var withPos = markers.length, missing = filtered.length - withPos;
      var parts = [];
      if (cnt.exact) parts.push('<span class="lg lg-full"></span>' + nf.format(cnt.exact) + " dokładnie");
      if (cnt.street) parts.push('<span class="lg lg-full"></span>' + nf.format(cnt.street) + " wg ulicy");
      if (cnt.area) parts.push('<span class="lg lg-ring"></span>' + nf.format(cnt.area) + " w przybliżeniu (osiedle/dzielnica)");
      $("mapNote").innerHTML = "Na mapie: " + nf.format(withPos) + " " + plural(withPos, "oferta", "oferty", "ofert") +
        (parts.length ? " — " + parts.join(", ") : "") +
        (missing ? ". " + nf.format(missing) + " bez żadnej lokalizacji (portal nie podał ulicy ani osiedla) — są w widoku listy" : "") +
        ". Wg ulicy = punkt na danej ulicy, bez numeru domu.";
    });
  }

  // ------------------------------------------------------------------ status aktualizacji
  function renderStatus() {
    var dm = dealMeta(DEAL) || {};
    var last = parseTs(dm.last_run || (META.deals ? null : META.last_run));
    var srcs = dm.sources || {};
    var names = Object.keys(srcs).sort(function (a, b) { return srcName(a).localeCompare(srcName(b), "pl"); });
    var bad = names.filter(function (k) { return srcs[k] && srcs[k].status !== "ok"; });
    var btn = $("statusBtn");
    btn.classList.toggle("has-warn", bad.length > 0);
    var txt = last ? '<span class="hide-sm">Aktualizacja </span>' + esc(ago(last)) : "Brak aktualizacji";
    if (bad.length) txt = bad.length === 1 ? "1 portal nie działa" : bad.length + " " + plural(bad.length, "portal", "portale", "portali") + ' <span class="hide-sm">nie działa</span>';
    $("updated").innerHTML = txt;
    btn.title = last ? "Ostatnia aktualizacja: " + fmtDate(last) : "";
    var rows = names.map(function (k) {
      var s = srcs[k] || {};
      var ok = s.status === "ok";
      return "<li><span><b>" + esc(srcName(k)) + "</b>" +
        (ok ? "" : ' <span class="small">' + esc(String(s.error || "").slice(0, 140)) + "</span>") + "</span>" +
        "<span>" + (ok
          ? nf.format(s.count || 0) + " " + plural(s.count || 0, "oferta", "oferty", "ofert")
          : '<span class="err">nie działa</span>' + (s.last_ok ? ' <span class="small">· ostatnio ' + esc(ago(parseTs(s.last_ok))) + "</span>" : "")) +
        "</span></li>";
    }).join("");
    var unsup = Object.keys(META.unsupported || {}).map(srcName).sort();
    $("statusPanel").innerHTML = "<h3>" + (last ? "Ostatnia aktualizacja: " + esc(fmtDate(last)) : "Jeszcze nie było aktualizacji") + "</h3>" +
      (rows ? "<ul>" + rows + "</ul>" : "") +
      (unsup.length ? '<div class="small">Nie obsługują tej miejscowości: ' + esc(unsup.join(", ")) + ".</div>" : "") +
      (bad.length ? '<div class="small">Oferty pobrane wcześniej z niedziałających portali nadal są widoczne.</div>'
        : '<div class="small">Oferty odświeżają się automatycznie kilka razy dziennie.</div>');
  }

  function enabledDeals() {
    return Array.isArray(META.enabled_deals) && META.enabled_deals.length ? META.enabled_deals : DEAL_ORDER;
  }
  function rentSubs() {
    var en = enabledDeals();
    return RENT_SUBS.filter(function (d) { return en.indexOf(d) >= 0; });
  }
  function applyEnabledDeals() {
    var en = enabledDeals();
    $$("#dealSwitch [data-deal]").forEach(function (b) {
      var d = b.getAttribute("data-deal");
      b.hidden = d === "rent" ? !rentSubs().length : en.indexOf(d) < 0;
    });
    $$(".sub-switch [data-sub]").forEach(function (b) { b.hidden = en.indexOf(b.getAttribute("data-sub")) < 0; });
    $$(".sub-switch").forEach(function (el) { el.hidden = !isRent(DEAL) || rentSubs().length < 2; });
    var vis = $$("#dealSwitch [data-deal]").filter(function (b) { return !b.hidden; });
    $("dealSwitch").style.gridTemplateColumns = "repeat(" + Math.max(1, vis.length) + ", 1fr)";
    $("dealSwitch").hidden = vis.length < 2;
    if (en.indexOf(DEAL) < 0 && en.length && DEALS[en[0]]) {
      DEAL = en[0];
      F = savedFilters(DEAL);
      showDeal();
    }
  }

  // ------------------------------------------------------------------ API dla docs/alerts.js (powiadomienia)
  // Tylko odczyt stanu; alerts.js ustawia filtry przez zmianę adresu (#…), którą obsługuje hashchange.
  window.MW = {
    getDeal: function () { return DEAL; },
    getFilters: function () { return JSON.parse(JSON.stringify(F)); },
    getCity: function () { return typeof CITY === "string" && CITY ? CITY : "wroclaw"; },
    getMap: function () { return mapState.map; },
    dealLabel: function (d) { return (DEALS[d] || DEALS[DEAL]).label; }
  };

  // ------------------------------------------------------------------ miejscowości
  function loadMeta() {
    metaReady = fetchJson(dataUrl("meta.json"))
      .then(function (r) { META = r.data && typeof r.data === "object" && !Array.isArray(r.data) ? r.data : {}; })
      .catch(function () { META = {}; });
    return metaReady;
  }
  // Ustawia miejscowość (bez wczytywania danych): nagłówek, tytuł, wybór miasta, pamięć.
  function setCity(slug) {
    if (!CITIES || !cityInfo(slug)) return false;
    var changed = CITY !== slug;
    CITY = slug;
    STORE = {};
    META = {};
    renderCityHeader();
    if (changed) lsSet("city", slug);
    return changed;
  }
  function renderCityHeader() {
    var info = cityInfo(CITY);
    var name = info ? info.name : "Wrocław";
    $("brandCity").textContent = name;
    $("brand").setAttribute("aria-label", "Mieszkania " + name + " — strona główna");
    document.title = "Mieszkania " + name;
    var sel = $("citySelect");
    var multi = multiCity();
    sel.hidden = !multi;
    sel.parentNode.classList.toggle("multi", multi);
    if (multi) {
      sel.innerHTML = CITIES.cities.map(function (c) {
        return '<option value="' + esc(c.slug) + '"' + (c.slug === CITY ? " selected" : "") + ">" + esc(c.name) + "</option>";
      }).join("");
    }
  }
  function switchCity(slug) {
    if (!setCity(slug)) return;
    var view = F.view;
    F = savedFilters(DEAL);
    F.view = view;
    F.tab = "all";
    closePanel();
    $$(".dist-search").forEach(function (i) { i.value = ""; });
    loadMeta();
    showDeal();
    metaReady.then(function () { applyEnabledDeals(); renderStatus(); });
  }
  // data/cities.json; brak pliku = stary układ danych (sam Wrocław, data/<typ>.json)
  function loadCities() {
    return fetchJson("data/cities.json")
      .then(function (r) {
        var d = r.data;
        if (d && Array.isArray(d.cities) && d.cities.length) {
          d.cities = d.cities.filter(function (c) { return c && typeof c.slug === "string" && /^[a-z0-9_-]+$/.test(c.slug); })
            .map(function (c) {
              return { slug: c.slug, name: String(c.name || c.slug), lat: +c.lat || null, lon: +c.lon || null };
            });
          if (d.cities.length) CITIES = d;
        }
      })
      .catch(function () { CITIES = null; });
  }

  // ------------------------------------------------------------------ start
  function init() {
    loadCities().then(function () {
      if (CITIES) setCity(readCity());
      var st = readLocation();
      DEAL = st.deal;
      F = st.F;
      bindFilters();
      bindCards();
      $("citySelect").addEventListener("change", function (e) { switchCity(e.target.value); });
      loadMeta();
      showDeal();
      metaReady.then(function () { applyEnabledDeals(); renderStatus(); });
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
