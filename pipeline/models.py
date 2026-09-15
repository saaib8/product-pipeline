"""Pipeline schema.

Mirrors the live `core_store` / `core_product` tables: same column names, same types,
same constraints. The pipeline's own state columns are *added* on top rather than
replacing anything, so pointing this app at the real database later is a set of
`ADD COLUMN`s plus a connection string — not a migration project.

Deviations from the source tables are few and each is called out inline. There are
exactly three: `detection` is nullable, `pinecone_id` is unique, and the enrichment
status columns are new.
"""

from __future__ import annotations

import uuid as uuid_lib

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models

from pipeline.categories import CATEGORY_CHOICES
from pipeline.enums import ArtifactType, JobStatus, ReviewStatus


class Store(models.Model):
    """Mirrors `core_store` (the subset the pipeline needs)."""

    PROVIDER = [
        ("SALLA", "Salla"),
        ("SHOPIFY", "Shopify"),
        ("MAGENTO", "Magento"),
        ("ZID", "Zid"),
        ("WOOCOMMERCE", "WooCommerce"),
        ("OTHER", "Other"),
    ]

    uuid = models.UUIDField(default=uuid_lib.uuid4, editable=False, unique=True)
    name_arabic = models.CharField(max_length=255, null=True, blank=True)
    name_english = models.CharField(max_length=255)
    provider = models.CharField(max_length=255, choices=PROVIDER, default="OTHER")
    prefered_store_name = models.CharField(max_length=255, null=True, blank=True)
    countries = ArrayField(models.CharField(max_length=3), blank=True, default=list)
    active_status = models.BooleanField(default=True)
    salla_merchant_id = models.IntegerField(null=True, blank=True)
    time_created = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    time_updated = models.DateTimeField(auto_now=True, null=True, blank=True)

    class Meta:
        ordering = ("name_english",)

    def __str__(self) -> str:
        return self.name_english


