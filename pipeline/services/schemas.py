from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class BBox:
    x1: int
    y1: int
    x2: int
    y2: int


@dataclass
class SearchHit:
    pinecone_id: str
    score: float
    image_url: str | None = None
    product_url: str | None = None
    name_english: str | None = None
    name_arabic: str | None = None
    category: str | None = None
    price_amount: int | None = None
    price_unit: str | None = None
    is_active: bool | None = None
    store_id: int | None = None
    countries: list[str] | None = None
    store: str | None = None
    # Raw embedding vector, populated only when bundling needs it. Never
    # serialized to the client (stripped during bundling).
    values: list[float] | None = None


@dataclass
class SearchResponse:
    query_category: str | None
    hits: list[SearchHit] = None
    message: str | None = None

    def __post_init__(self):
        if self.hits is None:
            self.hits = []
