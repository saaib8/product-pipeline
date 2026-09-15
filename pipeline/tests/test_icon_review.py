"""The icon review queue and its one destructive decision.

Rejecting an icon is the only reviewer action in the system that retires a product, so
most of what's here pins down that blast radius: which column moves, what else moves
with it, and whether the change can be undone from the audit trail alone.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from pipeline.enums import ArtifactType, ReviewStatus
from pipeline.models import Product, ReviewEvent

pytestmark = pytest.mark.django_db

QUEUE = "pipeline:icon-queue"
DECIDE = "pipeline:icon-decide"


@pytest.fixture(autouse=True)
def public_bucket(settings):
    """Pin the URL scheme so `icon_url` is deterministic and never signs against S3."""
    settings.S3_PUBLIC_BASE_URL = "https://icons.example.invalid"
    return settings


@pytest.fixture
def waiting(product) -> Product:
    """A product whose icon has been generated and is awaiting a human."""
    Product.objects.filter(pk=product.pk).update(
        category_status=ReviewStatus.APPROVED,
        icon_2d_status=ReviewStatus.IN_REVIEW,
        two_d_icon="2D_icons/demostore/1.svg",
    )
    product.refresh_from_db()
    return product


# ── what the queue shows ────────────────────────────────────────────────────────


def test_only_generated_icons_appear(api, product):
    """PENDING means the worker has not drawn it yet — there is nothing to look at.

    This is the one queue that does NOT use (PENDING, IN_REVIEW): including PENDING
    would fill the tab with rows whose only honest action is to wait.
    """
    assert api.get(reverse(QUEUE)).json()["count"] == 0     # PENDING -> absent

    Product.objects.filter(pk=product.pk).update(
        icon_2d_status=ReviewStatus.IN_REVIEW, two_d_icon="k.svg")
    assert api.get(reverse(QUEUE)).json()["count"] == 1


def test_decided_icons_leave_the_queue(api, waiting):
    for done in (ReviewStatus.APPROVED, ReviewStatus.REJECTED):
        Product.objects.filter(pk=waiting.pk).update(icon_2d_status=done)
        assert api.get(reverse(QUEUE)).json()["count"] == 0


def test_row_carries_both_images_and_the_key(api, waiting):
    """Judging an icon is a comparison, so the source photo must travel with it."""
    row = api.get(reverse(QUEUE)).json()["results"][0]
    assert row["image_url"] == waiting.image_url
    assert row["icon_url"] == "https://icons.example.invalid/2D_icons/demostore/1.svg"
    assert row["two_d_icon"] == "2D_icons/demostore/1.svg"


def test_a_broken_storage_url_does_not_empty_the_queue(api, waiting, monkeypatch):
    """One unresolvable key must not take the whole tab down with it."""
    import pipeline.serializers as serializers

    monkeypatch.setattr(serializers, "public_url",
                        lambda key: (_ for _ in ()).throw(RuntimeError("s3 down")))
    body = api.get(reverse(QUEUE)).json()
    assert body["count"] == 1                 # still listed
    assert body["results"][0]["icon_url"] == ""       # ...flagged as unloadable


def test_store_filter(api, waiting, store):
    url = reverse(QUEUE)
    assert api.get(url, {"store": store.id}).json()["count"] == 1
    assert api.get(url, {"store": store.id + 999}).json()["count"] == 0


# ── approve ─────────────────────────────────────────────────────────────────────


def test_approve_marks_the_icon_and_leaves_the_product_alone(api, waiting):
    api.post(reverse(DECIDE, args=[waiting.id]),
             {"decision": ReviewStatus.APPROVED}, format="json")

    waiting.refresh_from_db()
    assert waiting.icon_2d_status == ReviewStatus.APPROVED
    assert waiting.is_active is True                  # approval is not destructive
    assert waiting.two_d_icon == "2D_icons/demostore/1.svg"


def test_cannot_approve_a_product_with_no_icon(api, product):
    """Otherwise the row reads icon-complete while nothing exists at the key."""
    Product.objects.filter(pk=product.pk).update(
        icon_2d_status=ReviewStatus.IN_REVIEW, two_d_icon="")

    response = api.post(reverse(DECIDE, args=[product.id]),
                        {"decision": ReviewStatus.APPROVED}, format="json")

    assert response.status_code == 400
    product.refresh_from_db()
    assert product.icon_2d_status == ReviewStatus.IN_REVIEW      # unchanged


# ── reject: scoped to the icon ──────────────────────────────────────────────────


def test_reject_marks_the_icon_and_leaves_the_product_active(api, waiting):
    """A bad icon says the drawing is wrong, not that the product is. Rejection used to
    set `is_active=False` and retire the row everywhere; that blast radius was wrong."""
    api.post(reverse(DECIDE, args=[waiting.id]),
             {"decision": ReviewStatus.REJECTED}, format="json")

    waiting.refresh_from_db()
    assert waiting.icon_2d_status == ReviewStatus.REJECTED
    assert waiting.is_active is True


def test_reject_touches_no_other_column(api, waiting):
    """The whole point of scoping it: everything except the icon column is untouched."""
    before = {f: getattr(waiting, f) for f in
              ("is_active", "category_status", "dimensions_status",
               "ingestion_status", "metadata_status", "two_d_icon")}

    api.post(reverse(DECIDE, args=[waiting.id]),
             {"decision": ReviewStatus.REJECTED}, format="json")

    waiting.refresh_from_db()
    assert {f: getattr(waiting, f) for f in before} == before


def test_reject_is_audited(api, waiting):
    """No `changes` ride along any more, so `previous_value` is empty — but the
    decision, the note and the reviewer still have to be on the record."""
    api.post(reverse(DECIDE, args=[waiting.id]),
             {"decision": ReviewStatus.REJECTED, "note": "wrong object"}, format="json")

    event = ReviewEvent.objects.get(product=waiting, artifact_type=ArtifactType.ICON_2D)
    assert event.decision == ReviewStatus.REJECTED
    assert event.previous_value is None
    assert event.note == "wrong object"
    assert event.reviewer is not None


def test_rejection_leaves_every_other_queue_alone(api, waiting):
    """It leaves the ICON queue because that one filters on IN_REVIEW. The others are
    none of this decision's business — a reviewer can still measure the product."""
    assert api.get(reverse("pipeline:category-queue")).json()["count"] == 0   # approved
    assert api.get(reverse("pipeline:dimension-queue")).json()["count"] == 1

    api.post(reverse(DECIDE, args=[waiting.id]),
             {"decision": ReviewStatus.REJECTED}, format="json")

    assert api.get(reverse("pipeline:dimension-queue")).json()["count"] == 1
    assert api.get(reverse(QUEUE)).json()["count"] == 0


