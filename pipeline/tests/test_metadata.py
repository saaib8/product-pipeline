"""The metadata stage, and the validation that guards its three columns.

The vocabularies are the contract: 5,589 production rows were written against exactly
these styles and colours, so the tests below pin the canonicalisation rather than the
model. What matters is that nothing off-palette can ever reach the database, and that a
model saying nothing usable is treated as a retryable result rather than a crash.
"""

from __future__ import annotations

import pytest
from django.db.models import Q
from PIL import Image

from pipeline.clients.metadata import MetadataError
from pipeline.enums import ArtifactType, JobStatus, ReviewStatus
from pipeline.models import Product, ReviewEvent
from pipeline.services.metadata_prompt import is_incomplete, parse_metadata
from pipeline.stages import metadata as meta_module
from pipeline.stages.base import claim_one, run_stage
from pipeline.stages.metadata import METADATA_STAGE, TerminalMetadataError

pytestmark = pytest.mark.django_db(transaction=True)

APPROVED = ReviewStatus.APPROVED


# ── the vocabulary contract ─────────────────────────────────────────────────────


def test_off_palette_answers_are_dropped_not_stored():
    """The model can invent a colour; it must never reach the column."""
    out = parse_metadata('{"styles":["Brutalist"],"main_color":"Neon Pink",'
                         '"secondary_colors":["Chartreuse"]}')
    assert out == {"main_color": "", "secondary_colors": "", "styles": ""}


def test_matching_is_case_insensitive():
    """The model varies its casing run to run; the palette must not care."""
    for given in ("Beige", "beige", "BEIGE", "  bEiGe  "):
        assert parse_metadata('{"main_color":"%s"}' % given)["main_color"] == "Beige"
    for given in ("modern", "MODERN", "Modern"):
        assert parse_metadata('{"styles":["%s"]}' % given)["styles"] == "Modern"


def test_only_the_33_palette_colours_are_accepted():
    """No fuzzy matching, no nearest-neighbour: a colour outside the palette is dropped
    whatever its casing. `Silver` is the real-world case — the model answers it for
    chrome and steel products, and the palette has no cool metal, so the row is retried
    rather than silently mapped onto `Grey`."""
    from pipeline.metadata_vocab import ALL_COLORS

    assert len(ALL_COLORS) == 33
    for absent in ("Silver", "SILVER", "silver", "Chrome", "Turquoise", "Warm Neutral"):
        assert parse_metadata('{"main_color":"%s"}' % absent)["main_color"] == ""


def test_the_aliases_are_spelling_variants_not_substitutes():
    """`gray -> Grey` is fine: Grey IS in the palette. There is deliberately no
    `silver -> Grey`, which would be inventing a match for an absent colour."""
    from pipeline.metadata_vocab import _COLOR_CANON, ALL_COLORS

    for target in _COLOR_CANON.values():
        assert target in ALL_COLORS


def test_american_spellings_are_mapped():
    """Without these aliases every "gray" is silently discarded."""
    assert parse_metadata('{"main_color":"gray"}')["main_color"] == "Grey"
    assert parse_metadata('{"main_color":"light gray"}')["main_color"] == "Light Grey"
    assert parse_metadata('{"main_color":"dark grey"}')["main_color"] == "Charcoal"


def test_styles_are_capped_at_three():
    out = parse_metadata('{"styles":["Modern","Boho","Zen","Coastal","Islamic"]}')
    assert out["styles"] == "Modern, Boho, Zen"


def test_secondary_never_repeats_the_main_colour():
    """The prompt forbids it; the parser enforces it rather than trusting the model."""
    out = parse_metadata('{"main_color":"Grey","secondary_colors":["gray","Gold"]}')
    assert out["main_color"] == "Grey"
    assert out["secondary_colors"] == "Gold"