class Product(models.Model):
    """Mirrors `core_product`, plus the pipeline's stage state.

    Nothing here triggers work. A stage becomes eligible purely because this row's
    columns satisfy its predicate, which is what lets a poller, an API call, a
    management command or a raw SQL update all drive the pipeline identically.
    """

    # ── core_product, verbatim ──────────────────────────────────────────────────
    uuid = models.UUIDField(default=uuid_lib.uuid4, editable=False, unique=True)
    name_arabic = models.CharField(max_length=500)
    name_english = models.CharField(max_length=500)
    # DEVIATION: unique. The source assigns this with a check-then-act loop and no
    # constraint, which is a race that can hand two products the same vector id.
    pinecone_id = models.CharField(max_length=255, null=True, blank=True, unique=True)
    price_amount = models.DecimalField(max_digits=10, decimal_places=2)
    price_unit = models.CharField(max_length=255)
    image_url = models.URLField(max_length=2048)
    product_url = models.URLField(max_length=2048)
    # `choices` is Django-side validation only — the column stays a plain varchar, so
    # this remains structurally identical to the source while stopping unknown
    # categories entering through the API.
    category = models.CharField(max_length=255, null=True, blank=True, choices=CATEGORY_CHOICES)
    length = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True,
                                 help_text="Along-wall width, per the layout engine's convention.")
    width = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True,
                                help_text="Into-room depth, per the layout engine's convention.")
    height = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    dimension_unit = models.CharField(max_length=50, null=True, blank=True,
                                      help_text="cm, in, ft — mixed in live data.")
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="products")
    file = models.ForeignKey("ProductFile", null=True, blank=True,
                             on_delete=models.DO_NOTHING, related_name="products",
                             help_text="The uploaded sheet this row came from.")
    is_active = models.BooleanField(default=True)
    # DEVIATION: nullable. The source defaults to False, so its flag cannot tell
    # "the model looked and found nothing" from "nothing has looked yet".
    detection = models.BooleanField(null=True, default=None)
    product_color = models.CharField(max_length=50, null=True, blank=True,
                                     help_text="Legacy hex. Still decoded into a palette "
                                               "family for rows with no named main_color.")
    main_color = models.CharField(max_length=100, null=True, blank=True)
    secondary_colors = models.CharField(max_length=100, null=True, blank=True,
                                        help_text="Comma-separated, as in core_product.")
    styles = models.CharField(max_length=100, null=True, blank=True,
                              help_text="Comma-separated, as in core_product.")
    # Curation overrides. Normally DERIVED downstream (room_types from the placement
    # role, style_tags from `styles`) and a value here WINS — which also freezes the row
    # against future mapping improvements. No stage ever writes these.
    room_types = models.CharField(max_length=100, null=True, blank=True)
    style_tags = models.CharField(max_length=100, null=True, blank=True)
    two_d_icon = models.CharField(max_length=2048, null=True, blank=True,
                                  help_text="S3 key, not a URL.")
    three_d_model = models.CharField(max_length=2048, null=True, blank=True)
    salla_product_id = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    time_created = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    time_updated = models.DateTimeField(auto_now=True, null=True, blank=True, db_index=True)

    # ── NEW: review state ───────────────────────────────────────────────────────
    category_status = models.CharField(
        max_length=16, choices=ReviewStatus.choices, default=ReviewStatus.PENDING, db_index=True
    )
    dimensions_status = models.CharField(
        max_length=16, choices=ReviewStatus.choices, default=ReviewStatus.PENDING, db_index=True
    )

    # ── NEW: ingestion stage ────────────────────────────────────────────────────
    ingestion_status = models.CharField(
        max_length=16, choices=JobStatus.choices, default=JobStatus.PENDING
    )
    #: Smaller side of the source image, in pixels — written ONLY when the image is
    #: below `settings.IMAGE_MIN_DIMENSION`, so "is not null" is exactly the revisit
    #: list. An integer rather than a boolean so the list can be ordered by how far
    #: short each one falls. Below the threshold the model is never called; a product
    #: whose image is later replaced with a larger one has this cleared on re-run.
    image_min_dimension = models.PositiveIntegerField(null=True, blank=True, db_index=True)

    # ── NEW: 2D icon stage ──────────────────────────────────────────────────────
    icon_2d_status = models.CharField(
        max_length=16, choices=ReviewStatus.choices, default=ReviewStatus.PENDING
    )

    # ── NEW: metadata stage ─────────────────────────────────────────────────────
    # Metadata needs no human sign-off, so this is a `JobStatus`, not a `ReviewStatus` —
    # there is no queue, no tab, and no decision to make. It exists purely so the stage
    # can CLAIM a row: `SELECT … FOR UPDATE SKIP LOCKED` only excludes a claimed row from
    # the next worker if the claim writes a column the eligibility filter reads. Deriving
    # doneness from `main_color` alone (as the notebook's ONLY_MISSING does) has no such
    # column, so two workers would generate the same product concurrently and pay twice.
    metadata_status = models.CharField(
        max_length=16, choices=JobStatus.choices, default=JobStatus.PENDING
    )

    # ── NEW: 3D stage (not built yet) ───────────────────────────────────────────
    model_3d_status = models.CharField(
        max_length=16, choices=ReviewStatus.choices, default=ReviewStatus.PENDING
    )


    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["store", "salla_product_id"],
                condition=models.Q(salla_product_id__isnull=False),
                name="uniq_store_salla_product_id",
            ),
        ]
        indexes = [
            # One per stage predicate — without these the poller table-scans each tick.
            models.Index(fields=["category_status", "ingestion_status"], name="idx_stage_ingest"),
            # Icon and metadata gate on BOTH human reviews, so their indexes carry
            # `dimensions_status` too — otherwise the poll matches on two columns and
            # then filters the third row-by-row.
            models.Index(fields=["category_status", "dimensions_status", "icon_2d_status"],
                         name="idx_stage_icon"),
            # Same shape as every other stage: the poll filters on the gating columns
            # AND this stage's own column, so a composite index turns a full table
            # scan every 20 seconds into a lookup.
            models.Index(fields=["category_status", "dimensions_status", "metadata_status"],
                         name="idx_stage_metadata"),
            models.Index(fields=["category_status", "model_3d_status"], name="idx_stage_3d"),
            # Review queues, oldest first.
            models.Index(fields=["category_status", "time_created"], name="idx_q_category"),
            models.Index(fields=["dimensions_status", "time_created"], name="idx_q_dimensions"),
            models.Index(fields=["icon_2d_status", "time_updated"], name="idx_q_icon"),
        ]

    def __str__(self) -> str:
        return self.name_english

    # ── read-time readiness, evaluated per feature (never one global flag) ──────
    APPROVED_ENOUGH = (ReviewStatus.APPROVED, ReviewStatus.GRANDFATHERED)

    @property
    def is_listing_ready(self) -> bool:
        return self.is_active

    @property
    def is_recommendation_ready(self) -> bool:
        return (
            self.category_status in self.APPROVED_ENOUGH
            and self.ingestion_status == JobStatus.COMPLETED
            and self.detection is True
        )

    @property
    def is_layout_ready(self) -> bool:
        return (
            self.is_active
            and self.dimensions_status in self.APPROVED_ENOUGH
            and self.icon_2d_status in self.APPROVED_ENOUGH
            and bool(self.two_d_icon)
            and (self.length or 0) > 0
            and (self.width or 0) > 0
        )


