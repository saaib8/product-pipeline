"""The stage framework's guarantees.

These use a FAKE stage whose `run` does nothing but record, raise, or hang. That is the
point: the hard part here is concurrency and recovery, and testing it through a real
stage would need three mocked external services — at which point a concurrency bug and
a mocking bug look identical.

Three guarantees, each with a test that fails loudly if the mechanism regresses:
no double-claim, no lost work, no stranded rows.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from django.db import connection, connections
from django.db.models import Q
from django.utils import timezone

from pipeline.enums import ArtifactType, JobStatus, ReviewStatus
from pipeline.models import Product, ReviewEvent
from pipeline.stages.base import Stage, claim_one, retry_count, run_stage, sweep_stuck

pytestmark = pytest.mark.django_db(transaction=True)


APPROVED = ReviewStatus.APPROVED


def make_stage(run=lambda p: None, **overrides) -> Stage:
    """A stage that behaves exactly like a real one but touches nothing external."""
    defaults = dict(
        name="fake",
        artifact=ArtifactType.INGESTION,
        status_field="ingestion_status",
        eligible=Q(category_status=APPROVED, ingestion_status=JobStatus.PENDING),
        run=run,
    )
    defaults.update(overrides)
    return Stage(**defaults)


@pytest.fixture
def ready(product) -> Product:
    """A product sitting exactly on the fake stage's eligibility predicate."""
    Product.objects.filter(pk=product.pk).update(category_status=APPROVED)
    product.refresh_from_db()
    return product


# ── eligibility ─────────────────────────────────────────────────────────────────


def test_ineligible_rows_are_never_claimed(product):
    """Category not approved -> not eligible -> the stage cannot see it."""
    assert product.category_status == ReviewStatus.PENDING
    assert claim_one(make_stage()) is None


def test_claiming_removes_the_row_from_the_eligible_set(ready):
    """The whole no-double-claim property rests on this: the claim writes a column
    that `eligible` filters on, so a claimed row stops matching."""
    claimed = claim_one(make_stage())
    assert claimed.pk == ready.pk
    assert claimed.ingestion_status == JobStatus.IN_PROGRESS
    assert claim_one(make_stage()) is None          # nothing left to find


# ── guarantee 1: no double-claim ────────────────────────────────────────────────


def test_two_workers_never_claim_the_same_row(ready):
    """Two threads race for one row. Exactly one may win.

    Without SKIP LOCKED the loser would block and then claim the same row; without the
    status write it would find the row eligible again the moment the lock released.
    """
    stage = make_stage()
    claimed: list[int | None] = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()                     # maximise the overlap
        try:
            got = claim_one(stage)
            claimed.append(got.pk if got else None)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sorted(claimed, key=lambda x: (x is None, x)) == [ready.pk, None]


def test_two_workers_split_the_queue_rather_than_colliding(store, product):
    """With several eligible rows, concurrent workers should divide them — not fight
    over the first one and leave the rest."""
    Product.objects.update(category_status=APPROVED)
    for i in range(5):
        Product.objects.create(
            store=store, name_english=f"P{i}", name_arabic=f"P{i}",
            image_url=f"https://e.invalid/{i}.jpg", product_url=f"https://e.invalid/p{i}",
            category="chair", price_amount=1, price_unit="SAR",
            category_status=APPROVED,
        )
    total = Product.objects.filter(category_status=APPROVED,
                                   ingestion_status=JobStatus.PENDING).count()
    stage = make_stage()
    seen: list[int] = []
    lock = threading.Lock()

    def worker():
        try:
            while True:
                got = claim_one(stage)
                if got is None:
                    return
                with lock:
                    seen.append(got.pk)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(seen) == total
    assert len(set(seen)) == total          # every row claimed exactly once


# ── guarantee 2: no lost work ───────────────────────────────────────────────────


def test_success_moves_to_the_terminal_status_and_audits_it(ready):
    result = run_stage(make_stage(), limit=5)
    ready.refresh_from_db()
    assert ready.ingestion_status == JobStatus.COMPLETED
    assert (result.processed, result.succeeded) == (1, 1)
    assert ReviewEvent.objects.filter(product=ready).count() == 1


