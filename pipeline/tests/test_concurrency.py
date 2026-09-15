"""Draining a stage with more than one thread.

Concurrency is only worth having if it changes throughput without changing outcomes, so
what is pinned here is that every guarantee survives it: a row is claimed once and only
once, the limit still means what it says, counters do not lose updates to a race, and
shutdown still stops between products rather than during one.

These use `transaction=True` because the worker threads need to see rows committed by
the test, and the test needs to see theirs.
"""

from __future__ import annotations

import threading
import time

import pytest
from django.db.models import Q

from pipeline.enums import ArtifactType, JobStatus
from pipeline.models import Product, Store
from pipeline.stages.base import (
    Stage,
    reset_shutdown,
    request_shutdown,
    run_stage,
)

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _clean_shutdown():
    reset_shutdown()
    yield
    reset_shutdown()


@pytest.fixture
def many(db) -> list[Product]:
    store = Store.objects.create(name_english="Demo Store", provider="OTHER")
    return [
        Product.objects.create(
            store=store, name_english=f"Product {i}", name_arabic="—",
            image_url=f"https://example.invalid/{i}.jpg",
            product_url=f"https://example.invalid/p/{i}",
            category="3-seater-sofa", length=1, width=1, height=1,
            price_amount=1, price_unit="SAR",
        )
        for i in range(24)
    ]


def make_stage(run, concurrency: int) -> Stage:
    return Stage(
        name="probe",
        artifact=ArtifactType.INGESTION,
        status_field="ingestion_status",
        eligible=Q(ingestion_status=JobStatus.PENDING, is_active=True),
        run=run,
        concurrency=concurrency,
        max_attempts=2,
    )


# ── the guarantee that matters ──────────────────────────────────────────────────


def test_no_row_is_claimed_twice(many):
    """SKIP LOCKED holds across threads exactly as it does across processes."""
    seen: list[int] = []
    lock = threading.Lock()

    def run(product):
        time.sleep(0.01)                       # widen the window for a double-claim
        with lock:
            seen.append(product.pk)

    result = run_stage(make_stage(run, concurrency=8), limit=100)

    assert len(seen) == len(set(seen)) == len(many)
    assert result.processed == result.succeeded == len(many)


def test_work_actually_overlaps(many):
    """Otherwise this is just a slower sequential drain wearing threads."""
    peak = 0
    active = 0
    lock = threading.Lock()

    def run(product):
        nonlocal peak, active
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1

    run_stage(make_stage(run, concurrency=8), limit=100)
    assert peak > 1, "no two products were ever in flight together"


def test_rows_arriving_mid_drain_are_picked_up_in_parallel(many):
    """Regression: the drain used to collapse to ONE thread.

    A drain woken by the first approval of a batch starts when a single row is eligible.
    Seven of eight threads found nothing, exited within milliseconds, and the survivor
    processed the whole batch serially — six icons 30s apart instead of six at once. A
    thread must therefore keep looking while a peer is still working.
    """
    later = [p.pk for p in many[1:]]
    Product.objects.filter(pk__in=later).update(ingestion_status=JobStatus.COMPLETED)

    peak = active = 0
    lock = threading.Lock()
    released = threading.Event()

    def run(product):
        nonlocal peak, active
        if not released.is_set():
            released.set()
            # The rest of the batch is approved while the first row is being worked on.
            Product.objects.filter(pk__in=later).update(ingestion_status=JobStatus.PENDING)
            time.sleep(0.4)          # long enough for idle threads to poll again
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.15)
        with lock:
            active -= 1

    result = run_stage(make_stage(run, concurrency=8), limit=100)

    assert result.processed == len(many)
    assert peak > 1, (
        "the drain collapsed to a single thread — rows that arrived after it started "
        "were processed serially"
    )


def test_an_idle_thread_gives_up_once_nothing_is_in_flight(db):
    """The other half: with no work and no peers busy, the drain must end promptly
    rather than spinning."""
    started = time.monotonic()
    result = run_stage(make_stage(lambda p: None, concurrency=8), limit=100)
    assert result.processed == 0
    assert time.monotonic() - started < 2.0, "empty drain did not return promptly"


