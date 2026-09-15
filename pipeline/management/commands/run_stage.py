"""Drive the pipeline.

The poller IS the trigger. A post-commit push only shortens the wait, so correctness
never depends on it firing — which is why this command, run on a schedule, is enough on
its own:

    */2 * * * *  python manage.py run_stage --all

    python manage.py run_stage --stage ingest      # one stage
    python manage.py run_stage --all --sweep-only  # recovery pass only
"""

from django.core.management.base import BaseCommand, CommandError

from pipeline.stages import registry
from pipeline.stages.base import install_signal_handlers, run_stage, shutdown_requested, sweep_stuck


class Command(BaseCommand):
    help = "Claim and process eligible products for one or all stages."

    def add_arguments(self, parser):
        parser.add_argument("--stage", help=f"one of: {', '.join(registry.names()) or '(none registered)'}")
        parser.add_argument("--all", action="store_true", help="every registered stage")
        parser.add_argument("--limit", type=int, default=None, help="max rows per stage")
        parser.add_argument("--sweep-only", action="store_true",
                            help="recover stranded rows without claiming new work")

    def handle(self, *args, **opts):
        if opts["all"]:
            stages = registry.all_stages()
        elif opts["stage"]:
            try:
                stages = [registry.get(opts["stage"])]
            except KeyError as exc:
                raise CommandError(str(exc)) from exc
        else:
            raise CommandError("pass --stage <name> or --all")

        if not stages:
            self.stdout.write("no stages registered yet")
            return

        # Only the main thread may install these, so it happens here rather than at
        # import time.
        install_signal_handlers()

        for stage in stages:
            if shutdown_requested():
                self.stdout.write(self.style.WARNING("shutdown requested — stopping"))
                break
            # Sweep first: a row stranded by a dead worker should be reclaimable in
            # this same pass, not the next one.
            recovered = sweep_stuck(stage)
            if recovered:
                self.stdout.write(self.style.WARNING(
                    f"{stage.name}: recovered {recovered} stranded row(s)"))
            if opts["sweep_only"]:
                continue
            result = run_stage(stage, limit=opts["limit"])
            style = self.style.SUCCESS if not result.failed else self.style.WARNING
            self.stdout.write(style(str(result)))
