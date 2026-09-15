from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Detection:
    category: str
    bbox: tuple[int, int, int, int]
    score: float
    mask_polygon: list | None = None


@dataclass(frozen=True)
class Segment:
    bbox: tuple[int, int, int, int]
    mask: "object | None"