def test_rejection_costs_only_layout_readiness(api, waiting):
    """The single consequence, and it is read-time: no approved icon, no placement.
    Listing and recommendation are unaffected."""
    api.post(reverse(DECIDE, args=[waiting.id]),
             {"decision": ReviewStatus.REJECTED}, format="json")

    waiting.refresh_from_db()
    assert waiting.is_layout_ready is False
    assert waiting.is_listing_ready is True


def test_rejection_does_not_delete_the_icon(api, waiting):
    """The object stays in S3: the decision is still auditable, and the key is what a
    support request will quote."""
    api.post(reverse(DECIDE, args=[waiting.id]),
             {"decision": ReviewStatus.REJECTED}, format="json")

    waiting.refresh_from_db()
    assert waiting.two_d_icon == "2D_icons/demostore/1.svg"


# ── plumbing ────────────────────────────────────────────────────────────────────


def test_unknown_product_is_404(api):
    assert api.post(reverse(DECIDE, args=[999999]),
                    {"decision": ReviewStatus.APPROVED}, format="json").status_code == 404


def test_a_bad_decision_is_rejected(api, waiting):
    response = api.post(reverse(DECIDE, args=[waiting.id]),
                        {"decision": "MAYBE"}, format="json")
    assert response.status_code == 400
    waiting.refresh_from_db()
    assert waiting.icon_2d_status == ReviewStatus.IN_REVIEW