class ReviewEvent(models.Model):
    """Append-only audit of every decision. New table; no source-table equivalent.

    Written in the same transaction as the status change, so the two can never
    disagree. Kept even though assets are overwritten in place at a stable key — the
    decision history is the point, not the bytes.
    """

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="review_events")
    artifact_type = models.CharField(max_length=16, choices=ArtifactType.choices)
    decision = models.CharField(max_length=16, choices=ReviewStatus.choices)
    note = models.TextField(blank=True, default="",
                            help_text="Free-text reason. No predefined codes: with no\n                                       regeneration loop there is nothing to map them to.")
    attempt_no = models.PositiveIntegerField(default=0)
    #: Machine-made transitions (a stage completing) have no reviewer.
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    previous_value = models.JSONField(null=True, blank=True,
                                      help_text="What the reviewer changed, if anything.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["product", "artifact_type", "-created_at"], name="idx_event_lookup"),
        ]

    def __str__(self) -> str:
        return f"{self.artifact_type}:{self.decision} on {self.product_id}"


class OutboxEvent(models.Model):
    """Transactional outbox: "something may now be eligible."

    Written in the SAME transaction as the status change it describes. That is the whole
    point — the alternative, `product.save()` then `publish()`, can crash in between and
    leave a committed state change that nobody was ever told about.

    Two things this is deliberately NOT:

    * **Not the source of truth.** An event means "recheck this product", never "do this
      work". The consumer re-evaluates the stage's `eligible` filter against the row and
      discards the event if it no longer holds — so an event for a product that has since
      been deactivated is simply dropped.
    * **Not the queue.** The status columns remain the queue. This shortens the wait; it
      does not own the work. Losing every row in this table would cost latency only.

    Delivery is at-least-once: publishing marks `published_at` after the notification is
    sent, so a crash in between re-publishes. Consumers are idempotent because claiming
    is a conditional update — a duplicate finds nothing to claim.
    """

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="outbox_events")
    event_type = models.CharField(max_length=32, choices=ArtifactType.choices,
                                  help_text="Which artifact changed.")
    status = models.CharField(max_length=16, help_text="What it changed to.")
    created_at = models.DateTimeField(auto_now_add=True)
    #: NULL until relayed. The unpublished set IS the dispatcher's backlog.
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("created_at", "id")
        indexes = [
            # Partial index: the dispatcher only ever asks for unpublished rows, and this
            # keeps that lookup O(backlog) rather than O(history) as the table grows.
            models.Index(fields=["created_at"], name="idx_outbox_unpublished",
                         condition=models.Q(published_at__isnull=True)),
        ]

    def __str__(self) -> str:
        state = "pending" if self.published_at is None else "published"
        return f"{self.event_type}:{self.status} on {self.product_id} ({state})"


class ProductFile(models.Model):
    """Mirrors `core_productfile`: one uploaded sheet and what came of it.

    The report lives in `logs` rather than in dedicated count columns — same as the
    source table — so this maps across without translation. It matters because import
    is now the ONLY thing an upload does: it can no longer report ingestion outcomes
    (those happen later, per row), so "what did my upload actually do?" is answered here.
    """

    STATUS = [
        (1, "pending"),
        (2, "processing"),
        (3, "failed"),
        (4, "completed"),
    ]

    file = models.FileField(upload_to="product_files/", blank=True)
    file_name = models.CharField(max_length=255, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True
    )
    uploaded_at = models.DateTimeField(auto_now_add=True, null=True, blank=True)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name="product_files")
    file_status = models.IntegerField(choices=STATUS, default=1)
    logs = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ("-uploaded_at",)

    def __str__(self) -> str:
        return f"{self.file_name} ({(self.logs or {}).get('created', 0)} created)"

    # Convenience readers over `logs`, so callers don't reach into the blob.
    @property
    def total_rows(self) -> int:
        return (self.logs or {}).get("total_rows", 0)

    @property
    def created_count(self) -> int:
        return (self.logs or {}).get("created", 0)

    @property
    def skipped_count(self) -> int:
        return (self.logs or {}).get("skipped", 0)

    @property
    def issues(self) -> list:
        return (self.logs or {}).get("issues", [])
