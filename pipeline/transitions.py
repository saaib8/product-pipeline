"""The only sanctioned way a status changes.

Two rules make this safe, and both are easy to lose if transitions are written inline:

1. The status write and its audit row happen in **one transaction**, so they can never
   disagree.
2. Anything scheduled off the back of a transition is registered with
   ``transaction.on_commit``, so a rolled-back transaction never triggers downstream
   work for a state that never existed.

3. Every transition appends a row to the **transactional outbox** in that same
   transaction, and a post-commit relay turns it into a Postgres ``NOTIFY`` so workers
   wake in milliseconds instead of on their next poll.

Correctness still comes from the poller re-evaluating eligibility, never from the
notification firing. A lost NOTIFY costs latency, bounded by the poll interval; it can
never lose a product. That ordering — durable row first, nudge second — is what keeps
the fast path from becoming a correctness dependency.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction

from pipeline.enums import ArtifactType
from pipeline.models import Product, ReviewEvent

logger = logging.getLogger(__name__)


class StaleDecision(Exception):
    """The artifact moved on before this decision landed.

    Raised when a caller declared what it expected the current status to be and the
    locked row disagrees — meaning another reviewer (or a stage) already decided this
    artifact while the first reviewer's page was open.
    """

    def __init__(self, field: str, actual: str, expected):
        self.field, self.actual, self.expected = field, actual, tuple(expected)
        super().__init__(f"{field} is {actual}, expected one of {self.expected}")


#: Which column each reviewable artifact writes.
STATUS_FIELD: dict[str, str] = {
    ArtifactType.CATEGORY: "category_status",
    ArtifactType.DIMENSIONS: "dimensions_status",
    ArtifactType.INGESTION: "ingestion_status",
    ArtifactType.ICON_2D: "icon_2d_status",
    ArtifactType.MODEL_3D: "model_3d_status",
}


def dispatch_ready(product_id: int) -> None:
    """Relay the outbox immediately, so a worker wakes now rather than on its next poll.

    Runs via ``on_commit``, so it can only fire for state that actually committed. If the
    process dies before this runs, the outbox row is still there, unpublished, and the
    worker's periodic sweep relays it — the delay is bounded by the poll interval, and
    nothing is lost. That is the entire reason the row is written before this is called
    rather than instead of it.

    Failure here is logged and swallowed on purpose: the notification is an optimisation,
    and letting it raise would surface a latency problem to the reviewer as a failed
    decision when their decision has already been committed.
    """
    from pipeline import outbox

    try:
        outbox.publish_pending()
    except Exception:                                       # noqa: BLE001
        logger.warning("outbox relay failed for product %s; the sweep will retry",
                       product_id, exc_info=True)


@transaction.atomic
def transition(
    product: Product,
    artifact: str,
    to_status: str,
    *,
    reviewer=None,
    note: str = "",
    changes: dict[str, Any] | None = None,
    status_field: str | None = None,
    expect: tuple[str, ...] | None = None,
) -> Product:
    """Move one artifact to a new status, audit it, and schedule what became eligible.

    ``changes`` applies reviewer corrections (a fixed category, corrected dimensions) in
    the same transaction, and records the previous values on the audit row so a
    correction is always reversible in hindsight.

    ``expect`` is the caller's claim about the current status. It is checked **after**
    the row lock is taken, which is what makes it race-free: two reviewers submitting at
    the same instant serialise on the lock, the first wins, and the second finds a status
    it did not expect and raises `StaleDecision`. Without it the later write silently
    overwrites the earlier one — including rejecting a product a colleague just approved.

    Stages pass no ``expect``; they have already claimed the row with
    ``SELECT … FOR UPDATE SKIP LOCKED``, which is a stronger guarantee.
    """
    # A stage passes the column it owns; reviewable artifacts fall back to the map.
    field = status_field or STATUS_FIELD[artifact]
    updates: dict[str, Any] = {field: to_status}
    previous: dict[str, Any] | None = None

    # Lock the row so two reviewers acting at once can't interleave a correction.
    locked = Product.objects.select_for_update().get(pk=product.pk)

    if expect is not None and getattr(locked, field) not in expect:
        raise StaleDecision(field, getattr(locked, field), expect)

    if changes:
        # Read the previous values off the LOCKED row, not the caller's copy, which may
        # already be stale by the time we get here.
        previous = {k: _jsonable(getattr(locked, k)) for k in changes}
        updates.update(changes)

    for key, value in updates.items():
        setattr(locked, key, value)
    # `time_updated` must be bumped: claims are ordered by it, so a row that keeps
    # failing would otherwise stay at the front of the queue and starve every other
    # product. Retrying sends it to the back.
    locked.save(update_fields=[*updates.keys(), "time_updated"])

    ReviewEvent.objects.create(
        product=locked,
        artifact_type=artifact,
        decision=to_status,
        note=note,
        attempt_no=locked.review_events.filter(artifact_type=artifact).count(),
        reviewer=reviewer if (reviewer and reviewer.is_authenticated) else None,
        previous_value=previous,
    )

    # The outbox row lands in THIS transaction, alongside the status change it describes.
    # Committed together or not at all — which is the guarantee `product.save()` followed
    # by `publish()` cannot give, because a crash between them loses the notification for
    # a state change that is already durable.
    from pipeline import outbox

    outbox.record(locked.pk, artifact, to_status)

    transaction.on_commit(lambda: dispatch_ready(locked.pk))
    return locked



def _jsonable(value: Any) -> Any:
    """Decimals and the like don't survive JSONField round-tripping."""
    if value is None or isinstance(value, (str, int, float, bool, list, dict)):
        return value
    return str(value)