def test_a_failing_run_returns_the_row_for_another_go(ready):
    """A raise must not strand the row: it goes back to PENDING and is eligible again."""
    def boom(_p):
        raise RuntimeError("modal timed out")

    result = run_stage(make_stage(run=boom, max_attempts=3), limit=1)
    ready.refresh_from_db()
    assert ready.ingestion_status == JobStatus.PENDING
    assert (result.retried, result.failed) == (1, 0)
    assert claim_one(make_stage()) is not None       # genuinely retryable


def test_retries_stop_at_the_cap(ready):
    def boom(_p):
        raise RuntimeError("nope")

    stage = make_stage(run=boom, max_attempts=2)
    run_stage(stage, limit=1)                        # attempt 1 -> PENDING
    run_stage(stage, limit=1)                        # attempt 2 -> FAILED

    ready.refresh_from_db()
    assert ready.ingestion_status == JobStatus.FAILED
    assert claim_one(make_stage()) is None           # no longer eligible


def test_attempts_are_counted_from_the_audit_trail(ready):
    """No attempts column: the count comes from the events, so the two cannot drift."""
    def boom(_p):
        raise RuntimeError("nope")

    assert retry_count(ready, ArtifactType.INGESTION) == 0
    run_stage(make_stage(run=boom, max_attempts=5), limit=1)
    assert retry_count(ready, ArtifactType.INGESTION) == 1


def test_one_bad_row_does_not_stop_the_drain(store, product):
    """A stage processing many rows must survive a failure in the middle of the batch."""
    Product.objects.update(category_status=APPROVED)
    good = Product.objects.create(
        store=store, name_english="Good", name_arabic="Good",
        image_url="https://e.invalid/g.jpg", product_url="https://e.invalid/g",
        category="chair", price_amount=1, price_unit="SAR", category_status=APPROVED,
    )

    def boom_on_first(p):
        if p.pk == product.pk:
            raise RuntimeError("bad row")

    result = run_stage(make_stage(run=boom_on_first, max_attempts=5), limit=2)
    good.refresh_from_db()
    assert result.processed == 2
    assert good.ingestion_status == JobStatus.COMPLETED     # the healthy row still ran


def test_a_failing_row_goes_to_the_back_of_the_queue(store, product):
    """Claims are ordered by `time_updated`, so a retry must bump it. Otherwise one
    permanently-failing row sits at the front and starves everything behind it."""
    Product.objects.update(category_status=APPROVED)
    waiting = Product.objects.create(
        store=store, name_english="Waiting", name_arabic="Waiting",
        image_url="https://e.invalid/w.jpg", product_url="https://e.invalid/w",
        category="chair", price_amount=1, price_unit="SAR", category_status=APPROVED,
    )

    def boom_on_first(p):
        if p.pk == product.pk:
            raise RuntimeError("poison row")

    stage = make_stage(run=boom_on_first, max_attempts=99)
    run_stage(stage, limit=1)                # claims the oldest (product), fails, requeues
    product.refresh_from_db()
    assert product.ingestion_status == JobStatus.PENDING     # eligible again...
    assert claim_one(stage).pk == waiting.pk                 # ...but behind the other row


# ── guarantee 3: no stranded rows ───────────────────────────────────────────────


def test_a_killed_worker_strands_a_row(ready):
    """Establishes the failure the sweeper exists for: a claimed row that never
    finishes is invisible to the poller AND to reviewers — gone, with no error."""
    claim_one(make_stage())
    ready.refresh_from_db()
    assert ready.ingestion_status == JobStatus.IN_PROGRESS
    assert claim_one(make_stage()) is None              # no poller will find it again


def test_sweeper_recovers_a_stranded_row(ready):
    stage = make_stage(timeout=timedelta(minutes=30))
    claim_one(stage)

    # Simulate the worker having died half an hour ago.
    Product.objects.filter(pk=ready.pk).update(
        time_updated=timezone.now() - timedelta(minutes=31))

    assert sweep_stuck(stage) == 1
    ready.refresh_from_db()
    assert ready.ingestion_status == JobStatus.PENDING
    assert claim_one(stage) is not None                 # reclaimable


def test_sweeper_leaves_rows_that_are_still_running(ready):
    """A worker mid-job must not have its row taken away underneath it."""
    stage = make_stage(timeout=timedelta(minutes=30))
    claim_one(stage)
    assert sweep_stuck(stage) == 0
    ready.refresh_from_db()
    assert ready.ingestion_status == JobStatus.IN_PROGRESS


