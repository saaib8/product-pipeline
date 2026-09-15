"""Load sample products into the local database.

Seed data must use REAL product image URLs. Every stage downstream takes an image as
its input — detection, icon generation, metadata — so placeholder images make every
stage fail for reasons that tell you nothing about your code.

Export a small, deliberate sample from the real catalogue and point this at it:

    SELECT name_english, name_arabic, image_url, product_url, category,
           length, width, height, dimension_unit, price_amount, price_unit,
           store_id
    FROM core_product
    WHERE is_active AND image_url <> ''
      AND category IN (...)          -- see fixtures/README for the spread to pick
    LIMIT 100;

Usage:
    python manage.py seed_products                        # bundled demo fixture
    python manage.py seed_products --file export.json     # a real export
    python manage.py seed_products --file export.json --reset
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from pipeline.categories import resolve
from pipeline.importer import _new_pinecone_id
from pipeline.models import Product, Store

DEFAULT_FIXTURE = Path(settings.BASE_DIR) / "fixtures" / "sample_products.json"

REQUIRED = ("name_english", "image_url", "category")


class Command(BaseCommand):
    help = "Seed the local database with sample products (all flags PENDING)."

    def add_arguments(self, parser):
        parser.add_argument("--file", default=str(DEFAULT_FIXTURE),
                            help="JSON array of product dicts.")
        parser.add_argument("--reset", action="store_true",
                            help="Delete existing products/stores first.")

    def handle(self, *args, **opts):
        path = Path(opts["file"])
        if not path.exists():
            raise CommandError(f"Fixture not found: {path}")

        rows: list[dict[str, Any]] = json.loads(path.read_text())
        if not isinstance(rows, list):
            raise CommandError("Fixture must be a JSON array of product objects.")

        if opts["reset"]:
            Product.objects.all().delete()
            Store.objects.all().delete()
            self.stdout.write("reset: products and stores deleted")

        created = skipped = 0
        problems: list[str] = []

        with transaction.atomic():
            for i, row in enumerate(rows, start=1):
                missing = [f for f in REQUIRED if not row.get(f)]
                if missing:
                    problems.append(f"row {i}: missing {', '.join(missing)}")
                    skipped += 1
                    continue

                # Unknown categories are NOT dropped — they land as PENDING with
                # whatever the sheet said, and a reviewer fixes them. Silently skipping
                # is how six categories rotted unnoticed in the source system.
                canonical = resolve(row["category"])

                store, _ = Store.objects.get_or_create(
                    name_english=row.get("store") or "Demo Store",
                    defaults={"provider": row.get("provider", "OTHER")},
                )

                _, was_created = Product.objects.get_or_create(
                    store=store,
                    product_url=row.get("product_url") or "",
                    name_english=row["name_english"],
                    defaults={
                        # Same 12-digit id the importer and the backend generate, so
                        # seeded rows are indistinguishable from imported ones.
                        "pinecone_id": _new_pinecone_id(),
                        "name_arabic": row.get("name_arabic") or row["name_english"],
                        "image_url": row["image_url"],
                        "category": canonical or row["category"],
                        "length": row.get("length"),
                        "width": row.get("width"),
                        "height": row.get("height"),
                        "dimension_unit": row.get("dimension_unit") or "cm",
                        "price_amount": row.get("price_amount") or 0,
                        "price_unit": row.get("price_unit") or "SAR",
                        # Everything starts unreviewed and unprocessed. Nothing is
                        # eligible for any stage until a category is approved.
                    },
                )
                created += was_created
                skipped += not was_created

                if canonical is None:
                    problems.append(
                        f"row {i}: category {row['category']!r} is unknown — queued for review"
                    )

        self.stdout.write(self.style.SUCCESS(f"created {created}, skipped {skipped}"))
        for p in problems:
            self.stdout.write(self.style.WARNING(f"  {p}"))
        self.stdout.write(
            f"\nAll products are PENDING on every flag. Approve a category to make it "
            f"eligible for ingestion, icon and metadata."
        )
