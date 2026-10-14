/* Powiadomienia e-mail — tworzenie „kodu alertu” (sekret ALERT_… na GitHubie).
   Strona jest statyczna i publiczna, więc nic tu nie jest wysyłane ani zapisywane na serwerze:
   alert to zakodowany JSON, który właścicielka wkleja jako sekret repozytorium.
   Format (v1) opisuje scraper/alerts.py; filtry działają tak samo jak na stronie.
   Z app.js korzystamy tylko przez window.MW (getDeal, getFilters, getCity, getMap, dealLabel). */
(function () {
  "use strict";

  var LEAFLET = "https://unpkg.com/leaflet@1.9.4/dist/";
  var LS = "mwro_";
  var DEFAULT_REPO = "mjuchiewicz/mieszkania";
  var CITY_CENTER = { wroclaw: [51.11, 17.03] };
  var R_MIN = 500, R_MAX = 5000, R_STEP = 100, R_DEF = 1500;
  var EMAIL_RE = /^[^@\s,;<>"']+@[^@\s,;<>"']+\.[^@\s,;<>"']+$/;
  var DEAL_NAMES = { sprzedaz: "Kupno", wynajem: "Wynajem — całe mieszkanie", pokoj: "Wynajem — pokój" };
  var MONTHLY = { wynajem: 1, pokoj: 1 };
  var FEAT_LABEL = { balkon: "Balkon / taras / loggia", ogrodek: "Ogródek", winda: "Winda", parking: "Parking / garaż",
    piwnica: "Piwnica / komórka", klima: "Klimatyzacja", zwierzeta: "Zwierzęta OK" };
  var COND_LABEL = { zam: "Do zamieszkania", dew: "Stan deweloperski", rem: "Do remontu" };
  var BT_LABEL = { blok: "Blok", kamienica: "Kamienica", apartamentowiec: "Apartamentowiec", dom: "Dom", loft: "Loft" };
  var SELLER_LABEL = { prywatny: "Osoba prywatna", agencja: "Biuro", deweloper: "Deweloper" };
  var SOURCE_NAMES = { otodom: "Otodom", olx: "OLX", gratka: "Gratka", morizon: "Morizon",
    nieruchomosci_online: "Nieruchomosci-online", domiporta: "Domiporta" };

  // ------------------------------------------------------------------ narzędzia
  function $(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function icon(n) { return '<svg aria-hidden="true"><use href="#i-' + n + '"/></svg>'; }
  function lsGet(k, d) { try { var v = localStorage.getItem(LS + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } }
  function lsSet(k, v) { try { localStorage.setItem(LS + k, JSON.stringify(v)); } catch (e) { /* tryb prywatny */ } }
  var nf = new Intl.NumberFormat("pl-PL", { maximumFractionDigits: 0 });
  var nf1 = new Intl.NumberFormat("pl-PL", { maximumFractionDigits: 1 });
  // jak num() w app.js
  function num(s) {
    if (s == null || s === "") return null;
    if (typeof s === "number") return isFinite(s) ? s : null;
    var t = String(s).replace(/\s/g, "").replace(",", ".").replace(/[^0-9.]/g, "");
    if (!t) return null;
    var n = parseFloat(t);
    return isFinite(n) ? n : null;
  }
  // jak floorNum() w app.js
  function floorNum(s) {
    if (s == null || String(s).trim() === "") return null;
    if (typeof s === "number") return Math.round(s);
    if (/^\s*p/i.test(String(s))) return 0;
    var n = num(s);
    return n == null ? null : Math.round(n);
  }
  function shortZl(n) {
    if (n >= 1000000) return nf1.format(n / 1000000) + " mln";
    if (n >= 10000) return nf1.format(n / 1000) + " tys.";
    return nf.format(n) + " zł";
  }
  function radiusText(m) { return m >= 1000 ? nf1.format(m / 1000) + " km" : nf.format(m) + " m"; }
  function plural(n, one, few, many) {
    var n10 = n % 10, n100 = n % 100;
    if (n === 1) return one;
    if (n10 >= 2 && n10 <= 4 && (n100 < 12 || n100 > 14)) return few;
    return many;
  }
  function repo() {
    var h = location.hostname, m = /^([^.]+)\.github\.io$/i.exec(h);
    var seg = location.pathname.split("/").filter(Boolean)[0];
    return m && seg ? m[1] + "/" + seg : DEFAULT_REPO;
  }

  // ------------------------------------------------------------------ kod alertu (base64url z JSON-a, UTF-8)
  function encode(obj) {
    var bytes = new TextEncoder().encode(JSON.stringify(obj)), bin = "";
    for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }
  function decode(code) {
    var s = String(code || "").replace(/\s+/g, "");
    if (!s) throw new Error("Wklej kod alertu.");
    var obj;
    if (s.charAt(0) === "{") obj = JSON.parse(s);
    else {
      // ktoś mógł wkleić całą linię „ALERT_NAZWA=kod”
      s = s.replace(/^ALERT_[A-Za-z0-9_]*=/, "").replace(/-/g, "+").replace(/_/g, "/");
      while (s.length % 4) s += "=";
      try {
        var bin = atob(s), bytes = new Uint8Array(bin.length);
        for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        obj = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
      } catch (e) { throw new Error("Ten kod jest niepełny albo uszkodzony — skopiuj go jeszcze raz w całości."); }
    }
    if (!obj || typeof obj !== "object") throw new Error("To nie jest kod alertu.");
    if (obj.v && obj.v > 1) throw new Error("Ten alert pochodzi z nowszej wersji strony — odśwież stronę.");
    if (!DEAL_NAMES[obj.deal || "sprzedaz"]) throw new Error("Nieznany rodzaj ofert w alercie.");
    obj.deal = obj.deal || "sprzedaz";
    obj.filters = obj.filters && typeof obj.filters === "object" ? obj.filters : {};
    obj.emails = Array.isArray(obj.emails) ? obj.emails.filter(function (e) { return EMAIL_RE.test(e); }) : [];
    return obj;
  }

  // filtry strony (F z app.js) -> filtry alertu (nazwy pól jak w scraper/alerts.py)
  function filtersFromSite(F, deal) {
    var f = {};
    function setNum(k, v) { var n = num(v); if (n != null) f[k] = n; }
    if (F.q && F.q.trim()) f.text = F.q.trim();
    setNum("price_min", F.pmin); setNum("price_max", F.pmax);
    setNum("area_min", F.amin); setNum("area_max", F.amax);
    if (deal === "sprzedaz") setNum("ppm_max", F.ppm);
    var a = floorNum(F.fmin), b = floorNum(F.fmax);
    if (a != null) f.floor_min = a;
    if (b != null) f.floor_max = b;
    if (deal !== "pokoj" && F.rooms.length) f.rooms = F.rooms.slice();
    if (F.districts.length) f.districts = F.districts.slice();
    if (deal === "sprzedaz" && F.market.length) f.market = F.market.slice();
    if (F.seller.length) f.seller = F.seller.slice();
    if (F.sources.length) f.sources = F.sources.slice();
    if (F.cond.length) f.conditions = F.cond.slice();
    if (F.btype.length) f.building_types = F.btype.slice();
    var feats = F.feat.filter(function (x) { return MONTHLY[deal] || x !== "zwierzeta"; });
    if (feats.length) f.features = feats;
    if (F.furn === "tak") f.furnished = true;
    if (F.furn === "nie") f.furnished = false;
    if (F.noGround) f.no_ground = true;
    if (F.noTop) f.no_top = true;
    return f;
  }

  // filtry alertu -> adres strony (#deal=…&pmax=… — format toParams() z app.js)
  function hashFromAlert(al) {
    var f = al.filters || {}, p = new URLSearchParams();
    p.set("deal", al.deal);
    if (al.city && al.city !== "wroclaw") p.set("city", al.city);
    [["q", "text"], ["pmin", "price_min"], ["pmax", "price_max"], ["amin", "area_min"], ["amax", "area_max"],
      ["ppm", "ppm_max"], ["fmin", "floor_min"], ["fmax", "floor_max"]].forEach(function (x) {
      if (f[x[1]] != null && f[x[1]] !== "") p.set(x[0], String(f[x[1]]));
    });
    [["rooms", "rooms"], ["districts", "districts"], ["market", "market"], ["seller", "seller"], ["sources", "sources"],
      ["cond", "conditions"], ["btype", "building_types"], ["feat", "features"]].forEach(function (x) {
      if (Array.isArray(f[x[1]]) && f[x[1]].length) p.set(x[0], f[x[1]].map(String).join("|"));
    });
    if (f.furnished === true) p.set("furn", "tak");
    if (f.furnished === false) p.set("furn", "nie");
    if (f.no_ground) p.set("noGround", "1");
    if (f.no_top) p.set("noTop", "1");
    return p.toString();
  }

  // krótki opis filtrów (jak summary_parts() w scraper/alerts.py)
  function summary(al) {
    var f = al.filters || {}, deal = al.deal, out = [];
    function range(a, b, fmt) {
      a = num(a); b = num(b);
      if (a != null && b != null) return fmt(a) + " – " + fmt(b);
      if (a != null) return "od " + fmt(a);
      if (b != null) return "do " + fmt(b);
      return "";
    }
    var d = f.districts || [];
    if (d.length) out.push(d.slice(0, 3).join(", ") + (d.length > 3 ? " +" + (d.length - 3) : ""));
    if (al.area) out.push("do " + radiusText(al.area.radius_m) + " od punktu");
    var pr = range(f.price_min, f.price_max, MONTHLY[deal] ? function (n) { return nf.format(n) + " zł"; } : shortZl);
    if (pr) out.push(pr);
    if (deal !== "pokoj" && f.rooms && f.rooms.length) out.push(f.rooms.slice().sort().join(", ") + " pok.");
    var ar = range(f.area_min, f.area_max, function (n) { return nf1.format(n) + " m²"; });
    if (ar) out.push(ar);
    if (deal === "sprzedaz" && f.ppm_max != null) out.push("do " + nf.format(f.ppm_max) + " zł/m²");
    var a = f.floor_min, b = f.floor_max;
    if (a != null && b != null) out.push(a === b ? (a === 0 ? "Parter" : "Piętro " + a) : "Piętro " + a + "–" + b);
    else if (a != null) out.push("Piętro od " + a);
    else if (b != null) out.push("Piętro do " + b);
    if (f.no_ground) out.push("Bez parteru");
    if (f.no_top) out.push("Bez ostatniego piętra");
    (f.features || []).forEach(function (x) { if (MONTHLY[deal] || x !== "zwierzeta") out.push(FEAT_LABEL[x] || x); });
    if (f.furnished === true) out.push("Umeblowane");
    if (f.furnished === false) out.push("Nieumeblowane");
    (f.conditions || []).forEach(function (x) { out.push(COND_LABEL[x] || x); });
    (f.building_types || []).forEach(function (x) { out.push(BT_LABEL[x] || x); });
    if (deal === "sprzedaz") (f.market || []).forEach(function (m) { out.push("Rynek " + (m === "wtorny" ? "wtórny" : m)); });
    (f.seller || []).forEach(function (s) { out.push(SELLER_LABEL[s] || s); });
    (f.sources || []).forEach(function (s) { out.push(SOURCE_NAMES[s] || s); });
    if (f.text) out.push("„" + f.text + "”");
    return out;
  }
  function autoName(al) {
    var s = summary(al).slice(0, 2);
    return s.length ? s.join(", ") : DEAL_NAMES[al.deal];
  }
  function secretName(name) {
    var s = String(name || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/ł/g, "l")
      .toUpperCase().replace(/[^A-Z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 40).replace(/_+$/, "");
    return "ALERT_" + (s || "1");
  }

  // ------------------------------------------------------------------ stan panelu
  var st = { tab: "new", area: null, radius: R_DEF, areaOn: false, emails: [], drops: true, name: "", result: null, loaded: null };
  var map = null, marker = null, circle = null, loadedMap = null;

  function currentAlert() {
    var MW = window.MW, deal = MW.getDeal();
    var al = { v: 1, name: st.name.trim(), emails: st.emails.slice(), deal: deal, city: MW.getCity(),
      filters: filtersFromSite(MW.getFilters(), deal), price_drops: !!st.drops };
    if (st.areaOn && st.area) al.area = { lat: +st.area[0].toFixed(6), lon: +st.area[1].toFixed(6), radius_m: st.radius };
    return al;
  }

  // ------------------------------------------------------------------ DOM
  function build() {
    var el = document.createElement("div");
    el.innerHTML =
      '<div class="al-scrim" id="alScrim" hidden></div>' +
      '<aside class="al-panel" id="alPanel" role="dialog" aria-modal="true" aria-labelledby="alTitle" hidden>' +
        '<div class="al-head"><h2 id="alTitle">Powiadomienia e-mail</h2>' +
          '<button type="button" class="icon-btn" data-al="close" aria-label="Zamknij">' + icon("x") + "</button></div>" +
        '<div class="al-tabs" role="tablist">' +
          '<button type="button" role="tab" data-al-tab="new">Nowy alert</button>' +
          '<button type="button" role="tab" data-al-tab="load">Wczytaj alert</button></div>' +
        '<div class="al-body" id="alBody"></div>' +
        '<div class="al-foot" id="alFoot"></div>' +
      "</aside>";
    while (el.firstChild) document.body.appendChild(el.firstChild);
  }

  function open() {
    if (!window.MW) return;
    if (!st.emails.length && !st.loaded) st.emails = lsGet("alert_emails", []);
    st.result = null;
    $("alPanel").hidden = false;
    $("alScrim").hidden = false;
    document.body.classList.add("lock");
    render();
    setTimeout(function () { var b = $("alPanel").querySelector(".al-tabs button[aria-selected=true]"); if (b) b.focus(); }, 30);
  }
  function close() {
    $("alPanel").hidden = true;
    $("alScrim").hidden = true;
    document.body.classList.remove("lock");
    $("alertBtn").focus();
  }

  function render() {
    document.querySelectorAll("[data-al-tab]").forEach(function (b) {
      b.setAttribute("aria-selected", String(b.getAttribute("data-al-tab") === st.tab));
    });
    var body = $("alBody"), foot = $("alFoot");
    body.scrollTop = 0;
    if (st.tab === "load") { renderLoad(body, foot); return; }
    if (st.result) { renderResult(body, foot); return; }
    renderForm(body, foot);
  }

  function chipsHtml(list) {
    return list.map(function (s) { return '<span class="al-chip">' + esc(s) + "</span>"; }).join("");
  }

  function renderForm(body, foot) {
    var MW = window.MW, F = MW.getFilters(), al = currentAlert(), parts = summary(al).filter(function (s) { return !/od punktu$/.test(s); });
    var notes = [];
    if (F.days) notes.push("Filtr „Dodane” nie dotyczy powiadomień — mail zawsze zawiera tylko oferty nowe od ostatniego sprawdzenia.");
    if (F.tab === "fav") notes.push("Zakładka „Ulubione” nie wpływa na alert.");
    body.innerHTML =
      '<section class="al-sec">' +
        '<h4>Co obserwujemy</h4>' +
        '<div class="al-deal">' + esc(DEAL_NAMES[al.deal]) + "</div>" +
        (parts.length ? '<div class="al-chips">' + chipsHtml(parts) + "</div>"
          : '<p class="al-warn">Nie ustawiono żadnych filtrów — dostaniesz wszystkie nowe oferty tego rodzaju (może ich być kilkadziesiąt dziennie).</p>') +
        '<p class="hint">Alert używa filtrów ustawionych teraz na stronie. Żeby je zmienić, zamknij to okno, ustaw filtry i otwórz je ponownie.' +
          (notes.length ? " " + esc(notes.join(" ")) : "") + "</p>" +
      "</section>" +
      '<section class="al-sec">' +
        '<label class="toggle"><input type="checkbox" id="alAreaOn"' + (st.areaOn ? " checked" : "") + '><span class="tg"></span><span><b>Okolica</b> — tylko oferty blisko wybranego miejsca</span></label>' +
        '<div id="alAreaBox"' + (st.areaOn ? "" : " hidden") + ">" +
          '<div class="al-map" id="alMap"><div class="al-map-msg">Wczytywanie mapy…</div></div>' +
          '<div class="al-radius"><label for="alRadius">Promień</label>' +
            '<input type="range" id="alRadius" min="' + R_MIN + '" max="' + R_MAX + '" step="' + R_STEP + '" value="' + st.radius + '">' +
            '<output id="alRadiusOut">' + radiusText(st.radius) + "</output></div>" +
          '<p class="hint"><span id="alAreaTip">' + (st.area ? "Kliknij w inne miejsce mapy, żeby przesunąć punkt." : "Kliknij na mapie miejsce, wokół którego szukasz.") +
            "</span> Oferty bez dokładnego adresu sprawdzamy po ulicy lub osiedlu — w mailu będą oznaczone jako „lokalizacja przybliżona”.</p>" +
        "</div>" +
      "</section>" +
      '<section class="al-sec">' +
        '<h4><label for="alName">Nazwa alertu</label></h4>' +
        '<input type="text" class="al-input" id="alName" maxlength="60" autocomplete="off" placeholder="' + esc(autoName(al)) + '" value="' + esc(st.name) + '">' +
        '<p class="hint">Pojawi się w temacie maila. Puste = opis filtrów.</p>' +
      "</section>" +
      '<section class="al-sec">' +
        '<h4><label for="alEmail">Wysyłaj na adresy</label></h4>' +
        '<div class="al-emails" id="alEmails">' + emailChips() +
          '<input type="email" id="alEmail" autocomplete="email" inputmode="email" placeholder="' + (st.emails.length ? "Dodaj kolejny adres" : "adres@gmail.com") + '"></div>' +
        '<p class="hint" id="alEmailHint">Wpisz adres i naciśnij Enter. Bez adresów maile pójdą na konto Gmail, z którego są wysyłane.</p>' +
      "</section>" +
      '<section class="al-sec">' +
        '<label class="toggle"><input type="checkbox" id="alDrops"' + (st.drops ? " checked" : "") + '><span class="tg"></span><span>Także obniżki cen pasujących ofert</span></label>' +
      "</section>";
    foot.innerHTML = '<button type="button" class="btn-text" data-al="close">Anuluj</button>' +
      '<button type="button" class="btn-primary" data-al="create">Utwórz alert</button>';
    if (st.areaOn) initMap();
  }

  function emailChips() {
    return st.emails.map(function (e, i) {
      return '<span class="al-email">' + esc(e) + '<button type="button" data-al-rm="' + i + '" aria-label="Usuń ' + esc(e) + '">' + icon("x") + "</button></span>";
    }).join("");
  }
  function refreshEmails() {
    var box = $("alEmails"), input = $("alEmail");
    box.querySelectorAll(".al-email").forEach(function (n) { n.remove(); });
    input.insertAdjacentHTML("beforebegin", emailChips());
    input.placeholder = st.emails.length ? "Dodaj kolejny adres" : "adres@gmail.com";
  }
  // dodaje adresy z pola; zwraca false, gdy coś nie jest adresem
  function addEmails(text) {
    var bad = [];
    String(text || "").split(/[\s,;]+/).filter(Boolean).forEach(function (e) {
      e = e.replace(/^<|>$/g, "");
      if (!EMAIL_RE.test(e)) { bad.push(e); return; }
      if (st.emails.map(function (x) { return x.toLowerCase(); }).indexOf(e.toLowerCase()) < 0) st.emails.push(e);
    });
    var hint = $("alEmailHint");
    if (hint) {
      hint.classList.toggle("al-err", bad.length > 0);
      hint.textContent = bad.length ? "„" + bad.join(", ") + "” nie wygląda na adres e-mail."
        : "Wpisz adres i naciśnij Enter. Bez adresów maile pójdą na konto Gmail, z którego są wysyłane.";
    }
    return bad;
  }

  function renderResult(body, foot) {
    var r = st.result, newUrl = "https://github.com/" + repo() + "/settings/secrets/actions/new";
    var listUrl = "https://github.com/" + repo() + "/settings/secrets/actions";
    body.innerHTML =
      '<div class="al-done"><span class="al-done-ico">' + icon("check") + "</span><div><b>Alert gotowy.</b> Teraz dodaj go na GitHubie — to zajmie minutę.</div></div>" +
      '<ol class="al-steps">' +
        "<li><div>Otwórz stronę dodawania sekretu (zaloguj się na GitHub, jeśli trzeba):</div>" +
          '<a class="btn-outline al-gh" href="' + esc(newUrl) + '" target="_blank" rel="noopener">Otwórz GitHub ' + icon("ext") + "</a></li>" +
        "<li><div>W polu <b>Name</b> wpisz:</div>" + copyBox("alCopyName", r.secret, false) + "</li>" +
        "<li><div>W polu <b>Secret</b> wklej kod alertu:</div>" + copyBox("alCopyCode", r.code, true) + "</li>" +
        "<li><div>Kliknij zielony przycisk <b>Add secret</b>. Gotowe!</div>" +
          '<p class="hint">Maile przychodzą po pobieraniu ofert (co godzinę) — tylko gdy pojawi się coś nowego, pasującego do alertu.' +
          " Pierwszy raz trzeba też dodać dane Gmaila (sekrety GMAIL_USER i GMAIL_APP_PASSWORD) — " +
          '<a href="https://github.com/' + esc(repo()) + '#powiadomienia-e-mail" target="_blank" rel="noopener">instrukcja</a>.</p></li>' +
      "</ol>" +
      '<div class="al-info"><h4>Jak wyłączyć albo zmienić alert</h4>' +
        "<p><b>Wyłączenie:</b> wejdź w <a href=\"" + esc(listUrl) + '" target="_blank" rel="noopener">Settings → Secrets and variables → Actions</a>' +
        " i kliknij ikonę kosza przy <b>" + esc(r.secret) + "</b>.</p>" +
        "<p><b>Zmiana:</b> utwórz nowy kod tutaj, a na GitHubie kliknij ołówek przy sekrecie i wklej nowy kod. " +
        "Gdy nazwa <b>" + esc(r.secret) + "</b> jest już zajęta, GitHub nie doda drugiego sekretu o tej samej nazwie — nadaj alertowi inną nazwę.</p>" +
        "<p>Sekrety są prywatne: choć repozytorium jest publiczne, nikt nie zobaczy adresów ani filtrów alertu.</p></div>";
    foot.innerHTML = '<button type="button" class="btn-text" data-al="back">' + icon("back") + "Wróć do edycji</button>" +
      '<button type="button" class="btn-primary" data-al="close">Zamknij</button>';
  }
  function copyBox(id, value, multi) {
    return '<div class="al-copy' + (multi ? " multi" : "") + '">' +
      (multi ? '<textarea id="' + id + '" readonly rows="3" spellcheck="false">' + esc(value) + "</textarea>"
        : '<input id="' + id + '" readonly value="' + esc(value) + '" spellcheck="false">') +
      '<button type="button" class="al-copy-btn" data-al-copy="' + id + '">' + icon("copy") + "<span>Kopiuj</span></button></div>";
  }

  function renderLoad(body, foot) {
    var recent = lsGet("alerts_made", []);
    var L = st.loaded;
    body.innerHTML =
      '<section class="al-sec">' +
        '<h4><label for="alPaste">Kod alertu</label></h4>' +
        '<textarea class="al-input" id="alPaste" rows="4" spellcheck="false" placeholder="Wklej kod z sekretu ALERT_…">' + esc(st.paste || "") + "</textarea>" +
        '<p class="hint">GitHub nie pokazuje treści zapisanych sekretów, więc zachowaj kod albo utwórz alert od nowa. Alerty utworzone w tej przeglądarce są na liście poniżej.</p>' +
        '<p class="al-err" id="alLoadErr"' + (st.loadErr ? "" : " hidden") + ">" + esc(st.loadErr || "") + "</p>" +
      "</section>" +
      (L ? '<section class="al-sec al-loaded">' +
        "<h4>" + esc(L.name || autoName(L)) + "</h4>" +
        '<div class="al-deal">' + esc(DEAL_NAMES[L.deal]) + (L.city && L.city !== "wroclaw" ? " · " + esc(L.city) : "") + "</div>" +
        '<div class="al-chips">' + (summary(L).length ? chipsHtml(summary(L)) : '<span class="muted">bez filtrów</span>') + "</div>" +
        (L.area ? '<div class="al-map small" id="alLoadedMap"></div>' : "") +
        '<dl class="al-dl"><dt>Adresy</dt><dd>' + (L.emails.length ? L.emails.map(esc).join("<br>") : "konto Gmail, z którego wysyłamy") + "</dd>" +
        "<dt>Obniżki cen</dt><dd>" + (L.price_drops ? "tak" : "nie") + "</dd></dl>" +
      "</section>" : "") +
      (recent.length ? '<section class="al-sec"><h4>Utworzone w tej przeglądarce</h4><ul class="al-recent">' +
        recent.map(function (r, i) {
          return '<li><button type="button" data-al-recent="' + i + '"><b>' + esc(r.secret) + "</b><span>" + esc(r.label || "") + " · " +
            esc(new Date(r.at).toLocaleDateString("pl-PL", { day: "numeric", month: "short" })) + "</span></button></li>";
        }).join("") + "</ul></section>" : "");
    foot.innerHTML = L
      ? '<button type="button" class="btn-text" data-al="decode">Wczytaj ponownie</button><button type="button" class="btn-primary" data-al="edit">Edytuj ten alert</button>'
      : '<button type="button" class="btn-text" data-al="close">Anuluj</button><button type="button" class="btn-primary" data-al="decode">Wczytaj</button>';
    if (L && L.area) setTimeout(function () { showLoadedMap(L); }, 0);
  }

  // ------------------------------------------------------------------ mapa
  function withLeaflet(cb) {
    if (window.L && window.L.map) return cb();
    var css = document.createElement("link");
    css.rel = "stylesheet"; css.href = LEAFLET + "leaflet.css";
    document.head.appendChild(css);
    var s = document.createElement("script");
    s.src = LEAFLET + "leaflet.js";
    s.onload = cb;
    s.onerror = function () {
      var m = document.querySelector("#alMap .al-map-msg");
      if (m) m.textContent = "Nie udało się wczytać mapy (brak internetu?).";
    };
    document.head.appendChild(s);
  }
  function center() {
    var c = CITY_CENTER[window.MW.getCity()];
    if (c) return c;
    var m = window.MW.getMap && window.MW.getMap();
    if (m) { var ll = m.getCenter(); return [ll.lat, ll.lng]; }
    return CITY_CENTER.wroclaw;
  }
  function tiles(L, m) {
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>'
    }).addTo(m);
  }
  function accent() { return getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#0f766e"; }
  function initMap() {
    withLeaflet(function () {
      var box = $("alMap");
      if (!box) return;
      var L = window.L;
      if (map) { map.remove(); map = null; marker = circle = null; }
      box.innerHTML = "";
      map = L.map(box, { preferCanvas: true, zoomControl: true, scrollWheelZoom: true }).setView(st.area || center(), st.area ? 13 : 12);
      tiles(L, map);
      map.on("click", function (e) {
        st.area = [e.latlng.lat, e.latlng.lng];
        drawArea(true);
        var h = $("alAreaTip");
        if (h) { h.classList.remove("al-err"); h.textContent = "Kliknij w inne miejsce mapy, żeby przesunąć punkt."; }
      });
      if (st.area) drawArea(true);
      setTimeout(function () { if (map) map.invalidateSize(); }, 60);
    });
  }
  function drawArea(fit) {
    if (!map || !st.area) return;
    var L = window.L, col = accent();
    if (!marker) {
      marker = L.circleMarker(st.area, { radius: 6, weight: 2, color: "#fff", fillColor: col, fillOpacity: 1 }).addTo(map);
      circle = L.circle(st.area, { radius: st.radius, color: col, weight: 2, fillColor: col, fillOpacity: 0.12 }).addTo(map);
    } else {
      marker.setLatLng(st.area);
      circle.setLatLng(st.area);
      circle.setRadius(st.radius);
    }
    if (fit) map.fitBounds(circle.getBounds(), { padding: [16, 16], maxZoom: 15 });
  }
  function showLoadedMap(al) {
    withLeaflet(function () {
      var box = $("alLoadedMap");
      if (!box) return;
      var L = window.L;
      if (loadedMap) { loadedMap.remove(); loadedMap = null; }
      loadedMap = L.map(box, { preferCanvas: true, zoomControl: false, dragging: false, scrollWheelZoom: false, doubleClickZoom: false, boxZoom: false, keyboard: false, touchZoom: false });
      var ll = [al.area.lat, al.area.lon], col = accent();
      loadedMap.setView(ll, 13);
      tiles(L, loadedMap);
      var c = L.circle(ll, { radius: al.area.radius_m, color: col, weight: 2, fillColor: col, fillOpacity: 0.12 }).addTo(loadedMap);
      L.circleMarker(ll, { radius: 5, weight: 2, color: "#fff", fillColor: col, fillOpacity: 1 }).addTo(loadedMap);
      loadedMap.fitBounds(c.getBounds(), { padding: [10, 10] });
    });
  }

  // ------------------------------------------------------------------ akcje
  function create() {
    var input = $("alEmail");
    if (input && input.value.trim()) {
      if (addEmails(input.value).length) { input.focus(); return; }
      input.value = "";
    }
    if (st.areaOn && !st.area) {
      var h = $("alAreaTip");
      if (h) { h.classList.add("al-err"); h.textContent = "Kliknij na mapie punkt albo wyłącz „Okolicę”."; }
      return;
    }
    var al = currentAlert();
    var code = encode(al);
    var name = secretName(al.name || autoName(al));
    st.result = { code: code, secret: name };
    lsSet("alert_emails", st.emails);
    var recent = lsGet("alerts_made", []).filter(function (r) { return r.code !== code; });
    recent.unshift({ secret: name, code: code, label: DEAL_NAMES[al.deal], at: Date.now() });
    lsSet("alerts_made", recent.slice(0, 12));
    render();
  }

  function decodeNow() {
    var t = $("alPaste");
    st.paste = t ? t.value : st.paste;
    try {
      st.loaded = decode(st.paste);
      st.loadErr = "";
    } catch (e) {
      st.loaded = null;
      st.loadErr = e && e.message && /[ąęółśżźćń]|kod|alert/i.test(e.message) ? e.message : "Ten kod jest niepełny albo uszkodzony — skopiuj go jeszcze raz w całości.";
    }
    render();
  }

  function editLoaded() {
    var L = st.loaded;
    if (!L) return;
    st.name = L.name || "";
    st.emails = L.emails.slice();
    st.drops = !!L.price_drops;
    st.areaOn = !!L.area;
    st.area = L.area ? [L.area.lat, L.area.lon] : null;
    st.radius = L.area ? Math.min(R_MAX, Math.max(R_MIN, Math.round(L.area.radius_m / R_STEP) * R_STEP)) : R_DEF;
    st.tab = "new";
    st.result = null;
    // filtry alertu ustawiamy na stronie — app.js przełączy się sam (obsługa #hash)
    var h = hashFromAlert(L);
    if (location.hash.replace(/^#/, "") !== h) location.hash = h;
    setTimeout(render, 80);
  }

  function copy(id, btn) {
    var el = $(id);
    if (!el) return;
    var done = function () {
      btn.classList.add("ok");
      btn.innerHTML = icon("check") + "<span>Skopiowano</span>";
      setTimeout(function () { btn.classList.remove("ok"); btn.innerHTML = icon("copy") + "<span>Kopiuj</span>"; }, 1800);
    };
    var fallback = function () { el.focus(); el.select(); try { document.execCommand("copy"); done(); } catch (e) { /* zaznaczone — Ctrl+C */ } };
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(el.value).then(done, fallback);
    else fallback();
  }

  function bind() {
    $("alertBtn").addEventListener("click", open);
    $("alScrim").addEventListener("click", close);
    var panel = $("alPanel");
    panel.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { e.stopPropagation(); close(); }
    });
    panel.addEventListener("click", function (e) {
      var t = e.target;
      var tab = t.closest("[data-al-tab]");
      if (tab) {
        if (st.tab === "new" && $("alName")) st.name = $("alName").value;
        st.tab = tab.getAttribute("data-al-tab");
        render();
        return;
      }
      var rm = t.closest("[data-al-rm]");
      if (rm) { st.emails.splice(+rm.getAttribute("data-al-rm"), 1); refreshEmails(); $("alEmail").focus(); return; }
      var cp = t.closest("[data-al-copy]");
      if (cp) { copy(cp.getAttribute("data-al-copy"), cp); return; }
      var rc = t.closest("[data-al-recent]");
      if (rc) {
        var r = lsGet("alerts_made", [])[+rc.getAttribute("data-al-recent")];
        if (r) { st.paste = r.code; decodeNow(); }
        return;
      }
      if (t.closest("#alEmails") && t.id !== "alEmail") { $("alEmail").focus(); return; }
      var a = t.closest("[data-al]");
      if (!a) return;
      var act = a.getAttribute("data-al");
      if (act === "close") close();
      else if (act === "create") create();
      else if (act === "back") { st.result = null; render(); }
      else if (act === "decode") decodeNow();
      else if (act === "edit") editLoaded();
    });
    panel.addEventListener("input", function (e) {
      var t = e.target;
      if (t.id === "alName") { st.name = t.value; }
      else if (t.id === "alRadius") {
        st.radius = +t.value;
        $("alRadiusOut").textContent = radiusText(st.radius);
        drawArea(false);
      }
    });
    panel.addEventListener("change", function (e) {
      var t = e.target;
      if (t.id === "alAreaOn") {
        st.areaOn = t.checked;
        $("alAreaBox").hidden = !t.checked;
        if (t.checked) initMap();
      } else if (t.id === "alDrops") st.drops = t.checked;
      else if (t.id === "alRadius" && circle) map.fitBounds(circle.getBounds(), { padding: [16, 16], maxZoom: 15 });
    });
    panel.addEventListener("keydown", function (e) {
      var t = e.target;
      if (t.id === "alEmail") {
        if (e.key === "Enter" || e.key === "," || e.key === ";" || (e.key === " " && t.value.trim())) {
          e.preventDefault();
          if (!addEmails(t.value).length) t.value = "";
          refreshEmails();
        } else if (e.key === "Backspace" && !t.value && st.emails.length) {
          st.emails.pop();
          refreshEmails();
        }
      } else if (t.id === "alName" && e.key === "Enter") {
        e.preventDefault();
        create();
      }
    });
    panel.addEventListener("focusout", function (e) {
      var t = e.target;
      if (t.id === "alEmail" && t.value.trim()) {
        if (!addEmails(t.value).length) t.value = "";
        refreshEmails();
      }
    });
    panel.addEventListener("paste", function (e) {
      if (e.target.id !== "alEmail") return;
      var text = (e.clipboardData || window.clipboardData).getData("text");
      if (/[\s,;]/.test(text.trim())) { e.preventDefault(); addEmails(text); refreshEmails(); }
    });
  }

  function init() {
    if (!$("alertBtn")) return;
    build();
    bind();
  }
  // do testów w konsoli / headless
  window.MWAlerts = { encode: encode, decode: decode, filtersFromSite: filtersFromSite, hashFromAlert: hashFromAlert,
    summary: summary, secretName: secretName, currentAlert: function () { return currentAlert(); } };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
