"""The category vocabulary is the pipeline's gate — every stage keys off it, and the
detector compares its label against it by string equality. These tests pin the exact
failure that silently dropped six categories in the source system.
"""

from __future__ import annotations

from pipeline.categories import (
    CATEGORIES,
    CATEGORY_SET,
    ICON_CATEGORIES,
    matches,
    norm_category,
    resolve,
    wants_icon,
)


def test_vocabulary_is_unique_after_normalisation():
    """Two classes must never collapse to the same lookup key."""
    keys = [norm_category(c) for c in CATEGORIES]
    assert len(keys) == len(set(keys))


def test_spaced_classes_are_stored_verbatim():
    """The model was annotated with spaces for these. We must not invent a hyphen —
    a hyphen we made up is a class the detector has never heard of."""
    assert "leg press machine" in CATEGORY_SET
    assert "leg-press-machine" not in CATEGORY_SET


def test_any_spelling_resolves_to_the_canonical_class():
    for spelling in ("leg press machine", "Leg Press Machine", "leg-press-machine",
                     "  LEG_PRESS_MACHINE  "):
        assert resolve(spelling) == "leg press machine"


def test_hyphenated_classes_resolve_to_themselves():
    assert resolve("3-Seater-Sofa") == "3-seater-sofa"
    assert resolve("service table") == "service-table"


def test_unknown_category_resolves_to_none():
    assert resolve("garden gnome") is None
    assert resolve("") is None
    assert resolve(None) is None


def test_detector_label_matches_across_separator_styles():
    """The regression that mattered: the detector emits a spaced label while the row
    holds the same class, and exact string equality silently fails."""
    assert matches("leg press machine", "leg press machine")
    assert matches("Leg Press Machine", "leg press machine")
    assert matches("leg-press-machine", "leg press machine")
    assert not matches("treadmill", "leg press machine")
    assert not matches("", "sofa")
    assert not matches(None, "sofa")


def test_icon_scope_is_a_subset_of_the_vocabulary():
    assert ICON_CATEGORIES <= CATEGORY_SET
    assert len(ICON_CATEGORIES) == 44


def test_icon_scope_membership():
    assert wants_icon("3-seater-sofa")
    assert wants_icon("art-canvas")          # wall item, front-elevation prompt
    assert not wants_icon("treadmill")       # known class, but no icon is generated
    assert not wants_icon("garden gnome")
