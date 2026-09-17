"""The outbox relay: durable event rows -> Postgres LISTEN/NOTIFY.

Why both, rather than just one:

* **NOTIFY alone is not durable.** Postgres drops a notification if no session is
  listening. A worker restarting at the wrong moment would miss it permanently.
* **An outbox row alone is not fast.** It still has to be discovered.

So the row is the truth and the notification is the nudge. `publish_pending` sends the
nudge and marks the row; anything it fails to send stays unpublished and is retried on
the next sweep. Delivery is therefore at-least-once, which is the strongest guarantee
worth paying for here — consumers are idempotent because claiming a product is a
conditional update, so a duplicate nudge finds nothing to claim.

Ordering note: the NOTIFY is issued INSIDE the transaction that marks the row published.
Postgres queues notifications until commit, so if that transaction rolls back the
notification is never delivered and the row stays unpublished — the two can't disagree.
"""

from __future__ import annotations

import logging

import psycopg2
import psycopg2.extensions
from django.conf import settings
from django.db import connection, transaction
from django.utils import timezone

from pipeline.models import OutboxEvent

logger = logging.getLogger(__name__)

#: The single channel every worker listens on. The payload is advisory — a worker
#: re-queries eligibility regardless — so there is no need to fan out per stage.
CHANNEL = "pipeline_events"


def listen_connection() -> "psycopg2.extensions.connection":
    """A dedicated autocommit connection parked on `LISTEN {CHANNEL}`.

    Separate from Django's ORM connection by necessity: it must sit in autocommit and
    block in `select()`, neither of which is compatible with a connection the ORM is
    also using for transactions. Built from Django's own DATABASES entry so it cannot
    drift from the ORM's target.

    Shared by every long-lived listener — `run_worker.py`'s stage loop and the SSE
    endpoint (`pipeline/streaming.py`) alike — so there is exactly one place this setup
    can go wrong, not two slowly-diverging copies of it.
    """
    params = settings.DATABASES["default"]
    conn = psycopg2.connect(
        dbname=params["NAME"],
        user=params["USER"],
        password=params["PASSWORD"],
        host=params["HOST"] or "localhost",
        port=params["PORT"] or 5432,
    )
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    with conn.cursor() as cur:
        cur.execute(f"LISTEN {CHANNEL};")
    return conn


def record(product_id: int, event_type: str, status: str) -> OutboxEvent:
    """Append an event. MUST be called inside the transaction that changed the state."""
    return OutboxEvent.objects.create(
        product_id=product_id, event_type=event_type, status=status
    )


def notify(payload: str = "") -> None:
    """Wake every listening worker. Safe to call redundantly."""
    with connection.cursor() as cursor:
        # NOTIFY takes a literal channel, not a bind parameter. CHANNEL is a module
        # constant rather than anything caller-supplied, so there is nothing to inject.
        cursor.execute(f"NOTIFY {CHANNEL}, %s", [payload[:200]])


def publish_pending(limit: int = 500) -> int:
    """Relay unpublished events. Returns how many were sent.

    Claims with SKIP LOCKED so several dispatchers can run without doubling up, and so a
    stuck row never blocks the ones behind it.
    """
    with transaction.atomic():
        pending = list(
            OutboxEvent.objects.filter(published_at__isnull=True)
            .select_for_update(skip_locked=True)
            .order_by("created_at", "id")[:limit]
        )
        if not pending:
            return 0

        notify(f"{len(pending)} event(s)")
        OutboxEvent.objects.filter(pk__in=[e.pk for e in pending]).update(
            published_at=timezone.now()
        )

    logger.debug("outbox: published %d event(s)", len(pending))
    return len(pending)


def backlog() -> int:
    """Unpublished count — a dispatcher that has stopped shows up as this climbing."""
    return OutboxEvent.objects.filter(published_at__isnull=True).count()


def prune(older_than_days: int = 30) -> int:
    """Drop published events past their useful life.

    The table is an operational log, not the audit trail — `ReviewEvent` is the record
    that must be kept. Without pruning this grows without bound.
    """
    cutoff = timezone.now() - timezone.timedelta(days=older_than_days)
    deleted, _ = OutboxEvent.objects.filter(
        published_at__isnull=False, published_at__lt=cutoff
    ).delete()
    return deleted
