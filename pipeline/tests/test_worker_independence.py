"""Stages must not block one another.

The bug this pins: the worker used to drain stages one after another in a single pass,
each to exhaustion. Icons are sequential and cost ~35s each by decision, so a queue of
21 of them held ingestion — an 8-wide stage that clears a batch in seconds — for about
twelve minutes. Neither stage was broken; the scheduler was.

What is asserted here is scheduling, not stage internals: a slow stage running must not
stop a fast one from making progress.
"""

from __future__ import annotations

import threading
import time

import pytest
from django.db.models import Q

from pipeline.enums import ArtifactType, JobStatus, ReviewStatus
from pipeline.management.commands.run_worker import Command
from pipeline.models import Product, Store
from pipeline.stages.base import Stage, request_shutdown, reset_shutdown

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def _clean_shutdown():
    reset_shutdown()
    yield
    reset_shutdown()


@pytest.fixture
def products(db) -> list[Product]:
    store = Store.objects.create(name_english="Demo Store", provider="OTHER")
    return [
        Product.objects.create(
            store=store, name_english=f"P{i}", name_arabic="—",
            image_url=f"https://example.invalid/{i}.jpg",
            product_url=f"https://example.invalid/p/{i}",
            category="3-seater-sofa", length=1, width=1, height=1,
            price_amount=1, price_unit="SAR",
            category_status=ReviewStatus.APPROVED,
        )
        for i in range(6)
    ]


def slow_stage(run) -> Stage:
    """Stands in for icon_2d: strictly sequential, expensive per row."""
    return Stage(
        name="slow", artifact=ArtifactType.ICON_2D, status_field="icon_2d_status",
        eligible=Q(icon_2d_status=ReviewStatus.PENDING, is_active=True),
        in_progress=JobStatus.IN_PROGRESS, on_success=ReviewStatus.IN_REVIEW,
        pending=ReviewStatus.PENDING, failed=ReviewStatus.FAILED,
        run=run, concurrency=1,
    )


def fast_stage(run) -> Stage:
    """Stands in for ingest: parallel, quick."""
    return Stage(
        name="fast", artifact=ArtifactType.INGESTION, status_field="ingestion_status",
        eligible=Q(ingestion_status=JobStatus.PENDING, is_active=True),
        run=run, concurrency=8,
    )


def test_a_slow_stage_does_not_hold_up_a_fast_one(products):
    """The regression test. Both loops run at once; the fast stage must finish while
    the slow one is still grinding through its first rows."""
    fast_done = threading.Event()
    slow_started = threading.Event()

    def slow(product):
        slow_started.set()
        time.sleep(0.3)

    def fast(product):
        time.sleep(0.01)

    processed_fast = []

    def fast_wrapped(product):
        fast(product)
        processed_fast.append(product.pk)
        if len(processed_fast) == len(products):
            fast_done.set()

    cmd = Command()
    wake_slow, wake_fast = threading.Event(), threading.Event()

    t_slow = threading.Thread(
        target=cmd._stage_loop, args=(slow_stage(slow), wake_slow, 60), daemon=True)
    t_fast = threading.Thread(
        target=cmd._stage_loop, args=(fast_stage(fast_wrapped), wake_fast, 60), daemon=True)

    t_slow.start()
    slow_started.wait(timeout=5)          # ensure the slow stage is genuinely running
    t_fast.start()

    finished = fast_done.wait(timeout=10)
    request_shutdown()
    wake_slow.set(); wake_fast.set()
    t_slow.join(timeout=10); t_fast.join(timeout=10)

    assert finished, "the fast stage never completed while the slow stage was running"
    assert Product.objects.filter(ingestion_status=JobStatus.COMPLETED).count() == len(products)
    # The slow stage is still mid-queue — proving they were genuinely concurrent.
    assert Product.objects.filter(icon_2d_status=ReviewStatus.PENDING).exists()


