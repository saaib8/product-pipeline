"""The SSE endpoint's testable surface.

The blocking LISTEN/select loop itself is not exercised here, deliberately — the same
gap exists for `run_worker.py`'s equivalent (`_listen_loop`), for the same reason: a real
Postgres LISTEN socket in a test process is slow and fragile to assert against. What's
tested instead is the part that decides correctness: who gets in, and what a reconnecting
tab is handed to catch up on.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from pipeline.enums import ArtifactType
from pipeline.streaming import _catch_up, _format_sse
from pipeline.transitions import transition

pytestmark = pytest.mark.django_db


def test_unauthenticated_request_is_rejected(client):
    """A plain (non-`force_authenticate`d) client — the same guarantee every other
    endpoint gets from `IsAuthenticated`, just enforced by hand since this isn't DRF."""
    response = client.get(reverse("pipeline:event-stream"))
    assert response.status_code == 403


def test_catch_up_with_no_last_id_returns_nothing(product):
    """A fresh connection has nothing to catch up on — the page load already fetched
    current state, so replaying history here would just be noise."""
    transition(product, ArtifactType.CATEGORY, "APPROVED")
    assert list(_catch_up(None)) == []


def test_catch_up_returns_only_events_after_last_id(product):
    """The reconnect-replay path: everything newer than what the tab last saw."""
    transition(product, ArtifactType.CATEGORY, "APPROVED")
    first_id = product.outbox_events.get().pk

    transition(product, ArtifactType.DIMENSIONS, "APPROVED")
    second = product.outbox_events.exclude(pk=first_id).get()

    caught_up = list(_catch_up(first_id))
    assert caught_up == [second]


def test_catch_up_is_bounded(product):
    for _ in range(3):
        transition(product, ArtifactType.CATEGORY, "PENDING")

    assert len(list(_catch_up(0, limit=2))) == 2


def test_format_sse_carries_the_event_id_and_a_bare_nudge(product):
    transition(product, ArtifactType.CATEGORY, "APPROVED")
    event = product.outbox_events.get()

    frame = _format_sse(event)

    assert frame.startswith(f"id: {event.pk}\n")
    assert "data: refresh\n" in frame
    assert frame.endswith("\n\n")