def test_secondary_is_capped_at_two():
    out = parse_metadata('{"main_color":"Black",'
                         '"secondary_colors":["Gold","Brass","Bronze"]}')
    assert out["secondary_colors"] == "Gold, Brass"


def test_json_is_found_inside_prose_or_a_fence():
    """The model is told "JSON and nothing else" and mostly complies."""
    out = parse_metadata('Sure! ```json {"main_color":"Beige"} ``` hope that helps')
    assert out["main_color"] == "Beige"


def test_unparseable_output_is_empty_not_an_exception():
    assert is_incomplete(parse_metadata("I cannot see an image."))
    assert is_incomplete(parse_metadata(""))


def test_both_main_color_and_styles_are_required():
    """Either being blank triggers a retry. `main_color` derives `main_family`, the only
    field the colour-family filter matches on; `styles` derives `style_tags`."""
    assert is_incomplete({"main_color": "", "styles": "Modern", "secondary_colors": ""})
    assert is_incomplete({"main_color": "Navy", "styles": "", "secondary_colors": "Gold"})
    assert not is_incomplete({"main_color": "Navy", "styles": "Modern",
                              "secondary_colors": ""})


def test_empty_secondary_colors_is_acceptable():
    """The prompt allows 0 accents, and many products genuinely have one colour —
    requiring an accent would be asking the model to invent one."""
    assert not is_incomplete({"main_color": "Oak", "styles": "Minimalist",
                              "secondary_colors": ""})


def test_a_family_name_answer_is_caught_as_incomplete():
    """The realistic partial: the model answers "Warm Neutral", canonicalisation drops
    it, styles survive. Previously stored COMPLETED with no main_family at all."""
    meta = parse_metadata('{"main_color":"Warm Neutral","styles":["Modern"]}')
    assert meta["main_color"] == ""
    assert meta["styles"] == "Modern"
    assert is_incomplete(meta)


def test_stored_as_comma_separated_text_not_json():
    """The production columns are plain text, not arrays — this is the storage format
    5,589 existing rows already use."""
    out = parse_metadata('{"styles":["Modern","Boho"],"main_color":"Oak",'
                         '"secondary_colors":["Sage"]}')
    assert out["styles"] == "Modern, Boho"
    assert isinstance(out["styles"], str)


# ── the prompt targets ONE object ───────────────────────────────────────────────


def test_the_prompt_names_the_category():
    """Merchant photos are often styled rooms. Naming the object removes the guess."""
    from pipeline.services.metadata_prompt import build_prompt

    pr = build_prompt("dining-table")
    assert "the dining table" in pr
    assert "describe ONLY that object" in pr


def test_the_category_is_used_verbatim_never_reinterpreted():
    """An earlier version rewrote slugs into "nicer" nouns and every rewrite asserted
    something the category never said — `floor-stand` became "floor lamp", and
    `flower-pot-and-plant` became "potted plant", which points the model at the foliage
    so it answers Sage for a terracotta pot."""
    from pipeline.services.metadata_prompt import build_prompt

    assert "flower pot and plant" in build_prompt("flower-pot-and-plant")
    assert "potted plant" not in build_prompt("flower-pot-and-plant")
    assert "floor stand" in build_prompt("floor-stand")
    assert "floor lamp" not in build_prompt("floor-stand")


def test_a_missing_category_falls_back_to_the_generic_wording():
    from pipeline.services.metadata_prompt import build_prompt

    pr = build_prompt(None)
    assert "Look ONLY at the product" in pr
    assert "the None" not in pr


def test_only_categories_shot_with_seating_get_the_set_clause():
    """A dining table is almost always shot with its chairs, an office table with its
    chair, a dressing table with its stool. A console or a tv-table is not — telling the
    model to ignore seating that is not in the photograph is an instruction about absent
    objects, the same fault as enumerating props."""
    from pipeline.services.metadata_prompt import build_prompt

    for shot_with_seating in ("dining-table", "office-table", "dressing-table"):
        assert "not the seating around it" in build_prompt(shot_with_seating)
    for shot_alone in ("console", "tv-table", "side-table", "carpet"):
        assert "not the seating around it" not in build_prompt(shot_alone)


