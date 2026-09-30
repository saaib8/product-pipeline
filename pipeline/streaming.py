"""SSE endpoint: tells a connected browser "something changed", nothing more.

Mirrors the LISTEN/NOTIFY pattern `run_worker.py` already uses to wake stage threads, but
pushed to a browser tab instead. Same philosophy as the outbox itself: this is an advisory
nudge, not a data channel — every event just means "go re-call the REST endpoints you
already call," so a missed or duplicate event can never cause the UI to show wrong data,
only stale data for a while.

Async on purpose, and it has to stay that way: the app is served over ASGI, and Django
serves a *sync* streaming iterator under ASGI by calling `list()` on it first — an endless
generator never finishes that `list()`, so the tab would sit "connected" and never receive a
byte. As an async generator, an open tab costs an idle coroutine, not a thread.

One LISTEN connection per server process, not per tab: `_Broadcaster` holds it, and fans
each notification out to every open tab through a small in-memory queue. So Postgres sees
one connection per process however many reviewers have the app open.

Deliberately not a DRF `@api_view`: DRF's renderer/content-negotiation layer is built to
render a `Response` once, not to stream a `text/event-stream` body indefinitely. Auth is
still enforced — the same session middleware every other endpoint relies on has already
run, so no DRF machinery is needed for that check.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

import psycopg2
from asgiref.sync import sync_to_async
from django.db import connection
from django.http import HttpRequest, HttpResponseForbidden, StreamingHttpResponse

from pipeline import outbox
from pipeline.models import OutboxEvent

logger = logging.getLogger(__name__)

#: How long to wait for a notification before sending a keep-alive comment. Some
#: reverse proxies and corporate networks silently drop a connection that has sent
#: nothing for 60-120s, mistaking a quiet-but-alive stream for a dead one.
_KEEPALIVE_SECONDS = 20

#: Bound on reconnect replay, matching `outbox.publish_pending`'s own batch cap — a
#: reconnecting tab should never trigger an unbounded query.
_CATCH_UP_LIMIT = 500

#: Put on a tab's queue when the shared listener dies: the stream ends, the browser's
#: EventSource reconnects with `Last-Event-ID`, and catch-up covers whatever was missed.
_CLOSED = object()


def _format_sse(event_id: int) -> str:
    """One SSE frame. `id:` is what makes `Last-Event-ID` reconnect replay possible."""
    return f"id: {event_id}\ndata: refresh\nretry: 5000\n\n"


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


def _release_db_connection() -> None:
    """Close this thread's ORM connection so an open tab doesn't hold one idle.

    Skipped inside an atomic block — that only happens under a test transaction here, and
    closing there would break the test rather than save anything.
    """
    if not connection.in_atomic_block:
        connection.close()


def _catch_up_ids(last_event_id: int | None) -> list[int]:
    try:
        return [event.pk for event in _catch_up(last_event_id)]
    finally:
        _release_db_connection()


def _latest_event_id() -> int | None:
    # The NOTIFY payload is advisory only (same rule as the outbox itself) — re-query
    # rather than trust anything Postgres handed back.
    try:
        return OutboxEvent.objects.order_by("-pk").values_list("pk", flat=True).first()
    finally:
        _release_db_connection()


def _offer(queue: asyncio.Queue, item: object) -> None:
    """Replace whatever is waiting: a tab only ever needs the newest event id."""
    if queue.full():
        queue.get_nowait()
    queue.put_nowait(item)


class _Broadcaster:
    """One LISTEN connection for the whole process, shared by every open tab.

    Opened when the first tab subscribes, closed when the last one leaves. The socket is
    watched with `loop.add_reader`, so waiting for a notification blocks nothing. A burst
    of notifications costs one `latest id` query for the whole process, not one per tab.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()
        self._conn = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._dirty: asyncio.Event | None = None
        self._fan_out_task: asyncio.Task | None = None

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        async with self._lock:
            if self._conn is None:
                await self._start()
            self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)
            if not self._subscribers:
                self._stop()

    async def _start(self) -> None:
        # Connecting is blocking I/O — keep it off the event loop.
        conn = await sync_to_async(outbox.listen_connection, thread_sensitive=False)()
        loop = asyncio.get_running_loop()
        self._conn, self._loop = conn, loop
        self._dirty = asyncio.Event()
        loop.add_reader(conn.fileno(), self._on_readable)
        self._fan_out_task = loop.create_task(self._fan_out())

    def _stop(self) -> None:
        """Release the listener. Safe to call when it's already gone."""
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            self._loop.remove_reader(conn.fileno())
        except Exception:                                  # noqa: BLE001
            pass
        if self._fan_out_task is not None:
            self._fan_out_task.cancel()
            self._fan_out_task = None
        try:
            conn.close()
        except Exception:                                  # noqa: BLE001
            pass

    def _on_readable(self) -> None:
        try:
            self._conn.poll()
        except (psycopg2.Error, OSError) as exc:
            logger.warning("SSE listener dropped (%s); ending open streams", exc)
            self._fail()
            return
        # Coalesce: ten notifications and one both mean "tell the tabs".
        if self._conn.notifies:
            self._conn.notifies.clear()
            self._dirty.set()

    def _fail(self) -> None:
        # Swap the set out first, so a tab that subscribes after this gets a fresh
        # listener and can't have its queue handed the sentinel meant for these.
        subscribers, self._subscribers = self._subscribers, set()
        for queue in subscribers:
            _offer(queue, _CLOSED)
        self._stop()

    async def _fan_out(self) -> None:
        while True:
            await self._dirty.wait()
            self._dirty.clear()
            try:
                latest = await sync_to_async(_latest_event_id, thread_sensitive=False)()
            except Exception:                              # noqa: BLE001
                logger.exception("SSE fan-out: could not read the latest event")
                continue
            if latest is None:
                continue
            for queue in list(self._subscribers):
                _offer(queue, latest)


_broadcaster = _Broadcaster()


async def _generate(
    last_event_id: int | None, broadcaster: _Broadcaster | None = None
) -> AsyncIterator[bytes]:
    broadcaster = broadcaster or _broadcaster
    try:
        # Subscribe BEFORE catching up: a NOTIFY landing between the two is then already
        # queued, instead of falling into the gap and being missed.
        async with broadcaster.subscribe() as queue:
            for event_id in await sync_to_async(_catch_up_ids)(last_event_id):
                yield _format_sse(event_id).encode()

            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), _KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield b": keep-alive\n\n"
                    continue
                if item is _CLOSED:
                    return
                yield _format_sse(item).encode()
    except psycopg2.OperationalError as exc:
        # Couldn't open the listener. Ending the stream lets EventSource retry on its own.
        logger.warning("SSE: could not LISTEN (%s)", exc)


async def event_stream(request: HttpRequest) -> StreamingHttpResponse:
    """`GET /api/events/` — hold the connection open, nudge on every outbox write."""
    user = await request.auser()
    if not user.is_authenticated:
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
