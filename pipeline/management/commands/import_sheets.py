"""Drain queued sheet uploads.

`ProductFile.file_status = 1` is the queue. This is the same claim pattern the
enrichment stages use, standing in for the S3-event → Lambda hop the backend relies on.

    python manage.py import_sheets            # drain up to 10
    python manage.py import_sheets --limit 1
"""

from django.core.management.base import BaseCommand

from pipeline.importer import process_pending


class Command(BaseCommand):
    help = "Process uploaded sheets that are still pending."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10)

    def handle(self, *args, **opts):
        reports = process_pending(limit=opts["limit"])
        if not reports:
            self.stdout.write("nothing pending")
            return
        for r in reports:
            self.stdout.write(self.style.SUCCESS(
                f"{r.total_rows} rows · {r.created} created · {r.skipped} skipped"))
            for issue in r.issues:
                self.stdout.write(self.style.WARNING(
                    f"  row {issue['row']}: {issue['problem']} {issue['detail']}"))