def test_counters_do_not_lose_updates(many):
    """Every counter moves under one lock; a lost `+= 1` shows up as a shortfall here.

    Note `processed` exceeds the number of products, and that is correct: a failed row
    returns to PENDING, becomes eligible again, and is re-claimed inside the same drain
    until it exhausts `max_attempts`. The sequential path behaves identically — the
    threads change the timing, not the accounting.
    """
    doomed = {p.pk for p in many[:6]}

    def run(product):
        if product.pk in doomed:
            raise RuntimeError("boom")

    result = run_stage(make_stage(run, concurrency=8), limit=100)

    # 18 clean + 6 that each burn two attempts (PENDING, then FAILED at the cap).
    assert result.succeeded == 18
    assert result.retried == 6
    assert result.failed == 6
    assert result.processed == 30
    assert result.succeeded + result.retried + result.failed == result.processed


def test_every_product_reaches_a_terminal_state(many):
    """Nothing is left mid-flight or silently dropped by the threaded drain."""
    doomed = {p.pk for p in many[:6]}

    def run(product):
        if product.pk in doomed:
            raise RuntimeError("boom")

    run_stage(make_stage(run, concurrency=8), limit=100)

    assert Product.objects.filter(ingestion_status=JobStatus.IN_PROGRESS).count() == 0
    assert Product.objects.filter(ingestion_status=JobStatus.PENDING).count() == 0
    assert Product.objects.filter(ingestion_status=JobStatus.COMPLETED).count() == 18
    assert Product.objects.filter(ingestion_status=JobStatus.FAILED).count() == 6


# ── limits and shutdown still mean what they said ───────────────────────────────


def test_the_limit_is_respected_across_threads(many):
    seen = []
    lock = threading.Lock()

    def run(product):
        with lock:
            seen.append(product.pk)

    result = run_stage(make_stage(run, concurrency=8), limit=5)

    assert result.processed == 5
    assert len(seen) == 5
    assert Product.objects.filter(ingestion_status=JobStatus.PENDING).count() == len(many) - 5


def test_shutdown_stops_the_drain(many):
    """Requested mid-flight: whatever is running finishes, nothing new is claimed."""
    started = threading.Event()

    def run(product):
        started.set()
        time.sleep(0.02)

    def stopper():
        started.wait(timeout=5)
        request_shutdown()

    watcher = threading.Thread(target=stopper)
    watcher.start()
    result = run_stage(make_stage(run, concurrency=4), limit=100)
    watcher.join()

    assert result.processed < len(many)         # stopped early
    # ...and everything it did claim reached a terminal state, none left mid-flight.
    assert Product.objects.filter(ingestion_status=JobStatus.IN_PROGRESS).count() == 0


def test_an_empty_queue_returns_immediately(db):
    def run(product):
        raise AssertionError("should never be called")

    result = run_stage(make_stage(run, concurrency=8), limit=100)
    assert result.processed == 0


# ── sequential stays sequential ─────────────────────────────────────────────────


def test_concurrency_one_never_overlaps(many):
    """Icons rely on this: each is a paid image, so they are drawn strictly one at a
    time by explicit decision."""
    peak = 0
    active = 0
    lock = threading.Lock()

    def run(product):
        nonlocal peak, active
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with lock:
            active -= 1

    run_stage(make_stage(run, concurrency=1), limit=100)
    assert peak == 1


def test_both_real_stages_are_threaded():
    """Icons were sequential by an earlier decision; that was revisited once the claim
    framework made parallelism safe and profiling showed ~93% of an icon is spent
    waiting on the API. The source script ran 12-wide, so this restores the original
    design rather than departing from it."""
    from django.conf import settings

    from pipeline.stages.icon_2d import ICON_2D_STAGE

    assert ICON_2D_STAGE.concurrency == settings.ICON_CONCURRENCY > 1


def test_the_ingest_stage_is_threaded():
    from django.conf import settings

    from pipeline.stages.ingest import INGEST_STAGE

    assert INGEST_STAGE.concurrency == settings.INGESTION_CONCURRENCY > 1