def test_the_prompt_does_not_enumerate_props():
    """Listing "rugs, cushions, lamps, plants" introduces those words for the model to
    attend to, and reads as exhaustive — so a mirror or a painting may not register as a
    prop. "Describe the X and nothing else" covers everything without naming anything."""
    from pipeline.services.metadata_prompt import build_prompt

    pr = build_prompt("carpet")
    for prop in ("cushions", "rugs, cushions", "lamps, plants", "decor"):
        assert prop not in pr
    assert "and nothing else" in pr


def test_the_palette_is_flat_with_no_family_names():
    """Grouping put 11 illegal nouns beside the 33 legal ones, then needed a rule
    forbidding them. Listing colours alone removes the ambiguity instead."""
    from pipeline.metadata_vocab import ALL_COLORS
    from pipeline.services.metadata_prompt import build_prompt

    pr = build_prompt("chair")
    assert all(c in pr for c in ALL_COLORS)
    for family in ("Warm Neutral", "Cool Neutral", "Jewel Tones", "Monochrome"):
        assert family not in pr
    assert "never the family name" not in pr


# ── the stage ───────────────────────────────────────────────────────────────────


class FakeImageIO:
    def __init__(self, error: Exception | None = None):
        self.error = error

    def read_from_url(self, url, *a, **k):
        if self.error:
            raise self.error
        return Image.new("RGB", (800, 800), "white")


@pytest.fixture
def fakes(monkeypatch):
    state = {"calls": 0, "result": {"main_color": "Beige",
                                    "secondary_colors": "Gold",
                                    "styles": "Modern, Boho"},
             "error": None}

    monkeypatch.setattr(meta_module, "_get_image_io", lambda: FakeImageIO())

    def fake_generate(image, category=None, **kw):
        state["calls"] += 1
        state["category"] = category          # so the wiring can be asserted
        if state["error"]:
            raise state["error"]
        return state["result"]

    monkeypatch.setattr(meta_module, "generate_metadata", fake_generate)
    return state


@pytest.fixture
def ready(product) -> Product:
    Product.objects.filter(pk=product.pk).update(
        category_status=APPROVED, dimensions_status=APPROVED)
    product.refresh_from_db()
    return product


def test_waits_for_both_reviews(product):
    """Metadata serves the layout engine, so it is only worth deriving for a product the
    engine could place — which needs an approved measurement as well as an approved
    category. Neither gate on its own releases the row."""
    assert claim_one(METADATA_STAGE) is None

    Product.objects.filter(pk=product.pk).update(category_status=APPROVED)
    assert claim_one(METADATA_STAGE) is None, "category alone must not release it"

    Product.objects.filter(pk=product.pk).update(
        category_status=ReviewStatus.PENDING, dimensions_status=APPROVED)
    assert claim_one(METADATA_STAGE) is None, "dimensions alone must not release it"

    Product.objects.filter(pk=product.pk).update(category_status=APPROVED)
    assert claim_one(METADATA_STAGE) is not None


def test_a_rejected_dimension_never_generates(ready):
    """Rejection no longer deactivates the product, so nothing else would stop this."""
    Product.objects.filter(pk=ready.pk).update(dimensions_status=ReviewStatus.REJECTED)
    assert claim_one(METADATA_STAGE) is None


def test_does_not_wait_for_an_icon(ready):
    """The notebook required an existing icon too. Metadata and icons still run
    independently of each other — they simply share the same two human gates."""
    Product.objects.filter(pk=ready.pk).update(
        length=None, width=None,
        icon_2d_status=ReviewStatus.PENDING, two_d_icon="")
    assert claim_one(METADATA_STAGE) is not None


