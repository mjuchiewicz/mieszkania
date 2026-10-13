"""Wspólny format oferty — każdy scraper zwraca listę Listing."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import List, Optional


@dataclass
class Listing:
    source: str                      # np. "otodom", "olx"
    id: str                          # ID ogłoszenia w danym portalu (string)
    url: str                         # pełny link do ogłoszenia
    title: str
    price: Optional[int] = None      # cena całkowita w zł
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

    @property
    def key(self) -> str:
        return f"{self.source}:{self.id}"

    @property
    def price_per_m2(self) -> Optional[int]:
        if self.price and self.area:
            return round(self.price / self.area)
        return None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        d["price_per_m2"] = self.price_per_m2
        return d
