"""Two reviewers on the same product.

The failure this protects against is not theoretical: before the `expect` guard, a
reviewer whose page was minutes old could reject a product a colleague had already
approved, get HTTP 200, and — for icons — deactivate it. Ingestion had meanwhile fanned
out from the first approval, so the row read REJECTED while its vector sat in Pinecone.

The guard is checked *inside* the row lock, so it is a real serialisation point rather
than a read-then-write race of its own.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from pipeline.enums import ArtifactType, JobStatus, ReviewStatus
from pipeline.models import Product, ReviewEvent
from pipeline.transitions import StaleDecision, transition

pytestmark = pytest.mark.django_db

APPROVED = ReviewStatus.APPROVED
REJECTED = ReviewStatus.REJECTED


# ── category ────────────────────────────────────────────────────────────────────


def test_second_decision_is_refused(api, product):
    first = api.post(reverse("pipeline:category-decide", args=[product.id]),
                     {"decision": APPROVED}, format="json")
    second = api.post(reverse("pipeline:category-decide", args=[product.id]),
                      {"decision": REJECTED}, format="json")

    assert first.status_code == 200
    assert second.status_code == 409

    product.refresh_from_db()
    assert product.category_status == APPROVED          # the winner stands


def test_the_conflict_says_what_the_current_value_is(api, product):
    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": APPROVED}, format="json")
    body = api.post(reverse("pipeline:category-decide", args=[product.id]),
                    {"decision": REJECTED}, format="json").json()

    assert body["field"] == "category_status"
    assert body["current"] == APPROVED                  # UI can refresh in place


def test_a_refused_decision_writes_no_audit_row(api, product):
    """A losing decision must leave no trace, or the history implies it happened."""
    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": APPROVED}, format="json")
    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": REJECTED}, format="json")

    events = ReviewEvent.objects.filter(product=product,
                                        artifact_type=ArtifactType.CATEGORY)
    assert [e.decision for e in events] == [APPROVED]


def test_a_refused_correction_is_not_applied(api, product):
    """The loser's *correction* must not land either — not just its status."""
    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": APPROVED}, format="json")
    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": APPROVED, "category": "chair"}, format="json")

    product.refresh_from_db()
    assert product.category == "3-seater-sofa"          # unchanged


# ── dimensions ──────────────────────────────────────────────────────────────────


def test_dimensions_are_guarded_too(api, product):
    api.post(reverse("pipeline:dimension-decide", args=[product.id]),
             {"decision": APPROVED}, format="json")
    second = api.post(reverse("pipeline:dimension-decide", args=[product.id]),
                      {"decision": REJECTED}, format="json")

    assert second.status_code == 409
    product.refresh_from_db()
    assert product.dimensions_status == APPROVED


# ── icons: the destructive one ──────────────────────────────────────────────────


@pytest.fixture
def waiting(product) -> Product:
    Product.objects.filter(pk=product.pk).update(
        category_status=APPROVED,
        icon_2d_status=ReviewStatus.IN_REVIEW,
        two_d_icon="2D_icons/demostore/1.svg")
    product.refresh_from_db()
    return product


def test_a_stale_reject_cannot_deactivate_an_approved_icon(api, waiting):
    """The worst case: reviewer B retires a product reviewer A just approved."""
    api.post(reverse("pipeline:icon-decide", args=[waiting.id]),
             {"decision": APPROVED}, format="json")
    second = api.post(reverse("pipeline:icon-decide", args=[waiting.id]),
                      {"decision": REJECTED}, format="json")

    assert second.status_code == 409
    waiting.refresh_from_db()
    assert waiting.icon_2d_status == APPROVED
    assert waiting.is_active is True                    # NOT deactivated


def test_an_undrawn_icon_reports_the_specific_reason(api, product):
    """No icon at all: the precondition check answers first, and its message is more
    useful to a caller than a bare state conflict."""
    Product.objects.filter(pk=product.pk).update(category_status=APPROVED)

    response = api.post(reverse("pipeline:icon-decide", args=[product.id]),
                        {"decision": APPROVED}, format="json")

    assert response.status_code == 400
    assert "no generated icon" in response.json()["detail"]
    product.refresh_from_db()
    assert product.icon_2d_status == ReviewStatus.PENDING