def test_decisions_require_authentication(client, waiting):
    response = client.post(reverse(DECIDE, args=[waiting.id]),
                           {"decision": ReviewStatus.APPROVED},
                           content_type="application/json")
    assert response.status_code in (401, 403)
    waiting.refresh_from_db()
    assert waiting.icon_2d_status == ReviewStatus.IN_REVIEW


def test_counts_track_the_icon_queue(api, waiting):
    assert api.get(reverse("pipeline:queue-counts")).json()["icon_2d"] == 1

    api.post(reverse(DECIDE, args=[waiting.id]),
             {"decision": ReviewStatus.APPROVED}, format="json")

    assert api.get(reverse("pipeline:queue-counts")).json()["icon_2d"] == 0


# ── NOT_APPLICABLE: keeping PENDING honest ──────────────────────────────────────


def test_approving_an_out_of_scope_category_marks_it_not_applicable(api, product):
    """`treadmill` has no floor-plan icon. Leaving it PENDING meant "how many icons are
    outstanding?" counted gym equipment forever."""
    Product.objects.filter(pk=product.pk).update(category="treadmill")

    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": ReviewStatus.APPROVED, "category": "treadmill"}, format="json")

    product.refresh_from_db()
    assert product.category_status == ReviewStatus.APPROVED
    assert product.icon_2d_status == ReviewStatus.NOT_APPLICABLE


def test_an_in_scope_category_stays_pending(api, product):
    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": ReviewStatus.APPROVED}, format="json")

    product.refresh_from_db()
    assert product.icon_2d_status == ReviewStatus.PENDING       # queued, will happen


def test_a_correction_decides_applicability(api, product):
    """The FINAL category is what counts — correcting on the way through must flip it."""
    Product.objects.filter(pk=product.pk).update(category="treadmill")

    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": ReviewStatus.APPROVED, "category": "chair"}, format="json")

    product.refresh_from_db()
    assert product.category == "chair"
    assert product.icon_2d_status == ReviewStatus.PENDING       # now in scope


def test_not_applicable_is_never_claimed(api, product):
    from pipeline.stages.base import claim_one
    from pipeline.stages.icon_2d import ICON_2D_STAGE

    Product.objects.filter(pk=product.pk).update(category="treadmill")
    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": ReviewStatus.APPROVED, "category": "treadmill"}, format="json")

    assert claim_one(ICON_2D_STAGE) is None


def test_a_started_icon_is_never_retired_by_a_category_decision(api, waiting):
    """Once an icon exists it belongs to a reviewer. A later category approval must not
    quietly mark it not-applicable and hide it."""
    Product.objects.filter(pk=waiting.pk).update(category_status=ReviewStatus.PENDING)

    api.post(reverse("pipeline:category-decide", args=[waiting.id]),
             {"decision": ReviewStatus.APPROVED}, format="json")

    waiting.refresh_from_db()
    assert waiting.icon_2d_status == ReviewStatus.IN_REVIEW      # untouched


def test_pending_now_means_exactly_one_thing(api, product):
    """The point of the whole change: a PENDING icon is queued work, nothing else."""
    from pipeline.stages.icon_2d import ICON_2D_STAGE

    Product.objects.filter(pk=product.pk).update(category="treadmill")
    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": ReviewStatus.APPROVED, "category": "treadmill"}, format="json")

    pending = Product.objects.filter(icon_2d_status=ReviewStatus.PENDING)
    eligible = Product.objects.filter(ICON_2D_STAGE.eligible)
    # Every PENDING row that is active and approved is genuinely queued.
    assert pending.filter(is_active=True,
                          category_status=ReviewStatus.APPROVED).count() == eligible.count()
