"""Metadata: product photo -> `main_color`, `secondary_colors`, `styles`.

Ported from `qwen_product_metadata_colab_DB.ipynb`, keeping only what produces those
three columns: the controlled vocabularies, the prompt, and the validation that maps the
model's answer back onto them. The notebook's Excel/CSV export, Drive mounting, preview
cells, direct psycopg2 access, `STORES`/`CATEGORIES` config and `DRY_RUN`/`ONLY_MISSING`/
`LIMIT`/`COMMIT_EVERY` all fall away — the stage framework and `eligible` do that work.

Four differences from the notebook, each because it was a batch script and this is not:

**No review.** Metadata is machine-final: `metadata_status` is a `JobStatus`, there is no
queue and no reviewer. `COMPLETED` means the columns are written.

**Gated on category approval, scoped to the icon categories.** The notebook additionally
required real dimensions and an existing `two_d_icon`, so metadata could only run on
already-layout-ready products. Here it fans out from category approval and never waits on
the icon stage — but it covers the same 44 categories, because the layout catalog drops
any product without an icon, so metadata for a `treadmill` would never be read.

**An empty answer is retried, not raised.** The model returning nothing usable — or
something off-palette that canonicalisation discards — is a *result*. It gets
`METADATA_MAX_ATTEMPTS` goes and then stops, rather than being retried forever or
failing the row outright.

**Resume is the status column, not `ONLY_MISSING`.** The notebook re-derives what to do
from `main_color` being empty, which cannot express "in progress" and so cannot stop two
runs colliding.
"""

from __future__ import annotations

import io
import logging

from django.conf import settings
from django.db.models import Q

from pipeline.categories import ICON_CATEGORIES
from pipeline.clients.metadata import MetadataError, generate_metadata
from pipeline.enums import ArtifactType, JobStatus, ReviewStatus
from pipeline.models import Product
from pipeline.services.image_io import ImageIOService
from pipeline.services.metadata_prompt import is_incomplete
from pipeline.stages.base import Stage
from pipeline.stages.registry import register

logger = logging.getLogger(__name__)


class TerminalMetadataError(Exception):
    """Will fail identically on every retry — skip the ladder."""


_image_io: ImageIOService | None = None


def _get_image_io() -> ImageIOService:
    global _image_io
    if _image_io is None:
        # No minimum: the size gate belongs to ingestion. A photo too small to embed can
        # still be perfectly readable for "what colour is this sofa".
        _image_io = ImageIOService(
            min_dimension=1,
            max_dimension=settings.IMAGE_MAX_DIMENSION,
            max_file_size_mb=settings.IMAGE_MAX_FILE_SIZE_MB,
        )
    return _image_io


def _to_jpeg(img) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def run_metadata(product: Product) -> str:
    """Generate and store the three columns. Returns a note for the audit row."""
    try:
        img = _get_image_io().read_from_url(product.image_url)
    except ValueError as exc:
        raise TerminalMetadataError(f"image unusable: {exc}") from exc

    # The category names the target object in the prompt — merchant photos are often
    # styled rooms, and a generic 'look at the product' leaves the model to guess
    # which of six things in frame is being sold.
    meta = generate_metadata(_to_jpeg(img), product.category)

    if is_incomplete(meta):
        # Missing main_color or styles. Raised so the retry ladder handles it — two
        # attempts, then the row is left alone rather than being burned on repeatedly.
        # A partial answer is NOT stored: a row with styles but no main_color reads as
        # enriched while being invisible to the colour-family filter.
        missing = [k for k in ("main_color", "styles") if not meta.get(k)]
        raise RuntimeError(f"model returned no usable {' and '.join(missing)}")

    # Written together: a row with styles but no colour would look enriched to
    # `has_metadata` while carrying half an answer.
    Product.objects.filter(pk=product.pk).update(
        main_color=meta["main_color"],
        secondary_colors=meta["secondary_colors"],
        styles=meta["styles"],
    )
    for field, value in meta.items():
        setattr(product, field, value)

    logger.info("product %s: %s / %s / %s", product.pk,
                meta["main_color"] or "—", meta["secondary_colors"] or "—",
                meta["styles"] or "—")
    return ""


METADATA_STAGE = register(Stage(
    name="metadata",
    artifact=ArtifactType.METADATA,
    status_field="metadata_status",
    # Both human gates, and scoped to the SAME 44 categories the icon stage covers.
    # Metadata exists to serve the layout engine, so it is worth generating only for a
    # product the engine could actually place: the catalog drops anything without a
    # `two_d_icon`, and `is_layout_ready` additionally demands an approved dimension.
    # Gating on both means colour and style are never derived for a row whose
    # measurements a reviewer is about to reject.
    eligible=Q(
        category__in=sorted(ICON_CATEGORIES),
        category_status__in=Product.APPROVED_ENOUGH,
        dimensions_status__in=Product.APPROVED_ENOUGH,
        metadata_status=JobStatus.PENDING,
        is_active=True,
    ),
    in_progress=JobStatus.IN_PROGRESS,
    #: Machine-final. No human ever looks at this.
    on_success=JobStatus.COMPLETED,
    pending=JobStatus.PENDING,
    failed=JobStatus.FAILED,
    run=run_metadata,
    max_attempts=settings.METADATA_MAX_ATTEMPTS,
    # A transport failure is retryable; an unreadable image never becomes readable.
    terminal_errors=(TerminalMetadataError,),
    # Time spent waiting on a remote 32B model, exactly like ingestion waits on Modal.
    concurrency=settings.METADATA_CONCURRENCY,
))