def test_scoped_to_the_same_categories_as_icons(ready):
    """Metadata serves the layout engine, and the layout catalog drops any product
    without a `two_d_icon`. A treadmill never gets an icon, so it can never be placed
    and its colour would go unread — generating it would be spend with no consumer."""
    from pipeline.categories import ICON_CATEGORIES

    Product.objects.filter(pk=ready.pk).update(category="treadmill")
    assert "treadmill" not in ICON_CATEGORIES
    assert claim_one(METADATA_STAGE) is None

    Product.objects.filter(pk=ready.pk).update(category="3-seater-sofa")
    assert claim_one(METADATA_STAGE) is not None


def test_metadata_and_icon_scope_are_the_same_set():
    """They are deliberately identical: if one ever narrows, products would get an icon
    with no metadata or vice versa."""
    from pipeline.categories import ICON_CATEGORIES
    from pipeline.stages.icon_2d import ICON_2D_STAGE

    assert str(sorted(ICON_CATEGORIES)) in str(METADATA_STAGE.eligible)
    assert str(sorted(ICON_CATEGORIES)) in str(ICON_2D_STAGE.eligible)


def test_inactive_products_are_skipped(ready):
    Product.objects.filter(pk=ready.pk).update(is_active=False)
    assert claim_one(METADATA_STAGE) is None


def test_the_stage_passes_the_products_category_to_the_client(ready, fakes):
    """Without this the prompt says "the product" and the room-scene defence is lost."""
    run_stage(METADATA_STAGE, limit=1)
    assert fakes["category"] == ready.category


def test_success_writes_all_three_columns(ready, fakes):
    run_stage(METADATA_STAGE, limit=1)

    ready.refresh_from_db()
    assert ready.main_color == "Beige"
    assert ready.secondary_colors == "Gold"
    assert ready.styles == "Modern, Boho"
    assert ready.metadata_status == JobStatus.COMPLETED


def test_completion_is_machine_final_with_no_review(ready, fakes):
    """Metadata needs no sign-off — there is no IN_REVIEW state and no queue."""
    run_stage(METADATA_STAGE, limit=1)
    ready.refresh_from_db()
    assert ready.metadata_status == JobStatus.COMPLETED
    assert claim_one(METADATA_STAGE) is None


def test_an_empty_answer_is_retried_then_left_alone(ready, fakes):
    """The decided policy: an empty record gets two attempts."""
    fakes["result"] = {"main_color": "", "secondary_colors": "", "styles": ""}

    result = run_stage(METADATA_STAGE, limit=1)
    ready.refresh_from_db()
    assert ready.metadata_status == JobStatus.PENDING          # first attempt -> retry
    assert result.retried == 1

    run_stage(METADATA_STAGE, limit=1)
    ready.refresh_from_db()
    assert ready.metadata_status == JobStatus.FAILED           # second -> give up
    assert claim_one(METADATA_STAGE) is None


def test_the_reason_is_recorded(ready, fakes):
    fakes["result"] = {"main_color": "", "secondary_colors": "", "styles": ""}
    run_stage(METADATA_STAGE, limit=1)

    event = ReviewEvent.objects.filter(
        product=ready, artifact_type=ArtifactType.METADATA).first()
    assert "no usable" in event.note


def test_an_unusable_image_is_terminal(ready, fakes, monkeypatch):
    monkeypatch.setattr(meta_module, "_get_image_io",
                        lambda: FakeImageIO(error=ValueError("404 not found")))

    run_stage(METADATA_STAGE, limit=1)

    ready.refresh_from_db()
    assert ready.metadata_status == JobStatus.FAILED
    assert claim_one(METADATA_STAGE) is None                   # never comes back


def test_a_transport_failure_is_retried(ready, fakes):
    """Unlike an empty answer, the endpoint being down may well clear."""
    fakes["error"] = MetadataError("modal timed out")

    result = run_stage(METADATA_STAGE, limit=1)

    ready.refresh_from_db()
    assert ready.metadata_status == JobStatus.PENDING
    assert result.retried == 1


