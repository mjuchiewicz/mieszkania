"""Uzupełnianie cech mieszkania (piętro, umeblowanie, stan, typ budynku, udogodnienia)
na podstawie pola `floor` oraz tekstu ogłoszenia (tytuł + opis).

Główne API:

    enrich(d: dict) -> dict

Przyjmuje słownik oferty (jak z Listing.to_dict(), może być częściowo wypełniony przez
scraper portalu) i zwraca NOWY słownik, w którym uzupełnione są wyłącznie brakujące
wartości (None / pusta lista). Wartości ustawione przez scraper z danych strukturalnych
portalu nigdy nie są nadpisywane; dla `features` zwracana jest suma zbiorów.

Zasady:
- precyzja ważniejsza niż czułość — przy wątpliwościach (sprzeczne sygnały, zaprzeczenia,
  liczba mnoga w opisach inwestycji typu "balkony lub tarasy") nic nie ustawiamy;
- tekst analizowany jest po zamianie na małe litery, z zachowaniem polskich znaków;
  wzorce tolerują też brak ogonków (winda/winde, garaz, komorka...).

Decyzja: "stan deweloperski" i "do wykończenia" to na polskim rynku synonimy (lokal
bez podłóg, drzwi wewnętrznych, białego montażu). Z tekstu oba mapujemy na
"stan_deweloperski". Wartość "do_wykonczenia" z CONDITIONS zostaje dostępna dla
scraperów portali, które ją raportują strukturalnie (np. otodom "to_completion"),
ale enrich() jej nie wytwarza — nie znaleźliśmy w tekstach ogłoszeń rozróżnienia,
które dałoby się wiarygodnie wykryć.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple

try:  # import względny w pakiecie, ale moduł działa też samodzielnie
    from .models import FEATURES
except Exception:  # pragma: no cover
    FEATURES = (
        "balkon", "taras", "loggia", "ogrodek", "winda", "parking", "garaz",
        "piwnica", "komorka", "klimatyzacja", "zwierzeta",
    )

__all__ = ["enrich", "parse_floor_field", "parse_floor_text", "detect_furnished",
           "detect_condition", "detect_building_type", "detect_features"]

MAX_FLOOR = 40

# --------------------------------------------------------------------------- utils

_WS = re.compile(r"\s+")


def _norm(text: Optional[str]) -> str:
    if not text:
        return ""
    t = text.replace("&nbsp;", " ").replace("\xa0", " ")
    return _WS.sub(" ", t).strip()


def _missing(v) -> bool:
    return v is None or v == [] or v == ""


# znaki kończące zdanie / punkt listy — zaprzeczenie nie "przechodzi" przez nie
_CLAUSE_BREAK = re.compile(r"[.!?;\n|•✅✔➤►•]|\s[-–—]\s|\s\d+[.)]\s")

# słowa przeczące stojące przed cechą
_NEG_BEFORE = re.compile(
    r"\b(?:brak(?:uje)?|bez|nie|ani|zero|żadn\w*|zadn\w*)\b"
)
# po słowie przeczącym, a przed cechą — te słowa "zamykają" zasięg zaprzeczenia
_NEG_RESET = re.compile(
    r"\b(?:ale|jednak|natomiast|za to|z|ze|jest|są|sa|posiada|posiadają|oraz z|"
    r"wyposażon\w*|dostępn\w*|ma|mamy|oferuje\w*)\b"
)
# zaprzeczenie po cesze: "balkonu brak", "winda: nie", "winda - brak"
_NEG_AFTER = re.compile(
    r"^\s*(?:[:\-–—]\s*)?(?:brak|nie\b|nie ma\b|-\s*nie\b|x\b|✗|✘)"
)
_NEG_AFTER2 = re.compile(r"^\s*(?:nie ma|nie posiada|brak)\b")


def _negated(text: str, start: int, end: int, window: int = 40) -> bool:
    """Czy wystąpienie text[start:end] jest zaprzeczone w krótkim oknie w tym samym zdaniu."""
    pre = text[max(0, start - window):start]
    # przytnij do ostatniej granicy zdania/punktu
    cut = None
    for m in _CLAUSE_BREAK.finditer(pre):
        cut = m.end()
    if cut is not None:
        pre = pre[cut:]
    negs = list(_NEG_BEFORE.finditer(pre))
    if negs:
        gap = pre[negs[-1].end():]
        # "nie ma windy": słowo "ma" po "nie" nie resetuje
        gap_check = re.sub(r"^\s*(?:ma|posiada|posiadają|jest|będzie|przewiduje się)\b", "", gap)
        if not _NEG_RESET.search(gap_check):
            return True
    post = text[end:end + 15]
    post = _CLAUSE_BREAK.split(post)[0] if not re.match(r"^\s*[-–—]\s", post) else post
    if _NEG_AFTER.match(post) or _NEG_AFTER2.match(post):
        return True
    return False


def _first_positive(pattern: "re.Pattern", text: str, exclude: Optional["re.Pattern"] = None,
                    ctx: int = 30) -> Optional["re.Match"]:
    """Pierwsze niezaprzeczone wystąpienie wzorca (opcjonalnie z wykluczeniem kontekstu)."""
    for m in pattern.finditer(text):
        if exclude is not None:
            around = text[max(0, m.start() - ctx):m.end() + ctx]
            if exclude.search(around):
                continue
        if _negated(text, m.start(), m.end()):
            continue
        return m
    return None


# --------------------------------------------------------------------------- piętro

_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8,
          "IX": 9, "XI": 11, "XII": 12, "XIII": 13, "XIV": 14, "XV": 15, "XVI": 16}
# uwaga: samotne "X" pomijamy — w ogłoszeniach często jest symbolem zastępczym ("na X piętrze")

_ORDINALS = [
    ("pierwsz", 1), ("drug", 2), ("trzec", 3), ("czwart", 4), ("piąt", 5), ("piat", 5),
    ("szóst", 6), ("szost", 6), ("siódm", 7), ("siodm", 7), ("ósm", 8), ("osm", 8),
    ("dziewiąt", 9), ("dziewiat", 9), ("dziesiąt", 10), ("dziesiat", 10),
    ("jedenast", 11), ("dwunast", 12),
]
_ORD_RE = "|".join(sorted({o for o, _ in _ORDINALS}, key=len, reverse=True))
_ORD_MAP = dict(_ORDINALS)

_MULT = [
    ("jedno", 1), ("dwu", 2), ("trzy", 3), ("cztero", 4), ("pięcio", 5), ("piecio", 5),
    ("sześcio", 6), ("szescio", 6), ("siedmio", 7), ("ośmio", 8), ("osmio", 8),
    ("dziewięcio", 9), ("dziewiecio", 9), ("dziesięcio", 10), ("dziesiecio", 10),
    ("jedenasto", 11), ("dwunasto", 12),
]
_MULT_RE = "|".join(sorted({o for o, _ in _MULT}, key=len, reverse=True))
_MULT_MAP = dict(_MULT)

_PIETRO = r"pi[ęe]tr(?:o|ze)\b"
_PIETR_ANY = r"pi[ęe]t(?:r(?:o|ze|a)|er)\b"


def _floor_token(tok: str) -> Optional[int]:
    t = tok.strip().lower()
    if not t or t in ("-", "brak", "none"):
        return None
    if t in ("parter", "wysoki parter", "niski parter", "0"):
        return 0
    if t in ("suterena", "przyziemie", "piwnica", "-1"):
        return -1
    m = re.match(r"^(?:>|powyżej|powyzej|ponad)\s*(\d+)$", t)
    if m:
        return int(m.group(1)) + 1
    m = re.match(r"^(\d+)\s*\+$", t)
    if m:
        return int(m.group(1))
    m = re.match(r"^(\d+)(?:\s*(?:\.|piętro|pietro))?$", t)
    if m:
        return int(m.group(1))
    return None


def parse_floor_field(floor) -> Tuple[Optional[int], Optional[int]]:
    """Parsuje pole `floor` -> (floor_num, floors_total)."""
    if floor is None:
        return None, None
    if isinstance(floor, (int, float)):
        return int(floor), None
    s = _norm(str(floor)).lower()
    if not s:
        return None, None
    num = total = None
    poddasze = "poddasze" in s
    if "/" in s:
        a, b = s.split("/", 1)
        num = None if "poddasze" in a else _floor_token(a)
        bt = _floor_token(b)
        total = bt if bt is not None and bt >= 0 else None
    else:
        num = None if poddasze else _floor_token(s)
    if poddasze and num is None and total is not None:
        num = total
    if num is not None and not (-1 <= num <= MAX_FLOOR):
        num = None
    if total is not None and not (0 <= total <= MAX_FLOOR):
        total = None
    if num is not None and total is not None and num > total:
        # niespójne dane (np. "5/4") — ufamy numerowi piętra, odrzucamy liczbę pięter
        total = None
    return num, total


_RX_NUM_FLOOR = re.compile(
    r"(?<![\d/,.])(\d{1,2})\s*(?:\.|-?\s?(?:te|cie|gie|sze|ie|ym|im|m))?\s+" + _PIETRO)
_RX_ORD_FLOOR = re.compile(r"\b(" + _ORD_RE + r")(?:ym|im|e|ie|ego)\s+" + _PIETRO)
_RX_ROMAN_FLOOR = re.compile(r"(?<![\w/])([IVX]{1,4})\s*\.?\s+(?:[Pp]i[ęe]tr(?:o|ze)\b|PI[ĘE]TR(?:O|ZE)\b)")
_RX_FLOOR_PAIR = re.compile(
    r"(?:pi[ęe]tro[:\s]*)?(?<![\d,.])(\d{1,2}|parter)\s*/\s*(\d{1,2})(?!\d|\s*(?:m|zł|zl|pok))"
    r"(?=\s*(?:pi[ęe]t|\)|,|$|\s))")
_RX_FLOOR_OF = re.compile(
    r"(?<![\d,.])(\d{1,2})\s*(?:\.|-?\s?(?:te|cie|gie|ie))?\s+" + _PIETRO + r"\s*(?:z|na)\s+(\d{1,2})\b")
_RX_TOTAL_NUM = re.compile(r"(?<![\d,.])(\d{1,2})\s*-?\s*pi[ęe]trow\w*")
_RX_TOTAL_WORD = re.compile(r"\b(" + _MULT_RE + r")\s*-?\s*pi[ęe]trow\w*")
_RX_TOTAL_Z = re.compile(r"\b(?:z|ma|posiada|liczy)\s+(\d{1,2})\s+(?:kondygnacj\w*\s+)?pi[ęe]t(?:er|ra)\b")
_RX_PARTER = re.compile(r"\b(?:na\s+(?:wysokim\s+|niskim\s+)?parterze|(?:wysoki|niski)\s+parter\b|parter\s*[|,]|"
                        r"parterowe\s+mieszkanie|mieszkanie\s+parterowe)")
_RX_PARTER_EXCL = re.compile(r"(?:mieszkań|mieszkan\b|lokali|lokale|usług|sklep|recepcj|portier|"
                             r"hal[ai]\s|garaż|garaz|komór|komor|piwnic|ogródki|ogrodki|budynku\s+znajduj)")
_RX_SUTERENA = re.compile(r"\b(?:w\s+suterenie|suterena|w\s+przyziemiu)\b")
_RX_LAST = re.compile(r"\bostatni\w*\s+(?:\w+\s+)?" + _PIETRO + r"|\bna\s+ostatnim\s+pi[ęe]trze")


def parse_floor_text(text: str, raw: Optional[str] = None) -> Tuple[Optional[int], Optional[int], bool]:
    """Z tekstu (lowercase) -> (floor_num, floors_total, is_last_floor).
    `raw` = oryginalny tekst (z wielkością liter) do wykrywania cyfr rzymskich."""
    num = total = None
    m = _RX_FLOOR_OF.search(text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a <= b <= MAX_FLOOR:
            num, total = a, b
    if num is None:
        for m in _RX_FLOOR_PAIR.finditer(text):
            a = 0 if m.group(1) == "parter" else int(m.group(1))
            b = int(m.group(2))
            # wymagamy słowa "piętro" w pobliżu, by nie łapać numerów mieszkań "95/22"
            near = text[max(0, m.start() - 12):m.end() + 10]
            if re.search(r"pi[ęe]t|parter", near) and a <= b <= MAX_FLOOR:
                num, total = a, b
                break
    if num is None:
        cands = []
        m = _RX_NUM_FLOOR.search(text)
        if m:
            cands.append((m.start(), int(m.group(1))))
        m = _RX_ORD_FLOOR.search(text)
        if m:
            cands.append((m.start(), _ORD_MAP[m.group(1)]))
        if raw:
            for m in _RX_ROMAN_FLOOR.finditer(raw):
                if m.group(1) in _ROMAN:
                    cands.append((m.start(), _ROMAN[m.group(1)]))
                    break
        for m in _RX_PARTER.finditer(text):
            around = text[max(0, m.start() - 25):m.end() + 30]
            if not _RX_PARTER_EXCL.search(around):
                cands.append((m.start(), 0))
                break
        m = _RX_SUTERENA.search(text)
        if m:
            cands.append((m.start(), -1))
        if cands:
            cands.sort()
            v = cands[0][1]
            if -1 <= v <= MAX_FLOOR:
                num = v
    if total is None:
        m = _RX_TOTAL_NUM.search(text)
        if m and 1 <= int(m.group(1)) <= MAX_FLOOR:
            total = int(m.group(1))
        else:
            m = _RX_TOTAL_WORD.search(text)
            if m:
                total = _MULT_MAP[m.group(1)]
            else:
                m = _RX_TOTAL_Z.search(text)
                if m and 1 <= int(m.group(1)) <= MAX_FLOOR:
                    total = int(m.group(1))
    last = bool(_RX_LAST.search(text))
    if num is not None and total is not None and num > total:
        total = None
    return num, total, last


# --------------------------------------------------------------------------- umeblowanie

_RX_FURN_NEG = re.compile(
    r"\b(?:nie\s*umeblowan\w*|nieumeblowan\w*|bez\s+(?:\w+\s+)?mebli|bez\s+umeblowania|"
    r"(?:mieszkanie|lokal)\s+(?:jest\s+)?(?:pust[ey]\b)|pust[ey]\s+(?:mieszkanie|lokal)\b|"
    r"(?:mieszkanie|pokój|pokoj)\s+(?:jest\s+)?nieumeblowan\w*)")
_RX_FURN_POS = re.compile(
    r"\b(?:umeblowan\w*|z\s+meblami|z\s+umeblowaniem|meble\s+w\s+cenie|"
    r"(?:pozostawiamy|zostawiamy|pozostają|pozostaja|zostają|zostaja)\s+(?:\w+\s+)?meble|"
    r"(?:w\s+pełni|w\s+pelni|całkowicie|calkowicie|częściowo|czesciowo)\s+umeblowan\w*|"
    r"wyposażon\w*\s+i\s+umeblowan\w*)")
# kontekst, w którym "umeblowane/z meblami" nie dotyczy całego lokalu lub jest tylko opcją
_RX_FURN_EXCL = re.compile(
    r"(?:możliwoś\w*|mozliwos\w*|opcjonaln\w*|na\s+życzenie|na\s+zyczenie|do\s+uzgodnienia|"
    r"lub\s+(?:bez|nie)|albo\s+(?:bez|nie)|"
    r"do\s+umeblowania|łatwe?\s+do\s+umeblowania|wygodn\w+\s+do\s+umeblowania|"
    r"łatw\w*\s+(?:umeblowa|zaaranżowa)|latw\w*\s+umeblowa|ustawni\w*)")


# części mieszkania — "nieumeblowana sypialnia", "kuchnia umeblowana" nie mówią o całym lokalu
_RX_ROOM_PART = re.compile(
    r"(?:sypialni|salon|kuchni|kuchenk|aneks|łazienk|lazienk|gabinet|garderob|przedpok|pokój\s+dzienn|"
    r"pokoj\s+dzienn|pokoju\s+dzienn|jedn\w*\s+(?:z\s+)?pok|drugi\w*\s+pok)\w*")


def _room_part_near(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - 18):start]
    after = text[end:end + 14]
    return bool(_RX_ROOM_PART.search(before) or re.match(r"^\s*(?:\w+\s+)?" + _RX_ROOM_PART.pattern, after))


def detect_furnished(text: str) -> Optional[bool]:
    neg = False
    for m in _RX_FURN_NEG.finditer(text):
        around = text[max(0, m.start() - 30):m.end() + 30]
        if re.search(r"możliwoś|mozliwos|lub\s|albo\s", around):
            continue
        if _room_part_near(text, m.start(), m.end()):
            continue
        neg = True
        break
    pos = False
    for m in _RX_FURN_POS.finditer(text):
        # pomiń formy z "nie"/"nie-" (obsłużone wyżej)
        if re.search(r"(?:^|\W)nie\s*$", text[max(0, m.start() - 5):m.start()]):
            continue
        around = text[max(0, m.start() - 35):m.end() + 35]
        if _RX_FURN_EXCL.search(around):
            continue
        if re.match(r"\s*kuchenn", text[m.end():m.end() + 10]):
            continue
        if _negated(text, m.start(), m.end(), window=20):
            continue
        if _room_part_near(text, m.start(), m.end()) and not re.match(r"(?:w\s+pe|całk|calk)", m.group(0)):
            continue
        pos = True
        break
    if pos and neg:
        return None
    if pos:
        return True
    if neg:
        return False
    return None


# --------------------------------------------------------------------------- stan

_RX_COND_DEW = re.compile(
    r"\b(?:stan(?:ie)?\s+deweloperski\w*|standardzie\s+deweloperskim|do\s+(?:samodzielnego\s+)?wykończenia|"
    r"do\s+wykonczenia|do\s+własnej\s+aranżacji|stan\s+surowy\s+zamknięty)")
_RX_COND_REM = re.compile(
    r"\b(?:do\s+(?:generalnego\s+|kapitalnego\s+|częściowego\s+|czesciowego\s+|lekkiego\s+|drobnego\s+)?remontu|"
    r"do\s+(?:lekkiego\s+)?odświeżenia|do\s+odswiezenia|wymaga\w*\s+(?:\w+\s+){0,2}(?:remontu|odświeżenia|odswiezenia|nakładów|nakladow)|"
    r"do\s+modernizacji)")
_RX_COND_ZAM = re.compile(
    r"\b(?:gotow\w+\s+(?:\w+\s+)?do\s+(?:zamieszkania|wprowadzenia|wprowadzki)|"
    r"(?:^|(?<=[|,•\-–:]\s)|(?<=[|,•\-–:])|(?<=stan\s)|(?<=mieszkanie\s))do\s+zamieszkania\b|"
    r"do\s+zamieszkania\s+(?:od\s+(?:zaraz|ręki|reki)|bez\s+(?:remontu|wkładu|wkladu|nakładów|nakladow))|"
    r"(?:świeżo\s+|swiezo\s+)?po\s+(?:\w+\s+){0,2}remoncie|wyremontowan\w*|odnowion\w*|"
    r"(?:mieszkanie|apartament|lokal|kawalerka|pokój|pokoj)\s+(?:\w+\s+){0,2}wykończon\w*|"
    r"wykończon\w*\s+(?:pod\s+klucz|w\s+(?:wysokim|wysokiej|bardzo\s+wysokim|nowoczesnym|dobrym)\s+standard)|"
    r"nie\s+wymaga\w*\s+(?:\w+\s+){0,2}(?:remontu|nakładów|nakladow|wkładu|wkladu|inwestycji))")
_RX_COND_ZAM_EXCL = re.compile(
    r"(?:możliwoś\w*|mozliwos\w*|opcj\w*|pakiet\w*|oferujemy|na\s+życzenie|na\s+zyczenie|"
    r"remont\w*\s+(?:klatki|elewacji|dachu|budynku|kamienicy)|kamienic\w*\s+po\s+remoncie|"
    r"budyn\w*\s+po\s+remoncie|klatk\w*\s+(?:schodow\w*\s+)?po\s+remoncie|"
    r"elewacj\w*\s+po|odnowion\w*\s+(?:kamienic|elewac|klatk|budyn)|w\s+odnowionej\s+kamienicy|"
    r"standard\w*\s+deweloper|(?:łazienk|lazienk|kuchni|klatk|wind|elewac|budyn|kamienic|dach|okn|"
    r"podłog|podlog|instalac|częściow|czesciow|częśc|czesc)\w*\s+(?:\w+\s+){0,2}(?:odnowion|wyremontowan|po\s+(?:\w+\s+)?remoncie)|"
    r"(?:odnowion|wyremontowan)\w*\s+(?:łazienk|lazienk|kuchni|klatk|wind|elewac|budyn|kamienic|dach|okn|podłog|podlog|instalac))")


def detect_condition(text: str) -> Optional[str]:
    found = set()
    if _first_positive(_RX_COND_DEW, text, exclude=re.compile(r"możliwoś\w*\s+zakupu|mozliwos\w*\s+zakupu|w\s+stanie\s+deweloperskim\s+lub"), ctx=25):
        found.add("stan_deweloperski")
    m = None
    for m in _RX_COND_REM.finditer(text):
        around = text[max(0, m.start() - 25):m.end() + 5]
        if re.search(r"\bnie\s+(?:\w+\s+)?$", text[max(0, m.start() - 20):m.start()]) or \
                re.search(r"\bnie\s+wymaga", around):
            continue
        if re.search(r"(?:klatk|elewac|dach|budyn|kamienic)", text[m.end():m.end() + 20]):
            continue
        found.add("do_remontu")
        break
    for m in _RX_COND_ZAM.finditer(text):
        around = text[max(0, m.start() - 30):m.end() + 25]
        if m.group(0).startswith("nie"):
            found.add("do_zamieszkania")
            break
        if _RX_COND_ZAM_EXCL.search(around):
            continue
        if _negated(text, m.start(), m.end(), window=20):
            continue
        found.add("do_zamieszkania")
        break
    if len(found) == 1:
        return found.pop()
    return None


# --------------------------------------------------------------------------- typ budynku

_BT_PATTERNS = [
    ("szeregowiec", re.compile(r"\b(?:szeregow(?:iec|ca|cu|cem)|szeregówk\w*|szeregowk\w*|"
                               r"szeregówc\w*|szeregowc[eu]\b|zabudow\w*\s+szeregow\w*|dom\w*\s+szeregow\w*|segment\w*\s+szeregow\w*)\b")),
    ("dom", re.compile(r"\b(?:w\s+domu(?:\s+(?:jednorodzinnym|dwurodzinnym|wolnostojącym|wolnostojacym|"
                       r"bliźniaczym|blizniaczym))?|dom(?:u|em)?\s+(?:jednorodzinn\w*|dwurodzinn\w*|"
                       r"wolnostoj\w*|bliźniacz\w*|blizniacz\w*)|bliźniak\w*|blizniak\w*|"
                       r"zabudow\w*\s+bliźniacz\w*|zabudow\w*\s+blizniacz\w*|część\w*\s+domu|czesc\w*\s+domu|"
                       r"piętr\w*\s+domu|pietr\w*\s+domu|poddasz\w*\s+domu|parter\w*\s+domu)\b")),
    ("loft", re.compile(r"\b(?:loft(?:u|em|cie|y|ów|ow)?|typu\s+loft|budyn\w*\s+loftow\w*|"
                        r"(?:dawn\w*|zrewitalizowan\w*|odrestaurowan\w*)\s+(?:fabryk\w*|młyn\w*|mlyn\w*|"
                        r"browar\w*|spichlerz\w*|elewator\w*))\b")),
    ("apartamentowiec", re.compile(r"\bapartamentow(?:iec|ca|cu|cem)\b")),
    ("kamienica", re.compile(r"\bkamienic(?:a|y|ę|e|y|ą|zka|zce|zki)\b")),
    ("blok", re.compile(r"\b(?:blok(?:u|iem|owi)?|bloku\s+z\s+wielkiej\s+płyty|wielk\w+\s+płyt\w*|"
                        r"wielk\w+\s+plyt\w*)\b")),
]
_BT_EXCL = {
    "dom": re.compile(r"(?:dom\w*\s+studenck|dom\w*\s+(?:wielorodzinn|wielolokalow)|akademik|dom\w*\s+(?:handlow|towarow|kultury|opieki|"
                      r"spokojn\w*\s+starości)|jak\s+w\s+domu|poczuć\s+się\s+jak|czuć\s+się\s+jak|"
                      r"komfortu?\s+domu|domu\s+z\s+(?:własnym|wlasnym)\s+ogrod|zamiast\s+domu|"
                      r"pod\s+domem|przed\s+domem|blisko\s+domu|domu\s+i\s+mieszkania|"
                      r"w\s+domu\s+(?:i|lub)\s+|pracy\s+w\s+domu|z\s+domu)"),
    "loft": re.compile(r"(?:styl\w*\s+loft|loftow\w*\s+(?:okn|charakter|styl|klimat|wnętr|wnetr)|"
                       r"klimat\w*\s+loft|loft\w*\s+i\s+mieszka|mieszka\w*\s+(?:&|i)\s+loft|"
                       r"inspir\w*\s+loft|loftowy\s+open)"),
    "kamienica": re.compile(r"(?:wśród|wsrod|otoczeni\w*|sąsiedztw\w*|sasiedztw\w*|naprzeciw\w*|"
                            r"obok|okolic\w*|klimat\w*|widok\w*|stylu?|na\s+wzór|na\s+wzor|"
                            r"nawiązuj\w*|nawiazuj\w*|zamiast)\s+(?:\w+\s+){0,2}kamienic"),
    "blok": re.compile(r"(?:blok\w*\s+(?:kuchenn|starcz|tematyczn|zajęć|zajec|reklam|ćwicze|cwicze|"
                       r"czekolad|rysunk|grzewcz)|blokad|sąsiedn\w*\s+blok|sasiedn\w*\s+blok|"
                       r"naprzeciw\w*\s+blok|obok\s+blok|między\s+blok|miedzy\s+blok|"
                       r"w\s+odróżnieniu\s+od\s+blok|zamiast\s+blok|nie\s+(?:jest|w)\s+blok)"),
    "apartamentowiec": re.compile(r"(?:obok|naprzeciw\w*|sąsiedn\w*|sasiedn\w*)\s+apartamentow"),
}
# typ pewny, gdy jeden; dopuszczamy "blok" + "apartamentowiec" -> apartamentowiec (ten sam budynek
# bywa opisywany obu słowami), w innych przypadkach sprzeczności zwracamy None
_BT_COMPAT = {frozenset({"blok", "apartamentowiec"}): "apartamentowiec",
              frozenset({"kamienica", "loft"}): "loft",
              frozenset({"szeregowiec", "dom"}): "szeregowiec"}


def _bt_in(text: str) -> set:
    found = set()
    for name, rx in _BT_PATTERNS:
        excl = _BT_EXCL.get(name)
        for m in rx.finditer(text):
            if excl is not None:
                around = text[max(0, m.start() - 40):m.end() + 30]
                if excl.search(around):
                    continue
            if name == "kamienica":
                # liczba mnoga "kamienice" zwykle opisuje okolicę
                if m.group(0) == "kamienice" and not re.search(r"\bw\s+$", text[max(0, m.start() - 3):m.start()]):
                    continue
            found.add(name)
            break
    return found


def _resolve_bt(found: set) -> Optional[str]:
    if len(found) == 1:
        return next(iter(found))
    if len(found) == 2:
        return _BT_COMPAT.get(frozenset(found))
    return None


def detect_building_type(title: str, desc: str, deal: Optional[str] = None) -> Optional[str]:
    t = _resolve_bt(_bt_in(title)) if title else None
    if t:
        return t
    found = _bt_in(title + " \n " + desc)
    if deal == "pokoj" and "dom" in found and len(found) > 1:
        found.discard("dom")
    return _resolve_bt(found)


# --------------------------------------------------------------------------- udogodnienia

# (nazwa, wzorzec, wykluczenie kontekstu)
_FEAT = [
    ("balkon", re.compile(r"\bbalkon(?:u|em|ie|ik\w*)?\b|\b(?:dwa|2|trzy|3|oba|dwoma|dwóch|dwoch)\s+balkon\w*"), None),
    ("taras", re.compile(r"\btaras(?:u|em|ie|ik\w*)?\b|\b(?:dwa|2|oba|dwoma|dwóch|dwoch)\s+taras\w*"),
     re.compile(r"(?:taras\w*\s+widokow|wspóln\w*\s+(?:\w+\s+)?taras|wspoln\w*\s+(?:\w+\s+)?taras|"
                r"ogólnodostępn\w*\s+taras|ogolnodostepn\w*\s+taras|taras\w*\s+na\s+dachu\s+(?:budynku|dla)|"
                r"taras\w*\s+(?:rekreacyjn|dla\s+mieszkańców|dla\s+mieszkancow))")),
    ("loggia", re.compile(r"\blog(?:g)?i(?:a|i|ę|ą)\b|\b(?:dwie|2|obie|dwoma|dwóch|dwoch)\s+log(?:g)?i\w*"), None),
    ("ogrodek", re.compile(r"\bogr[oó]d(?:ek|ka|kiem|ku)\b|\bogr[oó]d(?:em)?\s+przydomow\w*|"
                           r"\bprzydomow\w*\s+ogr[oó]d\w*|\b(?:własn\w*|wlasn\w*|prywatn\w*)\s+ogr[oó]d(?:em|u|\b)|"
                           r"\bz\s+ogrodem\b|\bogr[oó]d\s+\d+\s*m"),
     re.compile(r"(?:ogr[oó]d\w*\s+(?:botaniczn|japońsk|japonsk|zoologiczn|zimow|działkow|dzialkow|"
                r"jordanowsk|na\s+dachu|wspóln|wspoln|osiedlow)|ogródk\w*\s+działkow|ogrodk\w*\s+dzialkow|"
                r"wspóln\w*\s+ogr|wspoln\w*\s+ogr|dom\w*\s+z\s+(?:własnym\s+)?ogrodem|"
                r"ogródk\w*\s+(?:piwn|restaur|kawiar)|osiedl\w*\s+z\s+ogrodem)")),
    ("winda", re.compile(r"\bwind(?:a|y|ę|e|ą|zie|ami)\b|\bdźwig\w*\s+osobow\w*"),
     re.compile(r"(?:wind\w*\s+(?:samochodow|towarow)|platform\w*\s+(?:dla|przyschodow))")),
    ("parking", re.compile(r"\bparking(?:u|iem|i|ów|ow)?\b|\bmiejsc\w*\s+(?:\w+\s+)?(?:parkingow\w*|postojow\w*)|"
                           r"\bstanowisk\w*\s+postojow\w*"),
     re.compile(r"(?:rower|parking\w*\s+(?:publiczn|miejsk|ogólnodostępn|ogolnodostepn|płatn|platn|"
                r"przy\s+(?:sklep|galeri|centrum|markec)|pod\s+(?:sklep|galeri|markec))|"
                r"strefa\s+płatnego|strefie\s+płatnego|problem\w*\s+z\s+(?:parking|miejsc)|"
                r"park\s*&\s*ride|p\+r)")),
    ("garaz", re.compile(r"\bgara(?:ż|z)(?:u|em|owa|owej|ową|owe|owego|owym|owych)?\b"),
     re.compile(r"(?:bram\w*\s+gara|garaż\w*\s+dla\s+rower|garaz\w*\s+dla\s+rower|"
                r"wjazd\w*\s+do\s+gara)")),
    ("piwnica", re.compile(r"\bpiwnic(?:a|y|ę|e|ą|zka|zki|zką|zkę)\b|\bpiwnicę\b"),
     re.compile(r"(?:wspóln\w*\s+piwnic|wspoln\w*\s+piwnic|w\s+piwnicy\s+(?:znajduje\s+się\s+)?(?:pralni|suszarni|rowerowni))")),
    ("komorka", re.compile(r"\bkom[oó]r(?:ka|ki|kę|ke|ką|ka|ek|kami|ce)\s+lokatorsk\w*|"
                           r"\blokatorsk\w*\s+kom[oó]r\w*|\bkom[oó]rk\w*\s+(?:w\s+piwnicy|na\s+kondygnacji)"), None),
    ("klimatyzacja", re.compile(r"\bklimatyzac(?:ja|ji|ję|je|ją)\b|\bklimatyzowan\w*|\bklima\b|\bklimatyzator\w*"),
     re.compile(r"(?:pod\s+klimatyzac|przygotowan\w*\s+(?:\w+\s+){0,3}klimat|instalacj\w*\s+pod|"
                r"możliwoś\w*\s+(?:\w+\s+){0,3}klimat|mozliwos\w*\s+(?:\w+\s+){0,3}klimat|"
                r"przyłącz\w*\s+(?:\w+\s+){0,2}klimat|przylacz\w*\s+(?:\w+\s+){0,2}klimat|"
                r"klimatyzac\w*\s+(?:gratis|w\s+promocji|w\s+cenie\s+do)|promocj\w*\s+klimatyzac|"
                r"klimat\w*\s+(?:dla|do\s+zakupu)|instalacj\w*\s+klimatyzac\w*\s+(?:możliw|mozliw)|"
                r"klimatyzac\w*\s+(?:w\s+)?(?:części\s+wspóln|czesci\s+wspoln|lobby|holu|recepcj))")),
]

# formy liczby mnogiej typowe dla opisów całej inwestycji ("balkony lub tarasy") — pomijane,
# chyba że poprzedza je liczebnik (obsłużone w osobnych gałęziach wzorców)
_PLURAL_ALT = re.compile(
    r"\b(?:balkon\w*|taras\w*|ogr[oó]d\w*|loggi\w*)\s*(?:lub|albo|bądź|badz|czy|/|i/lub)\s*"
    r"(?:\w+\s+){0,2}(?:balkon\w*|taras\w*|ogr[oó]d\w*|loggi\w*)")
_INVESTMENT = re.compile(
    r"(?:mieszkania\s+(?:posiadają|posiadaja|mają|maja|z\s+(?:balkonami|tarasami|ogródkami|ogrodkami))|"
    r"wszystkie\s+mieszkania|każde\s+mieszkanie|kazde\s+mieszkanie|w\s+inwestycji|lokale\s+(?:posiadają|mają)|"
    r"do\s+mieszkań\s+(?:na\s+parterze|przynale))")

_RX_PETS_POS = re.compile(
    r"(?:zwierz\w*\s+(?:są\s+|sa\s+)?(?:mile\s+widzian\w*|akceptowan\w*|dozwolon\w*|"
    r"(?:\w+\s+)?(?:nie\s+)?(?:stanowi\w*\s+problemu|nie\s+przeszkadzaj\w*)|tak\b|ok\b|po\s+uzgodnieniu)|"
    r"(?:akceptuj\w*|akceptowan\w*|dopuszcza\w*|dozwolon\w*|przyjmuj\w*|zgoda\s+na|zgodą\s+na)\s+"
    r"(?:\w+\s+){0,2}(?:zwierz\w*|psa|psy|kota|koty|pupil\w*)|"
    r"przyjazn\w*\s+(?:dla\s+)?zwierz\w*|pet\s*-?\s*friendly|"
    r"(?:można|mozna|możesz|mozesz|moż\w+\s+mieć|moz\w+\s+miec)\s+(?:\w+\s+){0,3}(?:zwierz\w*|psa|kota|pupil\w*)|"
    r"(?:ze?|wraz\s+ze?)\s+zwierz(?:ętami|etami|akiem|ęciem|eciem)\s+(?:mile|akcept|ok|dozwol)|"
    r"(?:pies|kot|psy|koty|pupil\w*)\s+(?:\w+\s+)?(?:mile\s+widzian\w*|akceptowan\w*|dozwolon\w*))")
_RX_PETS_NEG = re.compile(
    r"(?:bez\s+(?:\w+\s+)?zwierz|nie\s+(?:\w+\s+){0,2}(?:akceptuj\w*|przyjmuj\w*|wynajmuj\w*|"
    r"dopuszcza\w*)\s+(?:\w+\s+){0,3}(?:zwierz|psów|psow|kotów|kotow)|"
    r"zwierz\w*\s+(?:\w+\s+){0,2}nie\s+(?:są\s+|sa\s+)?(?:akceptowan|dozwolon|mile|przyjmowan|wchodz)|"
    r"zakaz\w*\s+(?:\w+\s+){0,2}zwierz|nie\s+dla\s+(?:\w+\s+)?(?:zwierz|os[oó]b\s+ze?\s+zwierz)|"
    r"nie\s+akceptujemy\s+zwierz|brak\s+zgody\s+na\s+zwierz|zwierz\w*\s*[:\-–]\s*nie\b)")


def detect_features(text: str, deal: Optional[str] = None) -> List[str]:
    out = []
    # obszary tekstu z alternatywami "balkon lub taras" -> ignorujemy te fragmenty
    alt_spans = [(m.start(), m.end()) for m in _PLURAL_ALT.finditer(text)]
    for name, rx, excl in _FEAT:
        hit = negated = False
        for m in rx.finditer(text):
            if any(a <= m.start() < b for a, b in alt_spans):
                continue
            if excl is not None:
                around = text[max(0, m.start() - 35):m.end() + 35]
                if excl.search(around):
                    continue
            if name in ("balkon", "taras", "ogrodek", "loggia"):
                around = text[max(0, m.start() - 45):m.start()]
                if _INVESTMENT.search(around):
                    continue
            if _negated(text, m.start(), m.end()):
                negated = True
                continue
            hit = True
        # sprzeczne sygnały ("bez windy" ... "nowa winda") -> dla precyzji nic nie dodajemy
        if hit and not negated:
            out.append(name)
    if deal != "sprzedaz":
        if _RX_PETS_POS.search(text) and not _RX_PETS_NEG.search(text):
            out.append("zwierzeta")
    return out


# --------------------------------------------------------------------------- enrich

def _order_features(feats: Iterable[str]) -> List[str]:
    s = set(feats)
    ordered = [f for f in FEATURES if f in s]
    ordered += sorted(f for f in s if f not in FEATURES)  # nieznane (od scrapera) zachowujemy
    return ordered


def enrich(d: dict) -> dict:
    """Zwraca kopię `d` z uzupełnionymi brakującymi polami cech mieszkania."""
    out = dict(d)
    title_raw = _norm(d.get("title"))
    desc_raw = _norm(d.get("description"))
    raw = title_raw + " \n " + desc_raw
    title = title_raw.lower()
    desc = desc_raw.lower()
    text = raw.lower()
    deal = d.get("deal")

    # --- piętro (pole `floor` ma pierwszeństwo przed tekstem)
    if _missing(out.get("floor_num")) or _missing(out.get("floors_total")):
        floor_raw = d.get("floor")
        fl = floor_raw.lower() if isinstance(floor_raw, str) else ""
        poddasze = "poddasze" in fl
        fnum, ftot = parse_floor_field(floor_raw)
        tnum, ttot, last = parse_floor_text(text, raw)
        known = out.get("floor_num") if not _missing(out.get("floor_num")) else fnum
        if known is None and not poddasze:
            known = tnum
        if ftot is None and not _missing(out.get("floors_total")):
            ftot = out["floors_total"]
        if ftot is None and ttot is not None and (known is None or known <= ttot):
            ftot = ttot
        if known is None and ftot is not None and (last or poddasze):
            known = ftot
        if _missing(out.get("floor_num")):
            out["floor_num"] = known
        if _missing(out.get("floors_total")):
            fn = out.get("floor_num")
            out["floors_total"] = ftot if (ftot is None or fn is None or fn <= ftot) else None

    # --- umeblowanie
    if out.get("furnished") is None:
        out["furnished"] = detect_furnished(text)

    # --- stan
    if _missing(out.get("condition")):
        out["condition"] = detect_condition(text)

    # --- typ budynku
    if _missing(out.get("building_type")):
        out["building_type"] = detect_building_type(title, desc, deal)

    # --- udogodnienia (suma zbiorów)
    existing = list(out.get("features") or [])
    out["features"] = _order_features(existing + detect_features(text, deal))
    return out


# --------------------------------------------------------------------------- backfill

def _backfill(deals: List[str]) -> None:
    """Jednorazowo uzupełnia cechy w istniejących docs/data/<miejscowość>/<typ>.json (bez pobierania ofert).
    Opisy są tam już przycięte — analizujemy to, co jest."""
    from collections import Counter

    from scraper.main import DATA_DIR, _clean_features, deal_path, load_state, save_state

    cities = sorted(p.name for p in DATA_DIR.iterdir() if p.is_dir())
    for city, deal in [(c, d) for c in cities for d in deals]:
        if not deal_path(deal, city).exists():
            continue
        state = load_state(deal, city)
        listings = []
        stats: Counter = Counter()
        for rec in state.values():
            new = enrich(rec)
            _clean_features(new)
            for k in ("floor_num", "furnished", "condition", "building_type"):
                if rec.get(k) is None and new.get(k) is not None:
                    stats[k] += 1
            if len(new.get("features") or []) > len(rec.get("features") or []):
                stats["features"] += 1
            listings.append(new)
        save_state(deal, listings, city)
        print("%s/%s: %d ofert, uzupełniono: %s" % (
            city, deal, len(listings), ", ".join("%s=%d" % kv for kv in sorted(stats.items())) or "nic"))


if __name__ == "__main__":
    import sys

    from scraper.models import DEALS as _DEALS

    args = sys.argv[1:]
    if not args or args[0] != "--backfill":
        print("Użycie: python -m scraper.features --backfill [sprzedaz wynajem pokoj]")
        sys.exit(2)
    _backfill(args[1:] or list(_DEALS))
