"""2D-icon prompts — ported verbatim from `regenerate_icons.py`.

This is the real intellectual property of the icon pipeline: which view each category
is drawn from, and the avoid-lists that stop `gpt-image-2` reverting to the source
photo's marketing perspective. It is copied rather than rewritten, because every branch
here encodes something learned from output that came back wrong.

Only the `gi.` indirections are changed: `normalize_category` now comes from
`pipeline.categories`, and `WALL_CATEGORIES` is inlined from `gpt_image_icons`.
"""

from __future__ import annotations

import re

from pipeline.categories import ICON_WALL_CATEGORIES, norm_category

REGEN_CATEGORIES = frozenset({
    "2-seater-sofa", "3-seater-sofa", "l-shape-sofa", "chaise-lounge", "sofa",
    "chair", "office-chair", "bed",
})


# Categories rendered FLAT / front-on (wall-mounted). Starts from gpt_image_icons'
# WALL set, but 'shelve' is shown TOP-DOWN in a floor plan, so it's excluded here.
WALL_FLAT_CATEGORIES = frozenset(ICON_WALL_CATEGORIES) - {"shelve"}

# Hanging / ceiling lighting: seen straight down a pendant or chandelier is an unreadable
# ring, so (like wall-lighting) these read best as a straight-on FRONT elevation.
# NB: floor-stand is intentionally NOT here — it is shown strictly TOP-DOWN like other floor items.
FRONT_LIGHTING = frozenset({
    "pendant-lighting", "chandelier", "lighting", "outdoor-lighting",
})
# Everything shown as a head-on front view rather than top-down.
FRONT_FLAT_CATEGORIES = WALL_FLAT_CATEGORIES | FRONT_LIGHTING

# Tables whose product photos are usually a full SET (table + chairs). The icon must show the
# table ALONE, so we explicitly strip the seating from the prompt for these categories.
TABLE_ONLY_CATEGORIES = frozenset({"dining-table"})


# Explicit avoid-list, folded into the positive prompt (gpt-image-2 /images/edits has
# no negative_prompt). The model's default failure mode is keeping the input photo's 3/4
# marketing perspective, so we name every perspective/depth cue to suppress.
_AVOID_TOPDOWN = (
    " Do NOT include: perspective, tilt, angle, any three-quarter / isometric / front / "
    "side / eye-level view, any visible vertical face or side, any thickness, height or "
    "depth cue, drop or cast shadow, reflection, floor, room, scene, props, other objects, "
    "people, hands, text, watermark, logo, label, frame or border."
)
_AVOID_FRONT = (
    " Do NOT include: perspective, tilt, angle, any three-quarter / isometric / top-down / "
    "side view, any visible depth or side face, drop or cast shadow, reflection, wall, room, "
    "props, other objects, people, hands, text, watermark, logo, label, frame or border."
)


# A side-table product whose NAME says it's a set (e.g. "Set of two side tables", "Set of
# side tables") must be drawn as the whole set, not a single table.
_SIDE_TABLE_SET_RE = re.compile(r"set of\s+(?:two|2|three|3)?\s*side tables?|side tables?\s+set",
                                re.IGNORECASE)


def _is_side_table_set(name) -> bool:
    return bool(name and _SIDE_TABLE_SET_RE.search(name))