def test_nothing_is_written_when_the_model_fails(ready, fakes):
    fakes["error"] = MetadataError("boom")
    run_stage(METADATA_STAGE, limit=1)

    ready.refresh_from_db()
    assert ready.main_color in ("", None)
    assert ready.styles in ("", None)


def test_a_partial_answer_is_retried_not_stored(ready, fakes):
    """Changed deliberately. This previously stored a colour with no style as COMPLETE;
    the reverse case — styles with no colour — left a row that reads enriched while
    being invisible to the colour-family filter."""
    fakes["result"] = {"main_color": "Walnut", "secondary_colors": "", "styles": ""}

    result = run_stage(METADATA_STAGE, limit=1)

    ready.refresh_from_db()
    assert ready.metadata_status == JobStatus.PENDING       # retried, not accepted
    assert result.retried == 1
    assert ready.main_color in ("", None)                   # nothing half-written


def test_the_missing_field_is_named_in_the_audit(ready, fakes):
    fakes["result"] = {"main_color": "", "secondary_colors": "Gold", "styles": "Modern"}
    run_stage(METADATA_STAGE, limit=1)

    event = ReviewEvent.objects.filter(
        product=ready, artifact_type=ArtifactType.METADATA).first()
    assert "main_color" in event.note


def test_the_stage_is_threaded_like_ingestion(settings):
    assert METADATA_STAGE.concurrency == settings.METADATA_CONCURRENCY > 1


# ── OpenRouter transport ────────────────────────────────────────────────────────
#
# Hosted, so the call is a single synchronous request — the submit + poll machinery
# existed only to survive a 25-minute cold start on self-hosted Qwen2.5-VL-32B, which
# no longer applies. What matters here is that a provider failure is never mistaken for
# a model result, since an empty result is retried and a failure is not.


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests as rq
            raise rq.HTTPError(f"{self.status_code}")


def _answer(text):
    return FakeResponse(200, {"model": "qwen/qwen3-vl-8b-instruct",
                              "choices": [{"message": {"content": text}}],
                              "usage": {"prompt_tokens": 1400, "completion_tokens": 40}})


@pytest.fixture
def openrouter(monkeypatch, settings):
    settings.OPENROUTER_API_KEY = "test-key"
    settings.OPENROUTER_METADATA_MODEL = "qwen/qwen3-vl-8b-instruct"

    import pipeline.clients.metadata as mod
    state = {"response": _answer('{"main_color":"Beige"}'), "sent": None}

    def fake_post(url, json=None, headers=None, timeout=None):
        state["sent"] = {"url": url, "json": json, "headers": headers}
        r = state["response"]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(mod.requests, "post", fake_post)
    return state


def test_a_normal_answer_is_parsed(openrouter):
    from pipeline.clients.metadata import generate_metadata

    assert generate_metadata(b"jpeg")["main_color"] == "Beige"


def test_the_request_carries_the_prompt_and_the_image(openrouter):
    """The image is sent as bytes, not as its source URL: merchant CDNs 403 datacenter
    IPs, and by this point we have already downloaded it successfully."""
    from pipeline.clients.metadata import generate_metadata
    from pipeline.services.metadata_prompt import build_prompt

    generate_metadata(b"jpeg-bytes")
    content = openrouter["sent"]["json"]["messages"][0]["content"]

    assert content[0]["text"] == build_prompt(None)
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert openrouter["sent"]["json"]["model"] == "qwen/qwen3-vl-8b-instruct"


def test_generation_is_deterministic(openrouter):
    """Matches the notebook's do_sample=False — two runs over the same catalogue must
    not disagree with each other."""
    from pipeline.clients.metadata import generate_metadata

    generate_metadata(b"jpeg")
    assert openrouter["sent"]["json"]["temperature"] == 0


