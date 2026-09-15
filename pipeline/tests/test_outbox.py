"""The transactional outbox.

The property under test is the one that justifies the table existing at all: the event
and the state change share a transaction, so there is no window in which the database
says APPROVED while nothing was ever told about it.

Everything else here defends the second rule — the event is a *nudge*, never an
instruction. A consumer that trusted the event instead of re-checking eligibility would
process products that have since been deactivated or already handled.
"""

from __future__ import annotations

import pytest
from django.db import transaction
from django.urls import reverse

from pipeline import outbox
from pipeline.enums import ArtifactType, JobStatus, ReviewStatus
from pipeline.models import OutboxEvent, Product
from pipeline.transitions import StaleDecision, transition

pytestmark = pytest.mark.django_db(transaction=True)

APPROVED = ReviewStatus.APPROVED


# ── atomicity: the reason this table exists ─────────────────────────────────────


def test_an_event_is_written_with_the_status_change(api, product):
    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": APPROVED}, format="json")

    event = OutboxEvent.objects.get(product=product, event_type=ArtifactType.CATEGORY)
    assert event.status == APPROVED


def test_a_rolled_back_change_leaves_no_event(product):
    """The half this protects against: a committed event for state that never landed."""
    with pytest.raises(RuntimeError):
        with transaction.atomic():
            transition(product, ArtifactType.CATEGORY, APPROVED)
            raise RuntimeError("something failed after the transition")

    product.refresh_from_db()
    assert product.category_status == ReviewStatus.PENDING
    assert OutboxEvent.objects.count() == 0


def test_a_refused_decision_emits_no_event(api, product):
    """A stale decision changes nothing, so it must announce nothing."""
    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": APPROVED}, format="json")
    before = OutboxEvent.objects.count()

    api.post(reverse("pipeline:category-decide", args=[product.id]),
             {"decision": ReviewStatus.REJECTED}, format="json")     # 409

    assert OutboxEvent.objects.count() == before


def test_stage_transitions_are_announced_too(product):
    """Not just reviewer decisions — a stage completing may make another eligible."""
    transition(product, ArtifactType.INGESTION, JobStatus.COMPLETED,
               status_field="ingestion_status")

    assert OutboxEvent.objects.filter(event_type=ArtifactType.INGESTION).exists()


# ── publishing ──────────────────────────────────────────────────────────────────


def test_publishing_marks_rows_and_is_idempotent(product):
    transition(product, ArtifactType.CATEGORY, APPROVED)
    # on_commit already relayed it; the sweep must then find nothing left to do.
    assert outbox.backlog() == 0
    assert outbox.publish_pending() == 0


def test_an_unpublished_event_is_relayed_by_the_sweep(product):
    """The crash path: the row committed but `on_commit` never ran."""
    transition(product, ArtifactType.CATEGORY, APPROVED)
    OutboxEvent.objects.update(published_at=None)            # simulate the miss

    assert outbox.backlog() == 1
    assert outbox.publish_pending() == 1
    assert outbox.backlog() == 0


def test_a_failing_relay_leaves_the_row_unpublished(product, monkeypatch):
    """At-least-once: if the notify fails, the row must stay claimable."""
    transition(product, ArtifactType.CATEGORY, APPROVED)
    OutboxEvent.objects.update(published_at=None)

    monkeypatch.setattr(outbox, "notify",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no conn")))
    with pytest.raises(RuntimeError):
        outbox.publish_pending()

    assert outbox.backlog() == 1                             # retried next sweep


def test_a_broken_relay_does_not_fail_the_decision(api, product, monkeypatch):
    """The notification is an optimisation. A reviewer must never see their committed
    decision reported as a failure because a nudge could not be sent."""
    monkeypatch.setattr(outbox, "publish_pending",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("relay down")))

    response = api.post(reverse("pipeline:category-decide", args=[product.id]),
                        {"decision": APPROVED}, format="json")

    assert response.status_code == 200
    product.refresh_from_db()
    assert product.category_status == APPROVED               # committed regardless


# ── the event is a nudge, not an instruction ────────────────────────────────────


def test_the_event_does_not_carry_the_work(product):
    """It names the product and what changed — nothing a consumer could act on blindly."""
    transition(product, ArtifactType.CATEGORY, APPROVED)
    event = OutboxEvent.objects.get()

    assert event.product_id == product.pk
    assert event.event_type == ArtifactType.CATEGORY
    assert not hasattr(event, "task")
    assert not hasattr(event, "payload")


def test_eligibility_still_decides_not_the_event(product):
    """An event for a product that was deactivated afterwards must lead to no work."""
    from pipeline.stages.base import claim_one
    from pipeline.stages.ingest import INGEST_STAGE

    transition(product, ArtifactType.CATEGORY, APPROVED)
    assert OutboxEvent.objects.exists()                      # announced

    Product.objects.filter(pk=product.pk).update(is_active=False)
    assert claim_one(INGEST_STAGE) is None                   # ...and correctly ignored


# ── housekeeping ────────────────────────────────────────────────────────────────


def test_prune_keeps_unpublished_rows(product):
    from datetime import timedelta

    from django.utils import timezone

    transition(product, ArtifactType.CATEGORY, APPROVED)
    OutboxEvent.objects.update(published_at=timezone.now() - timedelta(days=60))
    transition(product, ArtifactType.DIMENSIONS, APPROVED)
    OutboxEvent.objects.filter(event_type=ArtifactType.DIMENSIONS).update(published_at=None)

    assert outbox.prune(older_than_days=30) == 1
    assert OutboxEvent.objects.count() == 1                  # the unpublished one survives
