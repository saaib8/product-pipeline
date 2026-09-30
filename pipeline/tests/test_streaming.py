"""The SSE endpoint: who gets in, what a reconnecting tab catches up on, and the shared
per-process listener that fans notifications out to every open tab.

Most broadcaster tests use a fake listen connection backed by a socketpair, so the
event-loop reader has a real file descriptor to watch without needing Postgres. One test
at the bottom runs the whole path against a real LISTEN, since that's the part a fake
can't prove. Async code is driven with `async_to_sync` — no extra test plugin needed.
"""

from __future__ import annotations

import asyncio
import socket

import psycopg2
import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.urls import reverse

from pipeline import streaming
from pipeline.enums import ArtifactType
from pipeline.models import OutboxEvent
from pipeline.streaming import _CLOSED, _Broadcaster, _catch_up, _format_sse, _generate, _offer
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

    frame = _format_sse(event.pk)

    assert frame.startswith(f"id: {event.pk}\n")
    assert "data: refresh\n" in frame
    assert frame.endswith("\n\n")


def test_authenticated_stream_is_async(client, reviewer):
    """Regression guard: under ASGI a sync streaming iterator is `list()`ed before a byte
    is sent, so an endless one never delivers anything. This must stay async."""
    client.force_login(reviewer)
    response = client.get(reverse("pipeline:event-stream"))

    assert response.status_code == 200
    assert response.streaming and response.is_async
    assert response["Content-Type"] == "text/event-stream"
    assert response["X-Accel-Buffering"] == "no"


# ── the shared listener ─────────────────────────────────────────────────────────


class FakeListenConnection:
    """Stands in for `outbox.listen_connection()`: `ring()` is a NOTIFY arriving."""

    def __init__(self):
        self._reader, self._writer = socket.socketpair()
        self.notifies: list = []
        self.closed = False
        self.broken = False

    def fileno(self):
        return self._reader.fileno()

    def poll(self):
        self._reader.recv(1024)
        if self.broken:
            raise psycopg2.OperationalError("server closed the connection unexpectedly")

    def ring(self):
        self.notifies.append("pipeline_events")
        self._writer.send(b"x")

    def close(self):
        self.closed = True
        self._reader.close()
        self._writer.close()


@pytest.fixture
def listeners(monkeypatch):
    """Every listen connection opened during the test, newest last."""
    opened: list[FakeListenConnection] = []

    def fake_listen_connection():
        opened.append(FakeListenConnection())
        return opened[-1]

    monkeypatch.setattr("pipeline.outbox.listen_connection", fake_listen_connection)
    monkeypatch.setattr(streaming, "_latest_event_id", lambda: 42)
    monkeypatch.setattr(streaming, "_catch_up_ids", lambda last_event_id: [])
    return opened


def test_offer_keeps_only_the_newest_item():
    queue = asyncio.Queue(maxsize=1)
    _offer(queue, 1)
    _offer(queue, 2)
    assert queue.get_nowait() == 2
    assert queue.empty()


def test_one_listener_fans_out_to_every_tab_and_closes_with_the_last(listeners):
    async def scenario():
        broadcaster = _Broadcaster()
        async with broadcaster.subscribe() as tab1, broadcaster.subscribe() as tab2:
            assert len(listeners) == 1
            listeners[0].ring()
            assert await asyncio.wait_for(tab1.get(), 1) == 42
            assert await asyncio.wait_for(tab2.get(), 1) == 42
            assert not listeners[0].closed
        assert listeners[0].closed

    async_to_sync(scenario)()


def test_dropped_listener_ends_open_streams_and_next_tab_reopens(listeners):
    async def scenario():
        broadcaster = _Broadcaster()
        async with broadcaster.subscribe() as tab1, broadcaster.subscribe() as tab2:
            listeners[0].broken = True
            listeners[0].ring()
            assert await asyncio.wait_for(tab1.get(), 1) is _CLOSED
            assert await asyncio.wait_for(tab2.get(), 1) is _CLOSED
            assert listeners[0].closed

            async with broadcaster.subscribe() as tab3:
                assert len(listeners) == 2
                listeners[1].ring()
                assert await asyncio.wait_for(tab3.get(), 1) == 42
        assert listeners[1].closed

    async_to_sync(scenario)()


def test_stream_sends_keepalive_then_frame_and_releases_on_close(listeners, monkeypatch):
    monkeypatch.setattr(streaming, "_KEEPALIVE_SECONDS", 0.05)

    async def scenario():
        stream = _generate(None, _Broadcaster())
        assert await anext(stream) == b": keep-alive\n\n"
        listeners[0].ring()
        frame = await anext(stream)
        while frame.startswith(b":"):
            frame = await anext(stream)
        assert frame == _format_sse(42).encode()
        await stream.aclose()          # what Django does when the browser goes away
        assert listeners[0].closed

    async_to_sync(scenario)()


def test_stream_ends_when_the_listener_drops(listeners):
    async def scenario():
        stream = _generate(None, _Broadcaster())
        pending = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0.05)
        listeners[0].broken = True
        listeners[0].ring()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(pending, 1)

    async_to_sync(scenario)()


def test_subscribes_before_catching_up(listeners, monkeypatch):
    """A NOTIFY between catch-up and subscribe would otherwise be lost."""
    broadcaster = _Broadcaster()
    seen = []

    def catch_up_ids(last_event_id):
        seen.append(len(broadcaster._subscribers))
        return [7, 8]

    monkeypatch.setattr(streaming, "_catch_up_ids", catch_up_ids)

    async def scenario():
        stream = _generate(3, broadcaster)
        assert await anext(stream) == _format_sse(7).encode()
        assert await anext(stream) == _format_sse(8).encode()
        await stream.aclose()

    async_to_sync(scenario)()
    assert seen == [1]


@pytest.mark.django_db(transaction=True)
def test_real_notify_reaches_an_open_stream(product):
    """End to end against real Postgres: a committed transition reaches an open tab."""

    async def scenario():
        stream = _generate(None, _Broadcaster())
        pending = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0.3)       # let it subscribe
        await sync_to_async(transition)(product, ArtifactType.CATEGORY, "APPROVED")
        frame = await asyncio.wait_for(pending, 3)
        await stream.aclose()
        return frame

    frame = async_to_sync(scenario)()
    latest = OutboxEvent.objects.order_by("-pk").first()
    assert frame == _format_sse(latest.pk).encode()
