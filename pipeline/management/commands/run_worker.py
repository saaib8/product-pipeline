"""Long-running worker: LISTEN for outbox notifications, fall back to polling.

    python manage.py run_worker                    # every stage, each on its own thread
    python manage.py run_worker --stage ingest     # just one stage (one process per stage)
    python manage.py run_worker --interval 60      # rarer safety poll
    python manage.py run_worker --poll-only        # ignore notifications entirely

**Stages run independently.** Each gets its own thread and its own drain loop, so a slow
stage cannot delay a fast one. That matters here specifically: icons are sequential and
cost ~35s each by deliberate decision, while ingestion is 8-wide and finishes a batch in
seconds. Draining them one after another in a single pass — which is what this used to
do — meant ingestion waited out the entire icon queue before getting a turn.

The loop is shaped so that **the notification is never load-bearing**:

    drain this stage
        |
    wait for a notification, OR for the interval to elapse
        |
    (either way) loop and drain again

A notification means work starts in milliseconds. A notification lost — worker
restarting, connection dropped, Postgres discarding it with nobody listening — costs at
most one interval. Nothing is stranded, because the drain asks the database what is
eligible rather than trusting what it was told.

Two stages touching the same product concurrently is safe: each claims and writes only
the status column it owns, and `transition` takes a row lock, so their writes serialise
without clobbering each other.
"""

from __future__ import annotations

import select
import threading
import time

import psycopg2
import psycopg2.extensions
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from pipeline import outbox
from pipeline.stages import registry
from pipeline.stages.base import (
    install_signal_handlers,
    run_stage,
    shutdown_requested,
    sweep_stuck,
)


