"""The metadata prompt, and how the model's answer is read back.

The prompt names the ONE object being catalogued. That is a deliberate departure from
the notebook, which sent a single static prompt saying "look only at the product":
merchant photography is frequently a styled room, and a generic instruction leaves the
model to guess which of six objects is the product. Naming it — "the dining table" —
removes the guess.

The vocabularies are still assembled from `metadata_vocab`, so the list the model is
shown and the list its answer is validated against can never drift apart. They are the
same tuples.

Three rules in the prompt do real work and are easy to lose in a rewrite:

* naming the target — the room-scene defence. Stated positively ("describe the X and
  nothing else") rather than by listing props to ignore: enumerating rugs, cushions and
  lamps introduces those words for the model to attend to, and reads as an exhaustive
  list, so a mirror or a painting may not register as a prop at all.
* a set-photo clause for the three categories genuinely shot WITH seating.
* the palette is listed FLAT, and the colour rule states a FALLBACK. The original
  prompt said "choose ONLY from the palette" without saying what to do when nothing
  fits, so a silver lamp or a plain-blue mat got an honest off-list answer that
  canonicalisation then discarded. The notebook shipped 5,589 rows with 146 (2.6%)
  blank main colours for exactly this reason. Telling the model to approximate moves
  the judgement to where the image actually is.
"""

from __future__ import annotations

import json
import re

from pipeline.categories import norm_category
from pipeline.metadata_vocab import (
    ALL_COLORS,
    STYLES,
    canon_colors,
    canon_main_color,
    canon_styles,
)

_style_block = ", ".join(STYLES)
# Flat, not grouped. The families are real — `ALL_COLORS` is derived from them and the
# backend maps colours to families itself — but SHOWING them to the model put 11 illegal
# nouns next to the 33 legal ones, which then needed a rule saying "never the family
# name". Listing the colours alone removes the ambiguity instead of warning about it.
_color_block = "  " + ", ".join(ALL_COLORS)

#: Categories genuinely photographed WITH seating — a dining table with its chairs, an
#: office table with its chair, a dressing table with its stool. Without the exclusion
#: the model answers from the upholstery rather than the table top.
#:
#: Kept to these three on purpose. An earlier version also listed console, tv-table,
#: side-table and service-table, which told the model to ignore chairs that were not in
#: the photograph — an instruction about absent objects, which is the same fault as
#: enumerating props.
_SET_PHOTO_CATEGORIES = {"dining-table", "office-table", "dressing-table"}

def _cat_noun(category: str | None) -> str:
    """The category itself, de-hyphenated. Nothing more.

    There is deliberately no rewrite map. An earlier version had one and every entry
    turned out to assert something the category never said — `floor-stand` became "floor
    lamp" (it might be a plant stand), and `flower-pot-and-plant` became "potted plant",
    which points the model at the foliage so it answers Sage for a terracotta pot. The
    slug is the only description that cannot be wrong.

    Falls back to "product" when the category is missing, restoring the notebook's
    original wording rather than emitting "the None".
    """
    slug = norm_category(category)
    if not slug:
        return "product"
    return slug.replace("-", " ")


def build_prompt(category: str | None = None) -> str:
    """The metadata prompt, aimed at ONE named object in the photo."""
    noun = _cat_noun(category)
    if noun == "product":
        target = ("Look ONLY at the product in the image (ignore the background, props, "
                  "people and any room scene)")
    else:
        target = (f"The product being catalogued is the {noun}. Look ONLY at the {noun} "
                  f"in the image and describe ONLY that object. If the photo is a room "
                  f"scene or a styled set, everything else in it is a prop, not the "
                  f"product. Describe the {noun} and nothing else")
        if norm_category(category) in _SET_PHOTO_CATEGORIES:
            target += (f". The photo may show a complete set; describe ONLY the {noun} "
                       f"itself, not the seating around it")
    return f"""You are a furniture product cataloguer. {target} and return its design metadata.

Return STRICT JSON and nothing else, exactly this shape:
{{"styles": ["..."], "main_color": "...", "secondary_colors": ["..."]}}

RULES
- styles: pick 1 to 3 that best fit, ONLY from this list:
  {_style_block}
- main_color: find the dominant colour covering the largest surface of the product, then
  SELECT the one name from the PALETTE below that is visually closest to it. You are
  choosing from a fixed list of {len(ALL_COLORS)} names, not naming the colour freely.
  When the product's colour has no exact name in the list, the closest listed name IS
  the correct answer — this applies to EVERY colour, not only the examples that follow.
  For example: silver, chrome and steel are not listed, so use Light Grey; a plain
  "blue" is not listed, so use whichever of Powder Blue, Denim Blue or Navy is closest;
  clear glass takes the colour of its frame, base or contents.
- secondary_colors: 0, 1 or 2 accent colours selected the same way (never repeat the
  main colour). Use [] when there are none.

PALETTE
{_color_block}

main_color and every entry in secondary_colors must be copied from this PALETTE.

Output ONLY the JSON object."""


#: The model is told "JSON and nothing else" and mostly complies, but wraps it in prose
#: or a ```json fence often enough to need this. Greedy so a fenced object still matches.
_JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_metadata(raw: str) -> dict[str, str]:
    """Model output -> the three columns, as comma-separated text.

    Comma-separated **strings**, not lists: that is the storage format the production
    rows already use, and it is what `core_product.main_color` / `secondary_colors` /
    `styles` are — plain text columns, not arrays or JSON.

    Never raises. Unparseable output and off-palette answers both come back as empty
    strings, which the stage treats as "the model had nothing to say" — a retryable
    result rather than a crash.
    """
    match = _JSON_RE.search(raw or "")
    try:
        data = json.loads(match.group(0)) if match else {}
    except (ValueError, TypeError):
        data = {}
    if not isinstance(data, dict):
        data = {}

    main = canon_main_color(data.get("main_color"))
    styles = canon_styles(data.get("styles"))
    # The prompt forbids repeating the main colour; enforce it rather than trust it.
    secondary = [c for c in canon_colors(data.get("secondary_colors")) if c != main]

    return {
        "main_color": main,
        "secondary_colors": ", ".join(secondary),
        "styles": ", ".join(styles),
    }


def is_incomplete(meta: dict[str, str]) -> bool:
    """True when the answer is missing something the layout engine needs.

    Both `main_color` and `styles` are required, and either being blank triggers a retry:

    * `main_color` derives `main_family`, which is the ONLY field the colour-family
      filter matches on. Without it a product is invisible to a themed search.
    * `styles` derives `style_tags` and feeds the style filter.

    `secondary_colors` is deliberately NOT required. The prompt allows 0 accents ("Use []
    when there are none") and plenty of products genuinely have one colour, so demanding
    an accent would be asking the model to invent one.

    Note this fires on PARTIAL answers too, which the previous version did not: a model
    that returns a family name like "Warm Neutral" has its colour dropped by
    canonicalisation, and if styles survived, the row would otherwise be stored complete
    with no `main_family` at all.
    """
    return not (meta.get("main_color") and meta.get("styles"))
