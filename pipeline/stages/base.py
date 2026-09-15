"""The stage framework.

Every stage needs the same five things: find work, take it without another worker
taking the same row, do the job, record the outcome, and recover if the worker dies
mid-job. That machinery lives here once. A stage supplies only two things — *who is
eligible* and *what to do*.

Three guarantees, and the mechanism for each:

**No double-claim.** ``claim_one`` selects with ``FOR UPDATE SKIP LOCKED`` and, in the
same transaction, writes the in-progress status. The row then no longer matches
``eligible``, so once that transaction commits no other worker can find it. Without the
status write the lock would release at commit and the row would be claimable again.

**No lost work.** A failing run puts the row back to ``PENDING``; it is picked up on the
next pass. Only the attempt cap moves it to ``FAILED``.

**No abandoned work on shutdown.** ``SIGTERM``/``SIGINT`` stop the drain *between*
products, never during one. A deploy therefore hands over cleanly instead of leaving a
row ``IN_PROGRESS`` for the sweeper to find half an hour later — after the download,
the detection call and the embedding have all been paid for.

**No stranded rows.** A worker killed mid-job leaves its row ``IN_PROGRESS`` forever —
invisible to the poller (it no longer matches ``eligible``) and to reviewers (it is in
no queue). ``sweep_stuck`` returns those to ``PENDING`` after a timeout. This is not
optional: it is the bug the source system has, where a crashed ingestion leaves
``ProductFile.file_status = 2`` permanently.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from pipeline.enums import JobStatus, ReviewStatus
from pipeline.models import Product, ReviewEvent
from pipeline.transitions import transition

logger = logging.getLogger(__name__)

#: How long an idle thread waits before looking for work again, while a peer is still
#: busy. Short enough that a row approved mid-drain is picked up promptly, long enough
#: that waiting threads are not hammering the database.
_IDLE_POLL_SECONDS = 0.25

#: Consecutive quiet observations before an idle thread concludes the drain is over.
#: More than one, because "no peer is processing" is briefly true while a peer is
#: between claiming a row and recording that it holds it.
_QUIET_ROUNDS_BEFORE_EXIT = 2

# ── graceful shutdown ───────────────────────────────────────────────────────────

_shutdown = threading.Event()


def shutdown_requested() -> bool:
    return _shutdown.is_set()


def request_shutdown() -> None:
    """Ask the drain to stop after the product currently in flight."""
    _shutdown.set()


def reset_shutdown() -> None:
    """For tests, and for a supervisor that reuses the process."""
    _shutdown.clear()


def install_signal_handlers() -> None:
    """Make SIGTERM/SIGINT graceful.

    Call from the main thread (``signal.signal`` refuses anywhere else) — the
    management command does this, not import, so importing the package never changes
    process-wide signal behaviour.

    A second signal exits immediately, so an operator who does not want to wait for a
    16-second Modal call is not stuck.
    """
    def handle(signum, _frame):
        if _shutdown.is_set():
            logger.warning("second signal (%s) — exiting now, current product abandoned", signum)
            raise SystemExit(1)
        request_shutdown()
        logger.warning("signal %s received — finishing the current product, then stopping", signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handle)



@dataclass(frozen=True)
class Stage:
    """One unit of enrichment.

    ``eligible`` is the single source of truth for "is there work here". It is used to
    find rows, and — because claiming changes a column it filters on — to guarantee a
    claimed row is no longer findable.
    """

    name: str
    #: ArtifactType, so transitions land in the audit trail under the right heading.
    artifact: str
    #: The column this stage owns. Only this stage ever writes it.
    status_field: str
    #: Who is ready to run, right now.
    eligible: Q
    #: Value written on claim. MUST make the row fail `eligible`.
    in_progress: str = JobStatus.IN_PROGRESS
    #: Terminal value when `run` returns without raising.
    on_success: str = JobStatus.COMPLETED
    #: Value to return to so a failed row is retried.
    pending: str = JobStatus.PENDING
    #: Value once retries are exhausted.
    failed: str = JobStatus.FAILED
    #: The work itself. Raise to signal failure. May return a note — a caveat about
    #: work that succeeded but wants a human's attention — which is recorded on the
    #: success transition.
    run: Callable[[Product], str | None] = field(default=lambda p: None)
    max_attempts: int = 2
    timeout: timedelta = timedelta(minutes=30)
    #: Exceptions that will recur identically on every retry (an image that is
    #: too small stays too small). These go straight to `failed`.
    terminal_errors: tuple[type[Exception], ...] = ()
    #: Threads used to drain this stage. 1 means strictly sequential.
    #:
    #: Worth raising only for stages that spend their time waiting on a remote call —
    #: ingestion sits blocked on Modal, Gemini and Pinecone, so threads convert dead
    #: wall-clock into throughput. It buys nothing for CPU-bound work, and is left at 1
    #: for icons by explicit decision: each icon is a paid image, and sequential makes
    #: spend easy to reason about.
    #:
    #: Concurrency does NOT weaken any guarantee here — every thread still claims its
    #: own row through `claim_one`, so `SKIP LOCKED` keeps them off each other's work
    #: exactly as it does for separate processes.
    concurrency: int = 1

    def claim_updates(self) -> dict[str, str]:
        return {self.status_field: self.in_progress}


@dataclass
class StageResult:
    stage: str
    processed: int = 0
    succeeded: int = 0
    retried: int = 0
    failed: int = 0

    def __str__(self) -> str:
        return (f"{self.stage}: {self.processed} processed · {self.succeeded} ok · "
                f"{self.retried} retried · {self.failed} failed")


# ── claiming ────────────────────────────────────────────────────────────────────


def claim_one(stage: Stage) -> Product | None:
    """Atomically take one eligible row. None when there is nothing to do.

    ``skip_locked`` is what lets several workers pull concurrently: a worker that finds
    a row already locked is handed the next one instead of waiting behind it.
    """
    with transaction.atomic():
        product = (
            Product.objects.filter(stage.eligible)
            .select_for_update(skip_locked=True)
            .order_by("time_updated", "id")
            .first()
        )
        if product is None:
            return None
        for field_name, value in stage.claim_updates().items():
            setattr(product, field_name, value)
        product.save(update_fields=list(stage.claim_updates()))
        return product


# ── running ─────────────────────────────────────────────────────────────────────


def run_stage(stage: Stage, limit: int | None = None) -> StageResult:
    """Drain up to `limit` eligible rows. Safe to run many of these at once."""
    limit = limit if limit is not None else settings.STAGE_CLAIM_BATCH
    if stage.concurrency > 1:
        return _run_concurrent(stage, limit)

    result = StageResult(stage=stage.name)
    for _ in range(limit):
        # Checked BEFORE claiming: whatever is already in flight runs to completion,
        # and no new row is taken. That is the whole of "graceful".
        if shutdown_requested():
            logger.info("stage %s: shutting down, claimed no further work", stage.name)
            break
        product = claim_one(stage)
        if product is None:
            break
        result.processed += 1
        _apply(result, _process_one(stage, product))
    return result


def _run_concurrent(stage: Stage, limit: int) -> StageResult:
    """Drain with `stage.concurrency` threads.

    Each thread runs the *whole* claim-and-process cycle rather than being handed a
    pre-claimed batch. That distinction matters: a batch claimed up front would be
    stranded wholesale if the process died, whereas here a thread never holds more than
    the single row it is working on.

    Every thread closes its own database connection on the way out. Django connections
    are thread-local, so a thread that opens one and exits without closing leaks it for
    the life of the process — the same reason the backend's `_process_worker` does this
    in its `finally`.
    """
    result = StageResult(stage=stage.name)
    lock = threading.Lock()
    remaining = limit
    processing = 0                              # rows currently being worked on

    def worker() -> None:
        nonlocal remaining, processing
        from django.db import connection

        empty_rounds = 0
        try:
            while True:
                if shutdown_requested():
                    return
                with lock:
                    if remaining <= 0:
                        return
                    remaining -= 1              # reserve a slot BEFORE claiming, so
                                                # `limit` cannot be overshot by threads
                                                # claiming concurrently
                product = claim_one(stage)

                if product is None:
                    # An empty claim is NOT proof the drain is over. A drain woken by the
                    # first approval of a batch starts when one row is eligible: seven of
                    # eight threads would find nothing and exit within milliseconds, and
                    # the survivor would then process the rest of the batch serially —
                    # which is exactly what turned six parallel icons into six sequential
                    # ones. So a thread only gives up once nothing is in flight either;
                    # while a peer is still working, more rows may yet land.
                    with lock:
                        remaining += 1          # nothing taken; hand the slot back
                        busy = processing
                    # `processing` is incremented AFTER a successful claim, so there is a
                    # brief window in which a peer holds a row but has not yet said so.
                    # Requiring two consecutive quiet observations steps over that window
                    # — without it, a whole pool can decide the drain is finished in the
                    # microseconds before the first claimer registers, and collapse to
                    # one thread.
                    empty_rounds = empty_rounds + 1 if busy == 0 else 0
                    if empty_rounds >= _QUIET_ROUNDS_BEFORE_EXIT:
                        return
                    time.sleep(_IDLE_POLL_SECONDS)
                    continue

                empty_rounds = 0
                with lock:
                    processing += 1
                    result.processed += 1
                try:
                    outcome = _process_one(stage, product)
                finally:
                    with lock:
                        processing -= 1
                with lock:
                    _apply(result, outcome)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, name=f"{stage.name}-{i}", daemon=True)
               for i in range(stage.concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return result


def _process_one(stage: Stage, product: Product) -> str:
    """Run one product and record its transition. Returns the outcome name.

    Deliberately returns rather than mutating a result: the threaded drain needs every
    counter touched under one lock, and that is far easier to get right when the only
    place counters move is the caller.

    A stage's `run` may return a note — a caveat about work that succeeded but deserves
    a second look. It is recorded on the success transition, which is how a reviewer
    learns *why* a row wants attention rather than having to infer it from a log.
    """
    try:
        note = stage.run(product) or ""
    except stage.terminal_errors as exc:
        logger.warning("stage %s: %s is permanently unprocessable — %s",
                       stage.name, product.pk, exc)
        transition(product, stage.artifact, stage.failed,
                   status_field=stage.status_field, note=str(exc)[:500])
        return "failed"
    except Exception as exc:  # noqa: BLE001 — one bad row must not stop the drain
        logger.exception("stage %s failed for product %s", stage.name, product.pk)
        return _on_failure(stage, product, exc)
    else:
        transition(product, stage.artifact, stage.on_success,
                   status_field=stage.status_field, note=note)
        return "succeeded"


def _apply(result: StageResult, outcome: str) -> None:
    setattr(result, outcome, getattr(result, outcome) + 1)


def _on_failure(stage: Stage, product: Product, exc: Exception | None = None) -> str:
    """Retry by returning the row to PENDING, or give up at the cap.

    Attempts are counted from the audit trail rather than a column: each retry writes a
    `PENDING` event, so the count and the history can never disagree.

    The reason is recorded on every transition, not just terminal ones — a row that
    exhausts its retries is otherwise undiagnosable from the database, and the log line
    that explained it has usually rotated away by the time anyone looks.
    """
    note = str(exc)[:500] if exc else ""
    if retry_count(product, stage.artifact) + 1 >= stage.max_attempts:
        transition(product, stage.artifact, stage.failed,
                   status_field=stage.status_field, note=note)
        return "failed"
    transition(product, stage.artifact, stage.pending,
               status_field=stage.status_field, note=note)
    return "retried"


def retry_count(product: Product, artifact: str) -> int:
    """How many times this artifact has been put back for another go."""
    return ReviewEvent.objects.filter(
        product=product, artifact_type=artifact, decision=ReviewStatus.PENDING
    ).count()


# ── recovery ────────────────────────────────────────────────────────────────────


def sweep_stuck(stage: Stage) -> int:
    """Return rows abandoned mid-job to PENDING. Number recovered.

    A row is abandoned when it has been `in_progress` longer than the stage's timeout —
    the worker holding it is gone, and nothing else will ever look at it.
    """
    cutoff = timezone.now() - stage.timeout
    stranded = Product.objects.filter(
        **{stage.status_field: stage.in_progress}, time_updated__lt=cutoff
    )
    ids = list(stranded.values_list("pk", flat=True))
    if not ids:
        return 0

    # .update() skips auto_now, so time_updated is set explicitly — otherwise a
    # recovered row keeps its stale timestamp and is swept again immediately.
    Product.objects.filter(pk__in=ids).update(
        **{stage.status_field: stage.pending}, time_updated=timezone.now()
    )
    logger.warning("stage %s: recovered %d stranded row(s)", stage.name, len(ids))
    return len(ids)