def _listen_connection():
    """A dedicated autocommit connection parked on the channel.

    Separate from Django's ORM connection by necessity: it must sit in autocommit and
    block in `select()`, neither of which is compatible with a connection the ORM is
    also using for transactions. Built from Django's own DATABASES entry so it cannot
    drift from the ORM's target.
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
        cur.execute(f"LISTEN {outbox.CHANNEL};")
    return conn


def _sleep_interruptibly(seconds: float) -> None:
    """Sleep in short slices so SIGTERM is noticed promptly."""
    remaining = float(seconds)
    while remaining > 0 and not shutdown_requested():
        step = min(0.5, remaining)
        time.sleep(step)
        remaining -= step


class Command(BaseCommand):
    help = "Run stages continuously and independently, woken by outbox notifications."

    def add_arguments(self, parser):
        parser.add_argument("--stage", action="append", default=None,
                            help="limit to this stage (repeatable). Default: all, "
                                 "each on its own thread.")
        parser.add_argument("--interval", type=int, default=20,
                            help="seconds between safety polls (default 20)")
        parser.add_argument("--poll-only", action="store_true",
                            help="ignore LISTEN/NOTIFY; poll on the interval only")
        parser.add_argument("--once", action="store_true",
                            help="drain once and exit (for cron or tests)")

    def handle(self, *args, **opts):
        try:
            stages = ([registry.get(n) for n in opts["stage"]] if opts["stage"]
                      else registry.all_stages())
        except KeyError as exc:
            raise CommandError(str(exc)) from exc

        if not stages:
            self.stdout.write("no stages registered")
            return

        install_signal_handlers()
        interval = opts["interval"]

        if opts["once"]:
            for stage in stages:
                self._drain(stage)
            self._relay()
            return

        # One wake event per stage. A notification sets them all; each stage decides for
        # itself whether it actually has work, which is what keeps them independent.
        wakes = {stage.name: threading.Event() for stage in stages}

        threads = [
            threading.Thread(target=self._stage_loop, args=(stage, wakes[stage.name], interval),
                             name=f"loop-{stage.name}", daemon=True)
            for stage in stages
        ]
        listener = threading.Thread(target=self._listen_loop,
                                    args=(wakes, interval, opts["poll_only"]),
                                    name="listener", daemon=True)

        self.stdout.write(self.style.SUCCESS(
            f"running {len(stages)} stage(s) independently: "
            f"{', '.join(s.name for s in stages)} · safety poll every {interval}s"))

        for t in threads:
            t.start()
        listener.start()

        try:
            # Joining with a timeout keeps the main thread responsive to signals, which
            # a bare join() on a daemon thread would swallow.
            while any(t.is_alive() for t in threads):
                if shutdown_requested():
                    break
                time.sleep(0.5)
        except KeyboardInterrupt:                       # pragma: no cover
            pass

        for t in threads:
            t.join(timeout=max(interval, 60))

        if shutdown_requested():
            self.stdout.write(self.style.WARNING("shutdown requested — stopped cleanly"))

    # ── per-stage loop ──────────────────────────────────────────────────────────

    def _stage_loop(self, stage, wake: threading.Event, interval: int) -> None:
        """One stage's whole life: drain, wait, repeat. Independent of every other."""
        from django.db import connection

        try:
            while not shutdown_requested():
                # Drain REPEATEDLY while work keeps turning up, rather than once per
                # wake. A drain's threads each exit on their first empty claim, so one
                # that starts the instant the first row lands collapses to a single
                # thread and stays that way for its whole run — the other seven found
                # nothing and went home a millisecond before the rest of the batch was
                # committed. Going again immediately lets the next drain fan out across
                # everything that has since arrived.
                while not shutdown_requested():
                    if not self._drain(stage):
                        break
                if shutdown_requested():
                    break
                # Woken by a notification, or by the interval. Either way, re-check.
                wake.wait(timeout=interval)
                wake.clear()
        finally:
            # Django connections are thread-local; a thread that exits without closing
            # leaks one for the life of the process.
            connection.close()

    def _drain(self, stage) -> int:
        """One drain pass. Returns how many rows it processed."""
        recovered = sweep_stuck(stage)
        if recovered:
            self.stdout.write(self.style.WARNING(
                f"{stage.name}: recovered {recovered} stranded row(s)"))
        result = run_stage(stage)
        if result.processed:
            style = self.style.SUCCESS if not result.failed else self.style.WARNING
            self.stdout.write(style(str(result)))
        return result.processed

    # ── notifications ───────────────────────────────────────────────────────────

    def _relay(self) -> int:
        """Relay anything a crashed `on_commit` left behind."""
        try:
            relayed = outbox.publish_pending()
            if relayed:
                self.stdout.write(f"outbox: relayed {relayed} unpublished event(s)")
            return relayed
        except Exception as exc:                        # noqa: BLE001
            self.stdout.write(self.style.WARNING(f"outbox relay failed: {exc}"))
            return 0

    def _listen_loop(self, wakes: dict[str, threading.Event], interval: int,
                     poll_only: bool) -> None:
        """Wake every stage on a notification, and sweep the outbox as a backstop."""
        from django.db import connection

        listener = None
        if not poll_only:
            try:
                listener = _listen_connection()
                self.stdout.write(self.style.SUCCESS(
                    f"listening on '{outbox.CHANNEL}'"))
            except psycopg2.Error as exc:
                # Degrade to polling rather than refusing to run: the stage loops have
                # their own interval, so the pipeline keeps moving either way.
                self.stdout.write(self.style.WARNING(
                    f"could not LISTEN ({exc}); stages will poll every {interval}s"))

        try:
            while not shutdown_requested():
                self._relay()

                if listener is None:
                    _sleep_interruptibly(min(interval, 5))
                    continue

                try:
                    ready, _, _ = select.select([listener], [], [], min(interval, 5))
                except (psycopg2.Error, OSError) as exc:
                    self.stdout.write(self.style.WARNING(
                        f"listener dropped ({exc}); reconnecting"))
                    try:
                        listener.close()
                    except Exception:                   # noqa: BLE001
                        pass
                    _sleep_interruptibly(min(interval, 5))
                    try:
                        listener = _listen_connection()
                    except psycopg2.Error:
                        listener = None                 # stages keep polling meanwhile
                    continue

                if ready:
                    listener.poll()
                    # Coalesce: ten notifications and one both mean "drain now".
                    while listener.notifies:
                        listener.notifies.pop()
                    for event in wakes.values():
                        event.set()
        finally:
            if listener is not None:
                try:
                    listener.close()
                except Exception:                       # noqa: BLE001
                    pass
            connection.close()
