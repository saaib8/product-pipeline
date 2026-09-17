"""SSE endpoint: tells a connected browser "something changed", nothing more.

Mirrors the LISTEN/NOTIFY pattern `run_worker.py` already uses to wake stage threads, but
pushed to a browser tab instead. Same philosophy as the outbox itself: this is an advisory
nudge, not a data channel — every event just means "go re-call the REST endpoints you
already call," so a missed or duplicate event can never cause the UI to show wrong data,
only stale data for a while.

Deliberately not a DRF `@api_view`: DRF's renderer/content-negotiation layer is built to
render a `Response` once, not to stream a `text/event-stream` body indefinitely. Auth is
still enforced — `request.user` is already populated by the same session middleware every
other endpoint relies on, so no DRF machinery is needed for that check.
"""

from __future__ import annotations

import select
from collections.abc import Iterator

import psycopg2
from django.http import HttpRequest, HttpResponseForbidden, StreamingHttpResponse

from pipeline import outbox
from pipeline.models import OutboxEvent

#: How long to wait for a notification before sending a keep-alive comment. Some
#: reverse proxies and corporate networks silently drop a connection that has sent
#: nothing for 60-120s, mistaking a quiet-but-alive stream for a dead one.
_KEEPALIVE_SECONDS = 20

#: Bound on reconnect replay, matching `outbox.publish_pending`'s own batch cap — a
#: reconnecting tab should never trigger an unbounded query.
_CATCH_UP_LIMIT = 500


def _format_sse(event: OutboxEvent) -> str:
    """One SSE frame. `id:` is what makes `Last-Event-ID` reconnect replay possible."""
    return f"id: {event.pk}\ndata: refresh\nretry: 5000\n\n"


def _catch_up(last_event_id: int | None, limit: int = _CATCH_UP_LIMIT):
    """Events a reconnecting tab missed while it was disconnected.

    Empty on a fresh connection (no `Last-Event-ID` yet — there is nothing to catch up
    on, the first page load already fetched current state). Non-empty only on reconnect,
    which is exactly the gap a raw NOTIFY relay can't close on its own: Postgres only
    delivers to a session that is listening *at that instant*, so a tab that was mid
    reconnect when a NOTIFY fired would otherwise never learn about it.
    """
    if last_event_id is None:
        return OutboxEvent.objects.none()
    return OutboxEvent.objects.filter(pk__gt=last_event_id).order_by("pk")[:limit]


def _generate(last_event_id: int | None) -> Iterator[bytes]:
    for event in _catch_up(last_event_id):
        yield _format_sse(event).encode()

    conn = outbox.listen_connection()
    try:
        while True:
            try:
                ready, _, _ = select.select([conn], [], [], _KEEPALIVE_SECONDS)
            except (psycopg2.Error, OSError):
                return

            if not ready:
                yield b": keep-alive\n\n"
                continue

            conn.poll()
            while conn.notifies:
                conn.notifies.pop()
            # The payload is advisory only (same rule as the outbox itself) — re-query
            # rather than trust anything Postgres handed back in the NOTIFY payload.
            latest = OutboxEvent.objects.order_by("-pk").first()
            if latest is not None:
                yield _format_sse(latest).encode()
    finally:
        try:
            conn.close()
        except Exception:                                  # noqa: BLE001
            pass


def event_stream(request: HttpRequest) -> StreamingHttpResponse:
    """`GET /api/events/` — hold the connection open, nudge on every outbox write."""
    if not request.user.is_authenticated:
        return HttpResponseForbidden("Authentication required.")

    last_event_id_header = request.headers.get("Last-Event-ID")
    last_event_id = int(last_event_id_header) if last_event_id_header else None

    response = StreamingHttpResponse(
        _generate(last_event_id), content_type="text/event-stream"
    )
    response["Cache-Control"] = "no-cache"
    # Tells nginx specifically not to buffer this response. Other reverse proxies or a
    # CDN in front of this may need their own equivalent — not something this repo can
    # configure, since no production topology is defined here.
    response["X-Accel-Buffering"] = "no"
    return response