def test_a_reserved_key_is_still_not_decidable(api, product):
    """The gap the precondition misses: the stage reserves the key BEFORE generating,
    so a row can hold a key while still PENDING. Only IN_REVIEW may be decided, and the
    `expect` guard is what enforces that."""
    Product.objects.filter(pk=product.pk).update(
        category_status=APPROVED,
        icon_2d_status=ReviewStatus.PENDING,
        two_d_icon="2D_icons/demostore/1.svg")      # reserved, not yet drawn

    response = api.post(reverse("pipeline:icon-decide", args=[product.id]),
                        {"decision": APPROVED}, format="json")

    assert response.status_code == 409
    product.refresh_from_db()
    assert product.icon_2d_status == ReviewStatus.PENDING


# ── the mechanism itself ────────────────────────────────────────────────────────


def test_expect_is_evaluated_against_the_stored_row(product):
    """Not against the caller's in-memory copy, which is exactly what goes stale."""
    Product.objects.filter(pk=product.pk).update(category_status=APPROVED)
    # `product` still says PENDING in memory — the guard must not believe it.
    with pytest.raises(StaleDecision):
        transition(product, ArtifactType.CATEGORY, REJECTED,
                   expect=(ReviewStatus.PENDING, ReviewStatus.IN_REVIEW))


def test_stages_are_unaffected(product):
    """Stages claim with SKIP LOCKED and pass no `expect`; adding the guard must not
    change their behaviour."""
    updated = transition(product, ArtifactType.INGESTION, JobStatus.COMPLETED,
                         status_field="ingestion_status")
    assert updated.ingestion_status == JobStatus.COMPLETED


def test_previous_value_is_read_from_the_locked_row(api, product):
    """`changes` records the prior value for reversibility — it must come from the row
    as locked, not from a caller copy that may already be out of date.

    Uses CATEGORY rejection: it is now the decision that carries `is_active` in
    `changes`. Icon rejection used to, and no longer does — it is scoped to the icon.
    """
    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": REJECTED}, format="json")

    event = ReviewEvent.objects.get(product=product, artifact_type=ArtifactType.CATEGORY)
    assert event.previous_value == {"is_active": True}


# ── category rejection deactivates ──────────────────────────────────────────────


def test_rejecting_a_category_deactivates_the_product(api, product):
    """A category the reviewer will not accept and will not correct leaves nothing the
    pipeline can do: it can never be ingested (the namespace IS the category), never
    drawn, never placed."""
    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": REJECTED}, format="json")

    product.refresh_from_db()
    assert product.category_status == REJECTED
    assert product.is_active is False


def test_a_rejected_category_leaves_the_dimensions_queue(api, product):
    """Previously it stayed active, so a second reviewer could spend time measuring a
    product the first had already discarded."""
    assert api.get(reverse("pipeline:dimension-queue")).json()["count"] == 1

    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": REJECTED}, format="json")

    assert api.get(reverse("pipeline:dimension-queue")).json()["count"] == 0


def test_rejecting_a_category_is_reversible_from_the_audit_row(api, product):
    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": REJECTED, "note": "wrong product entirely"}, format="json")

    event = ReviewEvent.objects.get(product=product, artifact_type=ArtifactType.CATEGORY)
    assert event.previous_value == {"is_active": True}
    assert event.note == "wrong product entirely"


def test_approving_a_category_never_deactivates(api, product):
    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": APPROVED}, format="json")

    product.refresh_from_db()
    assert product.is_active is True


def test_a_rejected_product_is_claimed_by_no_stage(api, product):
    from pipeline.stages.base import claim_one
    from pipeline.stages.ingest import INGEST_STAGE

    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": REJECTED}, format="json")

    assert claim_one(INGEST_STAGE) is None
