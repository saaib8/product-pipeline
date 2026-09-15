"""The review API and the guarantees the state machine rests on.

The two that matter most: a status change and its audit row are atomic, and approving
a category is what makes downstream stages eligible — nothing else does.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from pipeline.enums import ArtifactType, JobStatus, ReviewStatus
from pipeline.models import Product, ReviewEvent

pytestmark = pytest.mark.django_db


# ── queues ──────────────────────────────────────────────────────────────────────


def test_new_product_appears_in_both_queues(api, product):
    """Imported products are PENDING on every flag, so both reviewers see them."""
    cat = api.get(reverse("pipeline:category-queue")).json()
    dim = api.get(reverse("pipeline:dimension-queue")).json()
    assert [p["id"] for p in cat["results"]] == [product.id]
    assert [p["id"] for p in dim["results"]] == [product.id]


def test_queues_are_independent(api, product):
    """Approving a category must not remove the product from the dimensions queue."""
    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": ReviewStatus.APPROVED}, format="json")

    cat = api.get(reverse("pipeline:category-queue")).json()
    dim = api.get(reverse("pipeline:dimension-queue")).json()
    assert cat["results"] == []
    assert [p["id"] for p in dim["results"]] == [product.id]


def test_queue_exposes_the_fields_a_reviewer_needs(api, product):
    row = api.get(reverse("pipeline:category-queue")).json()["results"][0]
    for field in ("image_url", "product_url", "is_active", "category", "flags"):
        assert field in row
    assert row["flags"]["detection"] is None          # ingestion has not run
    assert row["flags"]["wants_icon"] is True


def test_dimension_queue_adds_the_measurements(api, product):
    row = api.get(reverse("pipeline:dimension-queue")).json()["results"][0]
    assert row["length"] == "220.00"
    assert row["width"] == "95.00"
    assert row["height"] == "85.00"


def test_store_filter(api, product, store):
    url = reverse("pipeline:category-queue")
    assert api.get(url, {"store": store.id}).json()["count"] == 1
    assert api.get(url, {"store": store.id + 999}).json()["count"] == 0


# ── category decisions ──────────────────────────────────────────────────────────


def test_approving_category_makes_stages_eligible(api, product):
    """This is the trigger. Before: nothing is eligible. After: three stages are."""
    eligible = dict(category_status=ReviewStatus.APPROVED, ingestion_status=JobStatus.PENDING)
    assert not Product.objects.filter(pk=product.pk, **eligible).exists()

    res = api.post(reverse("pipeline:category-decide", args=[product.id]),
                   {"decision": ReviewStatus.APPROVED}, format="json")
    assert res.status_code == 200
    assert Product.objects.filter(pk=product.pk, **eligible).exists()


def test_approving_category_does_not_touch_dimensions(api, product):
    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": ReviewStatus.APPROVED}, format="json")
    product.refresh_from_db()
    assert product.dimensions_status == ReviewStatus.PENDING


def test_reviewer_can_correct_the_category_while_approving(api, product):
    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": ReviewStatus.APPROVED, "category": "2-Seater-Sofa"}, format="json")
    product.refresh_from_db()
    assert product.category == "2-seater-sofa"          # stored canonically
    assert product.category_status == ReviewStatus.APPROVED


def test_correction_records_the_previous_value(api, product):
    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": ReviewStatus.APPROVED, "category": "chair"}, format="json")
    event = ReviewEvent.objects.get(product=product, artifact_type=ArtifactType.CATEGORY)
    assert event.previous_value == {"category": "3-seater-sofa"}


def test_unknown_category_is_refused(api, product):
    res = api.post(reverse("pipeline:category-decide", args=[product.id]),
                   {"decision": ReviewStatus.APPROVED, "category": "garden gnome"},
                   format="json")
    assert res.status_code == 400
    product.refresh_from_db()
    assert product.category_status == ReviewStatus.PENDING


def test_rejection_is_audited_with_the_reviewer_and_note(api, product, reviewer):
    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": ReviewStatus.REJECTED, "note": "clearly a lamp"}, format="json")
    event = ReviewEvent.objects.get(product=product)
    assert event.decision == ReviewStatus.REJECTED
    assert event.note == "clearly a lamp"
    assert event.reviewer == reviewer
    product.refresh_from_db()
    assert product.category_status == ReviewStatus.REJECTED


# ── dimension decisions ─────────────────────────────────────────────────────────


def test_approving_dimensions_triggers_nothing(api, product):
    """Dimensions gate the layout feature at READ time. No stage keys off them."""
    before = Product.objects.values(
        "ingestion_status", "icon_2d_status", "model_3d_status"
    ).get(pk=product.pk)

    api.post(reverse("pipeline:dimension-decide", args=[product.id]),
             {"decision": ReviewStatus.APPROVED}, format="json")

    after = Product.objects.values(
        "ingestion_status", "icon_2d_status", "model_3d_status"
    ).get(pk=product.pk)
    assert before == after


def test_reviewer_can_correct_dimensions(api, product):
    api.post(reverse("pipeline:dimension-decide", args=[product.id]),
             {"decision": ReviewStatus.APPROVED, "length": "210.5", "width": "92"},
             format="json")
    product.refresh_from_db()
    assert str(product.length) == "210.50"
    assert str(product.width) == "92.00"
    assert product.dimensions_status == ReviewStatus.APPROVED


def test_cannot_approve_dimensions_without_length_and_width(api, store):
    """A product with no measurements can never be placed, so approving it would
    create a permanently unusable row."""
    bare = Product.objects.create(
        store=store, name_english="No dims", name_arabic="—",
        image_url="https://example.invalid/x.jpg", product_url="https://example.invalid/x",
        category="chair", price_amount=10, price_unit="SAR",
    )
    res = api.post(reverse("pipeline:dimension-decide", args=[bare.id]),
                   {"decision": ReviewStatus.APPROVED}, format="json")
    assert res.status_code == 400
    bare.refresh_from_db()
    assert bare.dimensions_status == ReviewStatus.PENDING


def test_negative_dimensions_are_refused(api, product):
    res = api.post(reverse("pipeline:dimension-decide", args=[product.id]),
                   {"decision": ReviewStatus.APPROVED, "length": "-5"}, format="json")
    assert res.status_code == 400


# ── audit + counts ──────────────────────────────────────────────────────────────


def test_every_decision_writes_exactly_one_event(api, product):
    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": ReviewStatus.APPROVED}, format="json")
    api.post(reverse("pipeline:dimension-decide", args=[product.id]),
             {"decision": ReviewStatus.APPROVED}, format="json")
    assert ReviewEvent.objects.filter(product=product).count() == 2
    assert set(ReviewEvent.objects.values_list("artifact_type", flat=True)) == {
        ArtifactType.CATEGORY, ArtifactType.DIMENSIONS,
    }


def test_counts_track_the_queues(api, product):
    counts = api.get(reverse("pipeline:queue-counts")).json()
    assert counts == {"category": 1, "dimensions": 1, "icon_2d": 0}

    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": ReviewStatus.APPROVED}, format="json")
    assert api.get(reverse("pipeline:queue-counts")).json()["category"] == 0


def test_anonymous_access_is_refused(client, product):
    res = client.get(reverse("pipeline:category-queue"))
    assert res.status_code in (401, 403)


def test_cannot_approve_an_unknown_category_as_is(api, store):
    """An imported row can carry a category the detector doesn't know. Approving it
    unchanged would leave it APPROVED forever while every detection fails to match."""
    odd = Product.objects.create(
        store=store, name_english="Mystery Item", name_arabic="—",
        image_url="https://example.invalid/m.jpg", product_url="https://example.invalid/m",
        category="garden gnome", price_amount=10, price_unit="SAR",
    )
    res = api.post(reverse("pipeline:category-decide", args=[odd.id]),
                   {"decision": ReviewStatus.APPROVED}, format="json")
    assert res.status_code == 400
    odd.refresh_from_db()
    assert odd.category_status == ReviewStatus.PENDING

    # ...but correcting it in the same action is fine.
    ok = api.post(reverse("pipeline:category-decide", args=[odd.id]),
                  {"decision": ReviewStatus.APPROVED, "category": "statue-and-antique"},
                  format="json")
    assert ok.status_code == 200
    odd.refresh_from_db()
    assert odd.category == "statue-and-antique"
    assert odd.category_status == ReviewStatus.APPROVED