def test_each_stage_only_touches_its_own_column(products):
    """Two stages on the same rows at once must not clobber each other's status."""
    def noop(product):
        time.sleep(0.01)

    cmd = Command()
    wakes = [threading.Event(), threading.Event()]
    threads = [
        threading.Thread(target=cmd._stage_loop, args=(slow_stage(noop), wakes[0], 60), daemon=True),
        threading.Thread(target=cmd._stage_loop, args=(fast_stage(noop), wakes[1], 60), daemon=True),
    ]
    for t in threads:
        t.start()

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        done_icons = Product.objects.filter(icon_2d_status=ReviewStatus.IN_REVIEW).count()
        done_ingest = Product.objects.filter(ingestion_status=JobStatus.COMPLETED).count()
        if done_icons == len(products) and done_ingest == len(products):
            break
        time.sleep(0.1)

    request_shutdown()
    for e in wakes:
        e.set()
    for t in threads:
        t.join(timeout=10)

    # Both stages completed every row, and neither overwrote the other's column.
    assert Product.objects.filter(icon_2d_status=ReviewStatus.IN_REVIEW).count() == len(products)
    assert Product.objects.filter(ingestion_status=JobStatus.COMPLETED).count() == len(products)
    assert Product.objects.filter(category_status=ReviewStatus.APPROVED).count() == len(products)


def test_a_wake_event_triggers_an_immediate_drain(products):
    """A notification must shorten the wait, not merely be recorded."""
    seen = threading.Event()

    def run(product):
        seen.set()

    cmd = Command()
    wake = threading.Event()
    # A 3600s interval: anything prompt can only be the wake event.
    t = threading.Thread(target=cmd._stage_loop, args=(fast_stage(run), wake, 3600),
                         daemon=True)
    t.start()

    assert seen.wait(timeout=5), "the first drain never ran"
    seen.clear()

    # New work arrives after the loop has gone to sleep.
    Product.objects.update(ingestion_status=JobStatus.PENDING)
    wake.set()

    assert seen.wait(timeout=5), "the wake event did not trigger a drain"

    request_shutdown()
    wake.set()
    t.join(timeout=10)


def test_work_arriving_mid_drain_is_picked_up_without_waiting(products):
    """Regression: a drain woken by the FIRST row of a batch collapsed to one thread.

    Every thread exits on its first empty claim, so eight threads starting when a single
    row is eligible leaves one survivor to process the rest of the batch serially. The
    stage loop must therefore re-drain while work keeps appearing, instead of waiting out
    the interval — which is what turned six parallel icons into six sequential ones.
    """
    # Only one row eligible when the loop starts; the rest arrive a moment later.
    later = [p.pk for p in products[1:]]
    Product.objects.filter(pk__in=later).update(ingestion_status=JobStatus.COMPLETED)

    seen: list[int] = []
    lock = threading.Lock()
    released = threading.Event()

    def run(product):
        with lock:
            seen.append(product.pk)
        if not released.is_set():
            released.set()
            # The rest of the batch commits while the first drain is still running.
            Product.objects.filter(pk__in=later).update(ingestion_status=JobStatus.PENDING)
        time.sleep(0.05)

    cmd = Command()
    wake = threading.Event()
    # A 3600s interval: anything processed beyond the first row proves the loop
    # re-drained rather than sitting on the wake event.
    t = threading.Thread(target=cmd._stage_loop, args=(fast_stage(run), wake, 3600),
                         daemon=True)
    t.start()

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if Product.objects.filter(ingestion_status=JobStatus.COMPLETED).count() == len(products):
            break
        time.sleep(0.05)

    request_shutdown()
    wake.set()
    t.join(timeout=10)

    assert len(seen) == len(products), (
        f"only {len(seen)} of {len(products)} processed — the loop stopped draining "
        f"while work was still arriving"
    )


def test_stage_filter_selects_one_stage():
    from django.core.management import CommandError

    cmd = Command()
    with pytest.raises(CommandError):
        cmd.handle(stage=["nonexistent"], interval=1, poll_only=True, once=True)