def test_recovered_row_is_not_immediately_swept_again(ready):
    """`.update()` bypasses auto_now, so the sweeper must stamp time_updated itself —
    otherwise a recovered row keeps its stale timestamp and is swept in a loop."""
    stage = make_stage(timeout=timedelta(minutes=30))
    claim_one(stage)
    Product.objects.filter(pk=ready.pk).update(
        time_updated=timezone.now() - timedelta(minutes=31))

    sweep_stuck(stage)
    claim_one(stage)                                    # a fresh worker picks it up
    assert sweep_stuck(stage) == 0                      # and is left alone


# ── guarantee 4: no abandoned work on shutdown ──────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_shutdown_flag():
    """The flag is process-wide; leaking it would silently disable later tests."""
    from pipeline.stages.base import reset_shutdown
    reset_shutdown()
    yield
    reset_shutdown()


def test_shutdown_finishes_the_product_in_flight(store, product):
    """A SIGTERM mid-run must not abandon the row: the work already paid for
    (download, detection, embedding) is completed and recorded."""
    from pipeline.stages.base import request_shutdown

    Product.objects.update(category_status=APPROVED)
    started: list[int] = []

    def run_then_signal(p):
        started.append(p.pk)
        request_shutdown()          # the signal arrives DURING this product

    result = run_stage(make_stage(run=run_then_signal), limit=10)

    assert len(started) == 1
    finished = Product.objects.get(pk=started[0])
    assert finished.ingestion_status == JobStatus.COMPLETED    # not left IN_PROGRESS
    assert result.succeeded == 1


def test_shutdown_claims_no_further_work(store, product):
    """Everything not yet claimed stays PENDING and is picked up by the next worker."""
    from pipeline.stages.base import request_shutdown

    Product.objects.update(category_status=APPROVED)
    for i in range(3):
        Product.objects.create(
            store=store, name_english=f"Q{i}", name_arabic=f"Q{i}",
            image_url=f"https://e.invalid/{i}.jpg", product_url=f"https://e.invalid/q{i}",
            category="chair", price_amount=1, price_unit="SAR", category_status=APPROVED,
        )

    def run_then_signal(p):
        request_shutdown()

    result = run_stage(make_stage(run=run_then_signal), limit=10)

    assert result.processed == 1                       # stopped after the first
    remaining = Product.objects.filter(
        category_status=APPROVED, ingestion_status=JobStatus.PENDING).count()
    assert remaining == 3                              # untouched, still claimable


def test_nothing_is_left_in_progress_after_a_shutdown(store, product):
    """The state that the sweeper exists to clean up should not arise from a
    planned shutdown at all."""
    from pipeline.stages.base import request_shutdown

    Product.objects.update(category_status=APPROVED)
    run_stage(make_stage(run=lambda p: request_shutdown()), limit=10)

    assert not Product.objects.filter(ingestion_status=JobStatus.IN_PROGRESS).exists()


def test_shutdown_flag_does_not_leak_into_the_next_run(ready):
    """A supervisor that reuses the process must get a working drain afterwards."""
    from pipeline.stages.base import request_shutdown, reset_shutdown

    request_shutdown()
    assert run_stage(make_stage(), limit=5).processed == 0

    reset_shutdown()
    assert run_stage(make_stage(), limit=5).processed == 1


def test_a_retryable_failure_records_its_reason(ready):
    """A row that exhausts its retries must say WHY, in the database.

    Without this the only explanation is a log line, which has usually rotated away by
    the time anyone looks at the row — leaving a FAILED product and no way to tell a
    network blip from a permanently broken image.
    """
    def boom(_p):
        raise RuntimeError("connection reset by peer")

    stage = make_stage(run=boom, max_attempts=2)
    run_stage(stage, limit=1)          # attempt 1 -> PENDING, with a reason
    run_stage(stage, limit=1)          # attempt 2 -> FAILED, with a reason

    notes = list(ReviewEvent.objects.filter(product=ready)
                 .order_by("created_at").values_list("decision", "note"))
    assert [d for d, _ in notes] == [ReviewStatus.PENDING, ReviewStatus.FAILED]
    assert all("connection reset by peer" in n for _, n in notes)
