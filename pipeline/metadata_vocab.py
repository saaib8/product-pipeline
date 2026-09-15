"""Controlled vocabularies for product metadata.

Copied verbatim from `qwen_product_metadata_colab_DB.ipynb` — the 19 styles and the
33-colour palette are the contract the model is held to, and 5,589 production rows were
written against exactly these values. Changing one silently re-labels the catalogue.

The palette is grouped into families **only to help the model decide**. The families are
never stored: a row gets `"Beige"`, never `"Warm Neutral"`.

Canonicalisation is the point of this module. The model returns free text; `canon_*`
maps it back onto the vocabulary case-insensitively and **drops anything not on the
list**. That is what stops an invented colour ever reaching the database — the same
role `matches()` plays for detector labels in `categories.py`.
"""

from __future__ import annotations

#: 1-3 of these per product.
STYLES: tuple[str, ...] = (
    "Modern", "Contemporary", "Minimalist", "Boho", "Industrial", "Classy",
    "Modern_Classic", "Rustic_Modern", "Eclectic", "Zen", "Shabby_Chic", "Islamic",
    "Tropical", "Scandinavian", "Mid_Century", "Japandi", "Coastal", "Traditional",
    "Moroccan",
)

#: Grouped for the prompt only; the family name is never stored.
COLOR_FAMILIES: dict[str, tuple[str, ...]] = {
    "Warm Neutral":        ("Ivory", "Beige", "Taupe", "Sand"),
    "Cool Neutral":        ("Greige", "Light Grey", "Grey"),
    "Monochrome":          ("White", "Charcoal", "Black"),
    "Wood / Natural":      ("Oak", "Walnut", "Espresso"),
    "Earthy / Terracotta": ("Clay", "Terracotta", "Olive"),
    "Bold / Vibrant":      ("Mustard", "Burnt Orange"),
    "Metallic / Gold":     ("Gold", "Brass", "Bronze"),
    "Green":               ("Sage", "Forest Green"),
    "Blue":                ("Powder Blue", "Denim Blue", "Navy"),
    "Jewel Tones":         ("Teal", "Emerald", "Sapphire", "Ruby", "Plum"),
    "Pastel / Soft":       ("Blush", "Lavender"),
}

ALL_COLORS: tuple[str, ...] = tuple(c for fam in COLOR_FAMILIES.values() for c in fam)

_STYLE_CANON: dict[str, str] = {s.lower(): s for s in STYLES}
_COLOR_CANON: dict[str, str] = {c.lower(): c for c in ALL_COLORS}
#: Spellings the model reaches for that are not in the palette. Kept exactly as the
#: notebook has them — without these, every American spelling is silently discarded.
_COLOR_CANON.update({
    "gray": "Grey",
    "light gray": "Light Grey",
    "dark grey": "Charcoal",
})

#: How many of each the model may return. Anything beyond is truncated, not rejected.
MAX_STYLES = 3
MAX_SECONDARY_COLORS = 2


def _canon(values, table: dict[str, str], limit: int) -> list[str]:
    """Map free text onto the vocabulary, dropping anything unknown.

    Order is preserved (the model returns its best guess first) and duplicates are
    collapsed, so `["modern","Modern","boho"]` becomes `["Modern","Boho"]`.
    """
    out: list[str] = []
    for value in (values or []):
        canonical = table.get(str(value).strip().lower())
        if canonical and canonical not in out:
            out.append(canonical)
        if len(out) >= limit:
            break
    return out


def canon_styles(values) -> list[str]:
    return _canon(values, _STYLE_CANON, MAX_STYLES)


def canon_colors(values, limit: int = MAX_SECONDARY_COLORS) -> list[str]:
    return _canon(values, _COLOR_CANON, limit)


def canon_main_color(value) -> str:
    """Exactly one colour, or empty if the model returned something off-palette."""
    if not value:
        return ""
    return _COLOR_CANON.get(str(value).strip().lower(), "")
