"""Create the Pinecone index if it does not exist.

Deliberately a separate, explicit command rather than something the client does on
connect: auto-creation means a typo in `PINECONE_INDEX_NAME` silently produces a new
empty index instead of failing, and you discover it when search returns nothing.
(The source system auto-creates inside `_build_ingestion_services`.)
"""

import time

from django.conf import settings
from django.core.management.base import BaseCommand
from pinecone import Pinecone, ServerlessSpec


class Command(BaseCommand):
    help = "Ensure the configured Pinecone index exists."

    def add_arguments(self, parser):
        parser.add_argument("--wait", action="store_true",
                            help="block until the index reports Ready")

    def handle(self, *args, **opts):
        name = settings.PINECONE_INDEX_NAME
        pc = Pinecone(api_key=settings.PINECONE_API_KEY)

        existing = {i["name"]: i for i in pc.list_indexes()}
        if name in existing:
            i = existing[name]
            if i.get("dimension") != settings.PINECONE_DIMENSION:
                self.stdout.write(self.style.ERROR(
                    f"{name} exists with dimension {i.get('dimension')}, but this pipeline "
                    f"embeds at {settings.PINECONE_DIMENSION}. Upserts would be rejected."))
                return
            self.stdout.write(f"{name}: already exists (dim {i.get('dimension')}, "
                              f"{i.get('metric')}, {i.get('status', {}).get('state')})")
            return

        self.stdout.write(f"creating {name} — dim {settings.PINECONE_DIMENSION}, cosine, "
                          f"serverless {settings.PINECONE_CLOUD}/{settings.PINECONE_REGION}")
        pc.create_index(
            name=name,
            dimension=settings.PINECONE_DIMENSION,
            metric="cosine",
            spec=ServerlessSpec(cloud=settings.PINECONE_CLOUD, region=settings.PINECONE_REGION),
        )

        if opts["wait"]:
            for _ in range(60):
                state = pc.describe_index(name).get("status", {}).get("state")
                if state == "Ready":
                    break
                time.sleep(2)
            self.stdout.write(self.style.SUCCESS(f"{name}: {state}"))
        else:
            self.stdout.write(self.style.SUCCESS(f"{name}: created (may take ~30s to be Ready)"))
