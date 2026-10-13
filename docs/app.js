/* Mieszkania Wrocław — statyczny frontend (bez budowania, bez backendu). */
(function () {
  "use strict";

  // ------------------------------------------------------------------ stałe
  var PAGE = 60;
  var DAY = 86400000;
  var LS_PREFIX = "mwro_";
  var SOURCE_NAMES = {
    otodom: "Otodom", olx: "OLX", gratka: "Gratka", morizon: "Morizon",
    nieruchomosci_online: "Nieruchomosci-online", domiporta: "Domiporta"
  };
  var MARKETS = [["pierwotny", "pierwotny"], ["wtorny", "wtórny"]];
  var SELLERS = [["prywatny", "prywatny"], ["agencja", "agencja"], ["deweloper", "deweloper"]];
  var ROOMS = ["1", "2", "3", "4", "5+"];
  var LEAFLET = "https://unpkg.com/leaflet@1.9.4/dist/";

  var DEFAULTS = {
    q: "", pmin: "", pmax: "", amin: "", amax: "", ppm: "",
    rooms: [], districts: [], market: [], seller: [], sources: [],
    days: 0, hideDups: true, showHidden: false, showInactive: false,
    sort: "newest", tab: "all", view: "list"
  };
  var ARRAY_KEYS = ["rooms", "districts", "market", "seller", "sources"];
  var BOOL_KEYS = ["hideDups", "showHidden", "showInactive"];

  // ------------------------------------------------------------------ narzędzia
  function $(id) { return document.getElementById(id); }

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
  function safeUrl(u) {
    return /^https?:\/\//i.test(u || "") ? u : "#";
  }
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
  var nf = new Intl.NumberFormat("pl-PL", { maximumFractionDigits: 0 });
  var nf1 = new Intl.NumberFormat("pl-PL", { maximumFractionDigits: 1 });
  function zl(n) { return n == null ? "—" : nf.format(n) + " zł"; }
  function parseTs(s) {
    if (!s) return 0;
    var t = Date.parse(s);
    return isNaN(t) ? 0 : t;
  }
  function ago(ts) {
    if (!ts) return "";
    var d = Date.now() - ts;
    if (d < 0) d = 0;
    var m = Math.round(d / 60000);
    if (m < 1) return "przed chwilą";
    if (m < 60) return m + " min temu";
    var h = Math.round(m / 60);
    if (h < 24) return h + " godz. temu";
    var days = Math.round(h / 24);
    if (days === 1) return "wczoraj";
    if (days < 30) return days + " dni temu";
    return new Date(ts).toLocaleDateString("pl-PL");
  }
  function fmtDate(ts) {
    try {
      return new Date(ts).toLocaleString("pl-PL", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });
    } catch (e) { return new Date(ts).toISOString(); }
  }
  function srcName(s) { return SOURCE_NAMES[s] || s; }
  function plural(n, one, few, many) {
    var n10 = n % 10, n100 = n % 100;
    if (n === 1) return one;
    if (n10 >= 2 && n10 <= 4 && (n100 < 12 || n100 > 14)) return few;
    return many;
  }

  // ------------------------------------------------------------------ stan
  var ALL = [];            // wszystkie oferty
  var BY_KEY = {};
  var GROUPS = {};         // dup_group -> [oferty]
  var META = {};
  var filtered = [];       // wynik filtrowania
  var shown = 0;
  var F = loadFilters();
  var fav = toSet(lsGet("fav", []));
  var hidden = toSet(lsGet("hidden", []));
  var seen = toSet(lsGet("seen", []));
  var mapState = { map: null, layer: null, loading: false };

  function toSet(arr) {
    var o = Object.create(null);
    if (Array.isArray(arr)) arr.forEach(function (k) { o[k] = 1; });
    return o;
  }
  function saveSet(name, set) { lsSet(name, Object.keys(set)); }

  function cloneDefaults() { return JSON.parse(JSON.stringify(DEFAULTS)); }

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
    return f;
  }
  function toParams(f) {
    var p = new URLSearchParams();
    Object.keys(DEFAULTS).forEach(function (k) {
      var v = f[k], d = DEFAULTS[k];
      if (ARRAY_KEYS.indexOf(k) >= 0) { if (v.length) p.set(k, v.join("|")); }
      else if (BOOL_KEYS.indexOf(k) >= 0) { if (v !== d) p.set(k, v ? "1" : "0"); }
      else if (String(v) !== String(d) && v !== "") p.set(k, String(v));
    });
    return p;
  }
  function loadFilters() {
    var h = (location.hash || "").replace(/^#/, "");
    if (h) {
      try { return fromParams(new URLSearchParams(h)); } catch (e) { /* zły hash */ }
    }
    var saved = lsGet("filters", null);
    var f = cloneDefaults();
    if (saved && typeof saved === "object") {
      Object.keys(DEFAULTS).forEach(function (k) {
        if (saved[k] !== undefined && typeof saved[k] === typeof DEFAULTS[k]) f[k] = saved[k];
        if (ARRAY_KEYS.indexOf(k) >= 0 && !Array.isArray(f[k])) f[k] = [];
      });
    }
    return f;
  }
  function persistFilters() {
    lsSet("filters", F);
    var s = toParams(F).toString();
    var url = location.pathname + location.search + (s ? "#" + s : "");
    try { history.replaceState(null, "", url); } catch (e) { /* np. file:// */ }
  }

  // ------------------------------------------------------------------ dane
  function prepare(list, meta) {
    var firstRun = parseTs(meta && meta.first_run);
    if (!firstRun) {
      list.forEach(function (l) {
        var t = parseTs(l.first_seen);
        if (t && (!firstRun || t < firstRun)) firstRun = t;
      });
    }
    var now = Date.now();
    list.forEach(function (l) {
      l._first = parseTs(l.first_seen);
      var created = parseTs(l.created_at);
      if (created > now + DAY) created = 0;
      // „Świeżość”: data publikacji z portalu albo moment, gdy pierwszy raz ją zobaczyliśmy.
      // Oferty z pierwszego w historii pobrania bez daty publikacji nie są traktowane jako „nowe”.
      var fresh = created || (l._first && l._first - firstRun > 3600000 ? l._first : 0);
      l._fresh = fresh;
      l._date = created || l._first;
      l._ppm = l.price_per_m2 || (l.price && l.area ? Math.round(l.price / l.area) : null);
      l._search = norm([l.title, l.street, l.district, l.description].join(" "));
      l._dist = l.district ? String(l.district).trim() : "";
      // obniżka ceny: poprzednia cena wyższa od obecnej
      l._oldPrice = null;
      var h = l.price_history || [];
      if (h.length >= 2 && l.price) {
        var prev = h[h.length - 2].price;
        if (prev && prev > l.price) l._oldPrice = prev;
      }
      BY_KEY[l.key] = l;
      if (l.dup_group) (GROUPS[l.dup_group] = GROUPS[l.dup_group] || []).push(l);
    });
  }

  function groupOf(l) {
    return l.dup_group && GROUPS[l.dup_group] ? GROUPS[l.dup_group] : [l];
  }
  function anyIn(set, l) {
    var g = groupOf(l);
    for (var i = 0; i < g.length; i++) if (set[g[i].key]) return true;
    return false;
  }
  function isFav(l) { return anyIn(fav, l); }
  function isHidden(l) { return anyIn(hidden, l); }
  function isSeen(l) { return anyIn(seen, l); }

  // ------------------------------------------------------------------ filtrowanie
  function applyFilters() {
    var pmin = num(F.pmin), pmax = num(F.pmax), amin = num(F.amin), amax = num(F.amax), ppm = num(F.ppm);
    var q = norm(F.q).trim();
    var words = q ? q.split(/\s+/) : [];
    var rooms = F.rooms, dists = toSet(F.districts), market = F.market, seller = F.seller, srcs = F.sources;
    var minFresh = F.days ? Date.now() - F.days * DAY : 0;
    var favTab = F.tab === "fav";

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
      if (minFresh && !(l._fresh >= minFresh)) continue;
      if (words.length) {
        var ok = true;
        for (var w = 0; w < words.length; w++) if (l._search.indexOf(words[w]) < 0) { ok = false; break; }
        if (!ok) continue;
      }
      out.push(l);
    }

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
    // wartości puste zawsze na końcu
    if (a == null && b == null) return 0;
    if (a == null) return 1;
    if (b == null) return -1;
    return asc ? a - b : b - a;
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
      return c || (b._date - a._date) || (a.key < b.key ? -1 : 1);
    });
  }

  // ------------------------------------------------------------------ karty
  function cardHtml(l) {
    var now = Date.now();
    var isNew = l._fresh && now - l._fresh < DAY;
    var favOn = isFav(l), hid = isHidden(l), seenOn = isSeen(l);
    var img = l.images && l.images[0];
    var url = safeUrl(l.url);
    var cls = "card" + (seenOn ? " seen" : "") + (hid ? " is-hidden" : "") + (l.active ? "" : " inactive");

    var badges = "";
    if (!l.active) badges += '<span class="badge gone">NIEAKTUALNA</span>';
    if (isNew) badges += '<span class="badge new">NOWE</span>';
    if (l._oldPrice) badges += '<span class="badge drop">↓ OBNIŻKA</span>';
    if (seenOn) badges += '<span class="badge seenb">oglądane</span>';

    var facts = [];
    if (l.area) facts.push("<b>" + nf1.format(l.area) + " m²</b>");
    if (l.rooms) facts.push(l.rooms + " " + plural(l.rooms, "pokój", "pokoje", "pokoi"));
    if (l._ppm) facts.push('<span class="ppm">' + nf.format(l._ppm) + " zł/m²</span>");

    var loc = [l.district, l.street].filter(Boolean).map(esc).join(", ");
    var tags = [];
    if (l.floor != null && l.floor !== "") {
      var fl = String(l.floor);
      tags.push(/^(0|parter)$/i.test(fl) ? "parter" : "piętro " + esc(fl));
    }
    if (l.market) tags.push(l.market === "wtorny" ? "rynek wtórny" : "rynek " + esc(l.market));
    if (l.seller) tags.push(esc(l.seller));
    if (l.build_year) tags.push("rok " + esc(l.build_year));

    var group = groupOf(l);
    var srcHtml = '<a class="src primary" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer" data-open="' + esc(l.key) + '">' + esc(srcName(l.source)) + "</a>";
    var others = group.filter(function (o) { return o.key !== l.key && (o.active || !l.active); });
    if (others.length) {
      srcHtml += '<span class="also">też na:</span>' + others.map(function (o) {
        var t = o.price && o.price !== l.price ? " (" + zl(o.price) + ")" : "";
        return '<a class="src" href="' + esc(safeUrl(o.url)) + '" target="_blank" rel="noopener noreferrer" data-open="' + esc(o.key) +
          '" title="' + esc(srcName(o.source) + t) + '">' + esc(srcName(o.source)) + "</a>";
      }).join("");
    }

    var priceHtml = '<span class="price">' + zl(l.price) + "</span>";
    if (l._oldPrice) {
      var pct = Math.round((1 - l.price / l._oldPrice) * 100);
      priceHtml += '<span class="old-price">' + zl(l._oldPrice) + "</span>" + (pct > 0 ? '<span class="drop-pct">−' + pct + "%</span>" : "");
    }

    return '<article class="' + cls + '" data-key="' + esc(l.key) + '">' +
      '<a class="thumb" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer" data-open="' + esc(l.key) + '" tabindex="-1">' +
        (img
          ? '<img src="' + esc(safeUrl(img)) + '" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer">'
          : '<span class="noimg" aria-hidden="true">🏢</span>') +
        '<span class="badges">' + badges + "</span>" +
      "</a>" +
      '<div class="actions">' +
        '<button type="button" class="act fav' + (favOn ? " on" : "") + '" data-act="fav" title="' + (favOn ? "Usuń z ulubionych" : "Dodaj do ulubionych") + '" aria-pressed="' + favOn + '">' + (favOn ? "★" : "☆") + "</button>" +
        '<button type="button" class="act" data-act="hide" title="' + (hid ? "Przywróć" : "Ukryj tę ofertę") + '">' + (hid ? "↺" : "✕") + "</button>" +
      "</div>" +
      '<div class="card-body">' +
        '<div class="price-row">' + priceHtml + "</div>" +
        '<div class="facts">' + facts.join(" · ") + "</div>" +
        '<a class="title" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer" data-open="' + esc(l.key) + '">' + esc(l.title || "Mieszkanie") + "</a>" +
        (loc ? '<div class="loc">📍 ' + loc + "</div>" : "") +
        (tags.length ? '<div class="tags">' + tags.map(function (t) { return '<span class="tag">' + t + "</span>"; }).join("") + "</div>" : "") +
        '<div class="card-foot"><div class="srcs">' + srcHtml + "</div>" +
          "<span>" + esc(l._date ? ago(l._date) : "") + "</span></div>" +
      "</div>" +
    "</article>";
  }

  // ------------------------------------------------------------------ renderowanie
  var grid = $("grid");

  function render() {
    filtered = applyFilters();
    shown = 0;
    grid.innerHTML = "";
    renderCount();
    renderEmpty();
    if (F.view === "map") {
      renderMap();
    } else {
      renderMore();
    }
    updateFavCount();
    updateFilterBadge();
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
    var l = BY_KEY[key];
    if (!el || !l) return;
    el.outerHTML = cardHtml(l);
  }
  function rerenderGroup(l) {
    groupOf(l).forEach(function (o) { rerenderCard(o.key); });
  }
  function cssEsc(s) {
    return window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/["\\]/g, "\\$&");
  }

  function renderCount() {
    var n = filtered.length;
    var txt = "<b>" + nf.format(n) + "</b> " + plural(n, "oferta", "oferty", "ofert");
    if (F.tab === "fav") txt += " w ulubionych";
    else if (F.hideDups) txt += " (bez duplikatów)";
    $("resultCount").innerHTML = txt;
  }

  function renderEmpty() {
    var box = $("empty");
    if (filtered.length) { box.hidden = true; return; }
    box.hidden = false;
    if (!ALL.length) {
      box.innerHTML = "<h3>Jeszcze nie ma żadnych ofert</h3>" +
        "<p>Oferty pobierają się automatycznie co 2 godziny — pierwsze pojawią się tu wkrótce.<br>" +
        "Można też uruchomić pobieranie ręcznie: na GitHubie zakładka <b>Actions</b> → <b>Run workflow</b>.</p>";
    } else if (F.tab === "fav") {
      box.innerHTML = "<h3>Brak ulubionych</h3><p>Kliknij ☆ na karcie oferty, żeby dodać ją do ulubionych.</p>" +
        (activeFilterCount() ? '<button type="button" class="btn" data-clear="1">Wyczyść filtry</button>' : "");
    } else {
      box.innerHTML = "<h3>Brak ofert spełniających filtry</h3><p>Spróbuj poszerzyć kryteria wyszukiwania.</p>" +
        '<button type="button" class="btn" data-clear="1">Wyczyść filtry</button>';
    }
  }

  function updateFavCount() {
    var n = 0, groupsSeen = Object.create(null);
    Object.keys(fav).forEach(function (k) {
      var l = BY_KEY[k];
      if (!l) return;
      if (l.dup_group) { if (groupsSeen[l.dup_group]) return; groupsSeen[l.dup_group] = 1; }
      n++;
    });
    $("favCount").textContent = n;
    var h = 0;
    Object.keys(hidden).forEach(function (k) { if (BY_KEY[k]) h++; });
    $("hiddenCount").textContent = h ? "(" + h + ")" : "";
  }

  function activeFilterCount() {
    var n = 0;
    ["q", "pmin", "pmax", "amin", "amax", "ppm"].forEach(function (k) { if (String(F[k]).trim()) n++; });
    ARRAY_KEYS.forEach(function (k) { if (F[k].length) n++; });
    if (F.days) n++;
    return n;
  }
  function updateFilterBadge() {
    var n = activeFilterCount();
    var b = $("activeFilterCount");
    b.hidden = !n;
    b.textContent = n;
    $("applyFilters").textContent = "Pokaż wyniki (" + nf.format(filtered.length) + ")";
  }

  // ------------------------------------------------------------------ panel filtrów
  function chipGroup(container, items, key) {
    container.innerHTML = items.map(function (it) {
      var on = F[key].indexOf(it[0]) >= 0;
      return '<button type="button" class="chip" data-key="' + key + '" data-val="' + esc(it[0]) + '" aria-pressed="' + on + '">' + esc(it[1]) + "</button>";
    }).join("");
  }

  var districtCounts = [];
  function buildDistrictList() {
    var counts = Object.create(null);
    ALL.forEach(function (l) { if (l.active && l._dist) counts[l._dist] = (counts[l._dist] || 0) + 1; });
    // zaznaczone wcześniej dzielnice zostają na liście nawet jeśli dziś brak ofert
    F.districts.forEach(function (d) { if (!(d in counts)) counts[d] = 0; });
    districtCounts = Object.keys(counts).map(function (d) { return [d, counts[d]]; })
      .sort(function (a, b) { return a[0].localeCompare(b[0], "pl"); });
    renderDistrictList();
  }
  function renderDistrictList() {
    var q = norm($("distSearch").value).trim();
    var sel = toSet(F.districts);
    var rows = districtCounts.filter(function (d) { return !q || norm(d[0]).indexOf(q) >= 0; });
    // zaznaczone na górze
    rows.sort(function (a, b) { return (sel[b[0]] ? 1 : 0) - (sel[a[0]] ? 1 : 0); });
    $("districtList").innerHTML = rows.length ? rows.map(function (d) {
      return '<label><input type="checkbox" data-dist="' + esc(d[0]) + '"' + (sel[d[0]] ? " checked" : "") + "> " +
        esc(d[0]) + '<span class="cnt">' + d[1] + "</span></label>";
    }).join("") : '<div class="none">' + (districtCounts.length ? "Brak pasujących dzielnic" : "Brak danych") + "</div>";
    $("distCount").textContent = F.districts.length ? "(" + F.districts.length + ")" : "";
  }

  function syncInputs() {
    ["q", "pmin", "pmax", "amin", "amax", "ppm"].forEach(function (k) { $(k).value = F[k]; });
    $("newDays").value = String(F.days);
    $("sort").value = F.sort;
    $("hideDups").checked = F.hideDups;
    $("showHidden").checked = F.showHidden;
    $("showInactive").checked = F.showInactive;
    chipGroup($("roomsChips"), ROOMS.map(function (r) { return [r, r]; }), "rooms");
    chipGroup($("marketChips"), MARKETS, "market");
    chipGroup($("sellerChips"), SELLERS, "seller");
    var srcSet = Object.create(null);
    ALL.forEach(function (l) { srcSet[l.source] = 1; });
    F.sources.forEach(function (s) { srcSet[s] = 1; });
    chipGroup($("sourceChips"), Object.keys(srcSet).sort().map(function (s) { return [s, srcName(s)]; }), "sources");
    renderDistrictList();
    Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (t) {
      t.setAttribute("aria-selected", String(t.getAttribute("data-tab") === F.tab));
    });
    Array.prototype.forEach.call(document.querySelectorAll(".seg-btn"), function (b) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-view") === F.view));
    });
    $("listView").hidden = F.view !== "list";
    $("mapView").hidden = F.view !== "map";
  }

  var renderTimer = null;
  function changed(debounce) {
    persistFilters();
    clearTimeout(renderTimer);
    if (debounce) renderTimer = setTimeout(render, 220);
    else render();
  }

  function bindFilters() {
    ["q", "pmin", "pmax", "amin", "amax", "ppm"].forEach(function (k) {
      $(k).addEventListener("input", function (e) { F[k] = e.target.value; changed(true); });
    });
    $("newDays").addEventListener("change", function (e) { F.days = parseInt(e.target.value, 10) || 0; changed(); });
    $("sort").addEventListener("change", function (e) { F.sort = e.target.value; changed(); });
    ["hideDups", "showHidden", "showInactive"].forEach(function (k) {
      $(k).addEventListener("change", function (e) { F[k] = e.target.checked; changed(); });
    });
    $("filters").addEventListener("click", function (e) {
      var chip = e.target.closest(".chip");
      if (!chip) return;
      var key = chip.getAttribute("data-key"), val = chip.getAttribute("data-val");
      var arr = F[key], i = arr.indexOf(val);
      if (i >= 0) arr.splice(i, 1); else arr.push(val);
      chip.setAttribute("aria-pressed", String(i < 0));
      changed();
    });
    $("districtList").addEventListener("change", function (e) {
      var d = e.target.getAttribute("data-dist");
      if (d == null) return;
      var i = F.districts.indexOf(d);
      if (e.target.checked && i < 0) F.districts.push(d);
      if (!e.target.checked && i >= 0) F.districts.splice(i, 1);
      $("distCount").textContent = F.districts.length ? "(" + F.districts.length + ")" : "";
      changed();
    });
    $("distSearch").addEventListener("input", renderDistrictList);
    $("clearFilters").addEventListener("click", clearFilters);
    $("empty").addEventListener("click", function (e) { if (e.target.closest("[data-clear]")) clearFilters(); });

    Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (t) {
      t.addEventListener("click", function () { F.tab = t.getAttribute("data-tab"); syncInputs(); changed(); window.scrollTo(0, 0); });
    });
    Array.prototype.forEach.call(document.querySelectorAll(".seg-btn"), function (b) {
      b.addEventListener("click", function () { F.view = b.getAttribute("data-view"); syncInputs(); changed(); });
    });

    // panel filtrów na telefonie
    function openF() { $("filters").classList.add("open"); $("backdrop").hidden = false; }
    function closeF() { $("filters").classList.remove("open"); $("backdrop").hidden = true; }
    $("openFilters").addEventListener("click", openF);
    $("closeFilters").addEventListener("click", closeF);
    $("applyFilters").addEventListener("click", function () { closeF(); window.scrollTo(0, 0); });
    $("backdrop").addEventListener("click", closeF);
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeF(); });

    window.addEventListener("hashchange", function () {
      var h = toParams(F).toString();
      if (location.hash.replace(/^#/, "") === h) return;
      F = loadFilters();
      syncInputs();
      changed();
    });
  }

  function clearFilters() {
    var keep = { tab: F.tab, view: F.view, sort: F.sort };
    F = cloneDefaults();
    F.tab = keep.tab; F.view = keep.view; F.sort = keep.sort;
    $("distSearch").value = "";
    buildDistrictList();
    syncInputs();
    changed();
  }

  // ------------------------------------------------------------------ akcje na kartach
  function markSeen(key) {
    if (seen[key]) return;
    seen[key] = 1;
    // limit, żeby localStorage nie puchł w nieskończoność
    var keys = Object.keys(seen);
    if (keys.length > 6000) { keys.slice(0, keys.length - 5000).forEach(function (k) { delete seen[k]; }); }
    saveSet("seen", seen);
  }

  function bindCards() {
    grid.addEventListener("click", function (e) {
      var btn = e.target.closest("[data-act]");
      var card = e.target.closest(".card");
      if (btn && card) {
        e.preventDefault();
        var l = BY_KEY[card.getAttribute("data-key")];
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
          var cl = BY_KEY[card.getAttribute("data-key")];
          // przyciemnienie po krótkiej chwili, żeby nie migało przed otwarciem karty
          if (cl) setTimeout(function () { rerenderGroup(cl); }, 300);
        }
      }
    });
    // środkowy przycisk myszy też otwiera ofertę
    grid.addEventListener("auxclick", function (e) {
      var link = e.target.closest("[data-open]");
      if (link && e.button === 1) markSeen(link.getAttribute("data-open"));
    });
    grid.addEventListener("error", function (e) {
      var t = e.target;
      if (t && t.tagName === "IMG") {
        var span = document.createElement("span");
        span.className = "noimg";
        span.textContent = "🏢";
        t.replaceWith(span);
      }
    }, true);

    $("moreBtn").addEventListener("click", renderMore);
    if ("IntersectionObserver" in window) {
      new IntersectionObserver(function (entries) {
        if (entries[0].isIntersecting && F.view === "list" && shown && shown < filtered.length) renderMore();
      }, { rootMargin: "800px" }).observe($("sentinel"));
    }
  }

  // ------------------------------------------------------------------ mapa
  function loadLeaflet(cb) {
    if (window.L) return cb();
    if (mapState.loading) return;
    mapState.loading = true;
    var css = document.createElement("link");
    css.rel = "stylesheet";
    css.href = LEAFLET + "leaflet.css";
    document.head.appendChild(css);
    var s = document.createElement("script");
    s.src = LEAFLET + "leaflet.js";
    s.onload = function () { mapState.loading = false; cb(); };
    s.onerror = function () {
      mapState.loading = false;
      $("mapNote").textContent = "Nie udało się wczytać mapy (brak internetu?).";
    };
    document.head.appendChild(s);
  }

  function popupHtml(l) {
    var img = l.images && l.images[0];
    var group = groupOf(l);
    return '<div class="popup">' +
      (img ? '<img src="' + esc(safeUrl(img)) + '" alt="" referrerpolicy="no-referrer">' : "") +
      '<div class="pp">' + zl(l.price) + "</div>" +
      "<div>" + [l.area ? nf1.format(l.area) + " m²" : "", l.rooms ? l.rooms + " pok." : "", l._ppm ? nf.format(l._ppm) + " zł/m²" : ""].filter(Boolean).join(" · ") + "</div>" +
      "<div>" + esc([l.district, l.street].filter(Boolean).join(", ")) + "</div>" +
      '<div style="margin-top:4px">' + group.map(function (o) {
        return '<a href="' + esc(safeUrl(o.url)) + '" target="_blank" rel="noopener noreferrer" data-open="' + esc(o.key) + '">' + esc(srcName(o.source)) + " →</a>";
      }).join(" &nbsp;") + "</div></div>";
  }

  function renderMap() {
    loadLeaflet(function () {
      var L = window.L;
      if (!mapState.map) {
        mapState.map = L.map("map", { preferCanvas: true }).setView([51.11, 17.03], 12);
        L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
          maxZoom: 19,
          attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
        }).addTo(mapState.map);
        mapState.map.getContainer().addEventListener("click", function (e) {
          var a = e.target.closest && e.target.closest("[data-open]");
          if (a) markSeen(a.getAttribute("data-open"));
        });
      }
      if (mapState.layer) mapState.layer.remove();
      var layer = L.layerGroup();
      var accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#2f6fde";
      var favColor = getComputedStyle(document.documentElement).getPropertyValue("--fav").trim() || "#e8a400";
      var withPos = 0, bounds = [];
      filtered.forEach(function (l) {
        if (typeof l.lat !== "number" || typeof l.lon !== "number") return;
        withPos++;
        var f = isFav(l);
        L.circleMarker([l.lat, l.lon], {
          radius: f ? 8 : 6, weight: 1.5, color: "#fff",
          fillColor: f ? favColor : accent, fillOpacity: isSeen(l) ? 0.45 : 0.9
        }).bindPopup(function () { return popupHtml(l); }, { maxWidth: 240 }).addTo(layer);
        bounds.push([l.lat, l.lon]);
      });
      layer.addTo(mapState.map);
      mapState.layer = layer;
      setTimeout(function () {
        mapState.map.invalidateSize();
        if (bounds.length) mapState.map.fitBounds(bounds, { padding: [30, 30], maxZoom: 15 });
      }, 50);
      var missing = filtered.length - withPos;
      $("mapNote").textContent = "Na mapie: " + nf.format(withPos) + " " + plural(withPos, "oferta", "oferty", "ofert") +
        (missing ? " · " + nf.format(missing) + " bez dokładnej lokalizacji (widoczne w widoku listy)" : "") +
        ". Lokalizacje bywają przybliżone.";
    });
  }

  // ------------------------------------------------------------------ nagłówek / meta
  function renderMeta() {
    var last = parseTs(META.last_run);
    $("updated").textContent = last
      ? "ostatnia aktualizacja: " + fmtDate(last) + " (" + ago(last) + ")"
      : "jeszcze nie było aktualizacji";
    var srcs = META.sources || {};
    var bad = Object.keys(srcs).filter(function (k) { return srcs[k] && srcs[k].status !== "ok"; });
    var btn = $("warnBtn"), panel = $("warnPanel");
    if (!bad.length) { btn.hidden = true; panel.hidden = true; return; }
    btn.hidden = false;
    btn.textContent = "⚠ " + (bad.length === 1 ? "1 portal nie działa" : bad.length + " " + plural(bad.length, "portal", "portale", "portali") + " nie działa" + (plural(bad.length, "", "ją", "") ));
    btn.title = "Pokaż szczegóły";
    panel.innerHTML = "Przy ostatniej aktualizacji nie udało się pobrać ofert z: " +
      "<ul>" + bad.map(function (k) {
        var s = srcs[k];
        return "<li><b>" + esc(srcName(k)) + "</b>" +
          (s.last_ok ? " — ostatnio działał " + esc(ago(parseTs(s.last_ok))) : "") +
          (s.error ? ' <span class="muted">(' + esc(String(s.error).slice(0, 160)) + ")</span>" : "") + "</li>";
      }).join("") + "</ul>Wcześniej pobrane oferty z tych portali są nadal widoczne.";
    btn.onclick = function () { panel.hidden = !panel.hidden; };
  }

  // ------------------------------------------------------------------ start
  function fetchJson(url, fallback) {
    return fetch(url + "?t=" + Math.floor(Date.now() / 60000), { cache: "no-cache" })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .catch(function (e) {
        if (window.console) console.warn("Nie udało się wczytać", url, e);
        return fallback;
      });
  }

  function init() {
    bindFilters();
    bindCards();
    syncInputs();
    Promise.all([fetchJson("data/listings.json", null), fetchJson("data/meta.json", {})]).then(function (res) {
      var list = res[0], meta = res[1];
      META = meta && typeof meta === "object" && !Array.isArray(meta) ? meta : {};
      if (list && !Array.isArray(list) && Array.isArray(list.listings)) list = list.listings;
      if (!Array.isArray(list)) {
        if (list === null) {
          $("empty").hidden = false;
          $("empty").innerHTML = "<h3>Nie udało się wczytać danych</h3><p>Odśwież stronę za chwilę. " +
            (location.protocol === "file:" ? "Stronę trzeba otworzyć przez serwer (np. GitHub Pages), a nie bezpośrednio z pliku." : "") + "</p>";
          $("resultCount").textContent = "";
          renderMeta();
          return;
        }
        list = [];
      }
      ALL = list.filter(function (l) { return l && l.key; });
      prepare(ALL, META);
      renderMeta();
      buildDistrictList();
      syncInputs();
      render();
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