def build_prompt(category: str, name=None) -> str:
    slug = norm_category(category)
    cat = slug.replace("-", " ")
    # FRONT-view items: wall-mounted things (art-canvas, wall-clock, wall-lighting,
    # decorative-hanger) plus hanging/upright lighting (pendant, chandelier, floor lamp) —
    # a top-down view of these is an edge-on sliver or unreadable ring, so they read best as
    # a flat straight-on elevation. FLOOR items stay top-down.
    if slug in FRONT_FLAT_CATEGORIES:
        return (
            f"Transform this product photo into a single {cat}, redrawn as a strictly "
            f"straight-on FRONT view (flat, head-on elevation), photographed dead level from "
            f"directly in front with a perfectly orthographic lens and ZERO perspective, tilt "
            f"or angle. You must see ONLY its full flat front face — never the top, never a "
            f"side, never any depth or edge. "
            f"Preserve the product's exact real colors, materials, and proportions. Show "
            f"exactly one isolated {cat}, centered, on a pure flat white background "
            f"(#FFFFFF). Clean crisp edges, professional flat 2D wall icon." + _AVOID_FRONT
        )
    # FLOOR-STAND (floor lamp / plant / coat stand): a tall vertical object. Seen straight down
    # it is just a small disc, so the model keeps defaulting to a side/standing view. This
    # dedicated prompt hammers the nadir view: looking DOWN the pole, height collapses to a
    # point, render concentric flat rings (top over base), pole = a dot at the centre.
    if slug == "floor-stand":
        return (
            f"Transform this product photo into a single {cat}, redrawn as a STRICTLY TOP-DOWN "
            f"(bird's-eye / architectural plan) 2D floor-plan icon, viewed by a camera mounted on "
            f"the ceiling looking straight DOWN the vertical pole of the stand (nadir), with a "
            f"perfectly orthographic lens and ABSOLUTELY ZERO perspective, tilt, angle or side "
            f"view. Because you look straight down, the entire HEIGHT collapses to a point: draw "
            f"it as flat CONCENTRIC shapes — the top part (lampshade / tray / ring) as a filled "
            f"disc or ring in the centre, and the base as a larger concentric ring or footprint "
            f"beneath it; the upright pole is only a small dot at the exact centre. Think of the "
            f"round footprint the stand leaves on the floor, traced from directly overhead. "
            f"Preserve the product's exact real colors and materials. Show exactly one isolated "
            f"{cat}, centered, on a pure flat white background (#FFFFFF). Clean crisp edges, "
            f"professional flat 2D architectural floor-plan icon." + _AVOID_TOPDOWN +
            " ABSOLUTELY NO vertical pole/stem/rod drawn as a line, NO lampshade or stand seen "
            "from the side, NO standing/upright silhouette, NO visible height."
        )
    # DIRECTIONAL FLOOR furniture (sofas/chairs/beds): strictly top-down WITH a canonical
    # orientation. The model's usual failure is retaining the photo's angled view, so we
    # (1) define the nadir camera, (2) spell out that only flat top surfaces may be visible
    # and the backrest/headboard is just a flat band along the TOP, and (3) append the
    # avoid-list. Canonical orientation: solid back along the TOP, open/usable side BOTTOM.
    if slug in REGEN_CATEGORIES:
        return (
            f"Transform this product photo into a single {cat}, redrawn as a strictly TOP-DOWN "
            f"(bird's-eye / architectural plan) 2D floor-plan icon — exactly the view a camera "
            f"mounted on the ceiling pointing straight down (nadir) would capture, with a "
            f"perfectly orthographic lens and ZERO perspective, tilt or angle. You must see "
            f"ONLY the flat horizontal top surfaces: the seat cushions, mattress and armrests "
            f"read as flat shapes, and the backrest or headboard appears ONLY as a flat band "
            f"running along the TOP edge of the frame — never its front face, never its height "
            f"or thickness, never any vertical side. Imagine the object flattened onto the floor "
            f"and traced from directly overhead. Orient it so the solid back (backrest or "
            f"headboard) runs along the TOP edge and the open, usable side (the seat, or the "
            f"foot of the bed) faces the BOTTOM. Preserve the product's exact real colors, "
            f"materials, and proportions. Show exactly one isolated {cat}, centered, on a pure "
            f"flat white background (#FFFFFF). Clean crisp edges, professional flat 2D "
            f"architectural floor-plan furniture symbol." + _AVOID_TOPDOWN
        )
    # side-table SETS: when the product NAME says it's a set ("set of two side tables"), render
    # the COMPLETE set (usually two matching tables), not a single one. Only side-table.
    if slug == "side-table" and _is_side_table_set(name):
        avoid_set = _AVOID_TOPDOWN.replace(", other objects", "")   # the 2nd table isn't "other"
        return (
            f"Transform this product photo into the COMPLETE matching SET of side tables shown "
            f"(render ALL the tables in the set — usually two — arranged together side by side "
            f"exactly as a nesting/pair set), redrawn as a strictly TOP-DOWN (bird's-eye / "
            f"architectural plan) 2D floor-plan icon — exactly the view a camera mounted on the "
            f"ceiling pointing straight down (nadir) would capture, with a perfectly orthographic "
            f"lens and ZERO perspective, tilt or angle. Show ONLY the flat top surfaces seen from "
            f"directly overhead — no visible front face, no side, no height, thickness or depth. "
            f"Render the WHOLE set; do NOT reduce it to a single table. Preserve the products' "
            f"exact real colors, materials, proportions and design. Center the set on a pure flat "
            f"white background (#FFFFFF). Clean crisp edges, professional flat 2D architectural "
            f"floor-plan icon." + avoid_set
        )
    # OTHER FLOOR items (tables, rugs, storage, wardrobes, decor, appliances, dishware, etc.):
    # strictly top-down too, but with NO furniture-specific backrest/seat language and NO
    # forced TOP/BOTTOM orientation — a table or rug has no canonical front. Just its flat
    # footprint seen from straight above.
    # For TABLE_ONLY categories the source photo is usually a full set, so we hard-strip the
    # seating: the icon must be the table alone.
    table_only = (
        (f" Render ONLY the {cat} itself — a single table, its top surface and legs. Completely "
         f"REMOVE and OMIT any chairs, stools, benches, cushions or seating of any kind, even if "
         f"the source photo shows a full dining set; the icon must contain the bare table alone.")
        if slug in TABLE_ONLY_CATEGORIES else ""
    )
    avoid = _AVOID_TOPDOWN
    if slug in TABLE_ONLY_CATEGORIES:
        avoid = avoid[:-1] + ", chairs, stools, benches, seating."   # extend the avoid-list
    return (
        f"Transform this product photo into a single {cat}, redrawn as a strictly TOP-DOWN "
        f"(bird's-eye / architectural plan) 2D floor-plan icon — exactly the view a camera "
        f"mounted on the ceiling pointing straight down (nadir) would capture, with a "
        f"perfectly orthographic lens and ZERO perspective, tilt or angle. Show ONLY the flat "
        f"footprint of the {cat} as seen from directly overhead — its top surface flattened "
        f"into the plane, with no visible front face, no visible side, and no height, "
        f"thickness or depth. Preserve the product's exact real colors, materials, "
        f"proportions and design. Show exactly one isolated {cat}, centered, on a pure flat "
        f"white background (#FFFFFF). Clean crisp edges, professional flat 2D architectural "
        f"floor-plan icon." + table_only + avoid
    )