def test_a_provider_error_returned_with_http_200_is_raised(openrouter):
    """OpenRouter reports upstream failures in-band. Treating one as a model result
    would burn the row's retries on an empty answer that never came from the model."""
    from pipeline.clients.metadata import MetadataError, generate_metadata

    openrouter["response"] = FakeResponse(200, {"error": {"message": "upstream 503"}})
    with pytest.raises(MetadataError, match="upstream 503"):
        generate_metadata(b"jpeg")


def test_an_empty_choices_list_is_an_error_not_an_empty_result(openrouter):
    from pipeline.clients.metadata import MetadataError, generate_metadata

    openrouter["response"] = FakeResponse(200, {"choices": []})
    with pytest.raises(MetadataError, match="no choices"):
        generate_metadata(b"jpeg")


def test_an_http_failure_is_raised(openrouter):
    from pipeline.clients.metadata import MetadataError, generate_metadata

    openrouter["response"] = FakeResponse(429, {})
    with pytest.raises(MetadataError, match="request failed"):
        generate_metadata(b"jpeg")


def test_an_off_palette_answer_is_an_empty_RESULT_not_an_error(openrouter):
    """The distinction the stage depends on: this gets retried, a transport failure
    does not get confused with it."""
    from pipeline.clients.metadata import generate_metadata
    from pipeline.services.metadata_prompt import is_incomplete

    openrouter["response"] = _answer('{"main_color":"Neon Pink"}')
    assert is_incomplete(generate_metadata(b"jpeg"))


def test_the_api_key_is_required(monkeypatch, settings):
    from pipeline.clients.metadata import MetadataError, generate_metadata

    settings.OPENROUTER_API_KEY = ""
    with pytest.raises(MetadataError, match="not configured"):
        generate_metadata(b"jpeg")


# ── NOT_APPLICABLE: keeping PENDING honest for metadata too ─────────────────────


def test_approving_an_out_of_scope_category_marks_metadata_not_applicable(api, product):
    """`treadmill` gets no icon, so it can never be placed and its metadata would go
    unread. Marking it NOT_APPLICABLE keeps PENDING meaning "queued"."""
    from django.urls import reverse

    Product.objects.filter(pk=product.pk).update(category="treadmill")
    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": APPROVED, "category": "treadmill"}, format="json")

    product.refresh_from_db()
    assert product.metadata_status == JobStatus.NOT_APPLICABLE
    assert product.icon_2d_status == ReviewStatus.NOT_APPLICABLE   # both, one predicate


def test_an_in_scope_category_leaves_metadata_queued(api, product):
    """PENDING, not NOT_APPLICABLE — the row is genuinely queued. It is not yet
    CLAIMABLE, because the dimension review has still to happen; that is the gate,
    not the scope decision this test is about."""
    from django.urls import reverse

    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": APPROVED}, format="json")

    product.refresh_from_db()
    assert product.metadata_status == JobStatus.PENDING
    assert claim_one(METADATA_STAGE) is None

    api.post(reverse("pipeline:dimension-decide", args=[product.id]),
             {"decision": APPROVED}, format="json")
    assert claim_one(METADATA_STAGE) is not None


def test_a_correction_flips_both_stages_together(api, product):
    """One predicate decides both, so they can never disagree about scope."""
    from django.urls import reverse

    Product.objects.filter(pk=product.pk).update(category="treadmill")
    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": APPROVED, "category": "chair"}, format="json")

    product.refresh_from_db()
    assert product.metadata_status == JobStatus.PENDING
    assert product.icon_2d_status == ReviewStatus.PENDING


def test_not_applicable_is_never_claimed(api, product):
    from django.urls import reverse

    Product.objects.filter(pk=product.pk).update(category="treadmill")
    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": APPROVED, "category": "treadmill"}, format="json")

    assert claim_one(METADATA_STAGE) is None
