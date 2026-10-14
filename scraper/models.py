"""Wspólny format oferty — każdy scraper zwraca listę Listing."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import List, Optional


DEALS = ("sprzedaz", "wynajem", "pokoj")  # typ ogłoszenia: kupno, wynajem mieszkania, wynajem pokoju


CONDITIONS = ("do_zamieszkania", "do_wykonczenia", "stan_deweloperski", "do_remontu")
BUILDING_TYPES = ("blok", "kamienica", "apartamentowiec", "dom", "szeregowiec", "loft")
FEATURES = (
    "balkon", "taras", "loggia", "ogrodek", "winda", "parking", "garaz",
    "piwnica", "komorka", "klimatyzacja", "zwierzeta",
)


@dataclass
class Listing:
    source: str                      # np. "otodom", "olx"
    id: str                          # ID ogłoszenia w danym portalu (string)
    url: str                         # pełny link do ogłoszenia
    title: str
    deal: str = "sprzedaz"           # jeden z DEALS
    price: Optional[int] = None      # sprzedaż: cena całkowita w zł; wynajem/pokój: czynsz najmu zł/mies.
    area: Optional[float] = None     # metraż w m2
    rooms: Optional[int] = None      # liczba pokoi
    floor: Optional[str] = None      # np. "3", "parter", "3/5"
    district: Optional[str] = None   # dzielnica / osiedle Wrocławia, np. "Krzyki", "Nadodrze"
    street: Optional[str] = None
    market: Optional[str] = None     # "pierwotny" | "wtorny" | None
    seller: Optional[str] = None     # "prywatny" | "agencja" | "deweloper" | None
    build_year: Optional[int] = None
    images: List[str] = field(default_factory=list)  # URL-e zdjęć (pierwsze = miniatura)
    description: Optional[str] = None  # krótki opis, max ~500 znaków
    lat: Optional[float] = None
    lon: Optional[float] = None
    created_at: Optional[str] = None   # data publikacji wg portalu, ISO 8601, jeśli dostępna
    # cechy mieszkania (None = nie wiadomo):
    floor_num: Optional[int] = None    # piętro jako liczba: 0 = parter, -1 = suterena
    floors_total: Optional[int] = None # liczba pięter w budynku
    furnished: Optional[bool] = None   # umeblowane
    condition: Optional[str] = None    # jeden z CONDITIONS
    building_type: Optional[str] = None  # jeden z BUILDING_TYPES
    features: List[str] = field(default_factory=list)  # podzbiór FEATURES
    # tylko wynajem/pokój:
    rent_extra: Optional[int] = None   # dodatkowe opłaty / czynsz administracyjny zł/mies., jeśli podane osobno
    deposit: Optional[int] = None      # kaucja w zł
    available_from: Optional[str] = None  # dostępne od, ISO date, jeśli podane

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"

    @property
    def price_per_m2(self) -> Optional[int]:
        if self.deal == "sprzedaz" and self.price and self.area:
            return round(self.price / self.area)
        return None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        d["price_per_m2"] = self.price_per_m2
        return d
