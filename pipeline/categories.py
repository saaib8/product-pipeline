"""Canonical product-category vocabulary.

This list is NOT ours to invent — it mirrors the object-detection model's class
vocabulary **verbatim**. A category the detector doesn't know can never produce a
detection, so adding one here without retraining the model is a silent no-op.

The stored form is exactly what the detector emits. Most classes are hyphenated; a
handful are spaced because the training data was annotated that way. We do NOT rewrite
them — a hyphen we invent is a class the model has never heard of.

Instead, normalisation is used only as a **lookup key**, so any input spelling resolves
to the one canonical string:

    resolve("Leg Press Machine") == resolve("leg-press-machine") == "leg press machine"

Apply :func:`norm_category` on BOTH sides of any comparison — merchant input, this
list, and the detector's returned label — so label formatting can never cause a silent
``detection = false``.
"""

from __future__ import annotations

import re

_SEPARATORS = re.compile(r"[\s_]+")


def norm_category(value: str | None) -> str:
    """Lookup key for a category label: lowercase, separators collapsed to hyphens.

    This is a comparison key, never a value to store. Safe on None/empty.
    """
    return _SEPARATORS.sub("-", str(value or "").strip().lower())


#: The detector's classes, spelled exactly as the model emits them.
CATEGORIES: tuple[str, ...] = (
    "chair",
    "2-seater-sofa",
    "3-seater-sofa",
    "l-shape-sofa",
    "sofa",
    "bed",
    "bedspread",
    "pillow",
    "mattresses",
    "mattress-pad",
    "service-table",
    "center-table",
    "side-table",
    "console",
    "dressing-table",
    "comforter",
    "tv-table",
    "dining-table",
    "storage-box",
    "carpet",
    "flower-pot-and-plant",
    "statue-and-antique",
    "laundry-basket",
    "candle",
    "candlestick",
    "vase",
    "flower",
    "wall-clock",
    "shelve",
    "decorative-hanger",
    "lighting",
    "lampshade",
    "floor-stand",
    "wall-lighting",
    "outdoor-lighting",
    "chandelier",
    "pendant-lighting",
    "coffee-maker",
    "cooking-appliance",
    "food-processor",
    "cooking-pot",
    "serving-utensil-and-tray",
    "cup",
    "plate",
    "chaise-lounge",
    "art-canvas",
    "office-table",
    "office-chair",
    "wardrobe",
    "weight-bench-flat",
    "weight-bench-adjustable",
    "stationary-bike",
    "treadmill",
    "dumbbell",
    "elliptical-machine",
    "kettlebells",
    "medicine-ball",
    "power-rack",
    "yoga-mat",
    # Annotated with spaces rather than hyphens. Left exactly as the model knows them.
    "leg press machine",
    "chest press machine",
    "jump rope",
    "air bike",
    "barbell",
    "boxing gloves",
    "weight plates",
)

CATEGORY_SET: frozenset[str] = frozenset(CATEGORIES)

#: normalised key -> canonical class name, so any spelling resolves to the real one.
_BY_KEY: dict[str, str] = {norm_category(c): c for c in CATEGORIES}

#: `(value, label)` pairs for a Django `choices=` argument.
CATEGORY_CHOICES: tuple[tuple[str, str], ...] = tuple(
    (c, c.replace("-", " ").title()) for c in CATEGORIES
)


def resolve(value: str | None) -> str | None:
    """Canonical class name for any input spelling, or None if unsupported."""
    return _BY_KEY.get(norm_category(value))


def is_supported(value: str | None) -> bool:
    """Is this a category the detector knows, in any input spelling?"""
    return norm_category(value) in _BY_KEY


def matches(detector_label: str | None, category: str | None) -> bool:
    """Compare a detector label to a product category, ignoring separator style.

    The model emits mixed formats because of the annotation inconsistency, so both
    sides are normalised. Without this, six classes silently never match.
    """
    return bool(detector_label) and norm_category(detector_label) == norm_category(category)


# ── Icon generation scope ───────────────────────────────────────────────────────
# The 2D-icon pipeline covers a SUBSET of the detector's vocabulary. Mirrors
# gpt_image_icons.FLOOR_CATEGORIES / WALL_CATEGORIES, which drive prompt routing:
# floor items are drawn top-down, wall items head-on.

ICON_FLOOR_CATEGORIES: frozenset[str] = frozenset({
    "2-seater-sofa", "3-seater-sofa", "bed", "bedspread", "candle", "carpet",
    "center-table", "chair", "chaise-lounge", "coffee-maker", "comforter", "console",
    "cooking-appliance", "cooking-pot", "cup", "dining-table", "dressing-table",
    "floor-stand", "flower", "flower-pot-and-plant", "food-processor", "l-shape-sofa",
    "lampshade", "laundry-basket", "mattress-pad", "mattresses", "office-chair",
    "office-table", "pillow", "plate", "service-table", "serving-utensil-and-tray",
    "side-table", "sofa", "statue-and-antique", "storage-box", "tv-table", "vase",
    "wardrobe",
})

ICON_WALL_CATEGORIES: frozenset[str] = frozenset({
    "art-canvas", "decorative-hanger", "shelve", "wall-clock", "wall-lighting",
})

#: Every category the icon + metadata stages run for.
ICON_CATEGORIES: frozenset[str] = ICON_FLOOR_CATEGORIES | ICON_WALL_CATEGORIES


def wants_icon(category: str | None) -> bool:
    """Is this a category the 2D-icon stage covers?"""
    return norm_category(category) in {norm_category(c) for c in ICON_CATEGORIES}
