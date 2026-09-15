"""2D icon generation.

Runs for the 44 categories the icon scripts cover — floor items drawn top-down, wall
items head-on. Everything else is skipped: a `treadmill` has no floor-plan icon, and
generating one would be spend with no consumer.

Order of operations matters and mirrors the source script:

1. generate (retrying transient errors AND degenerate output)
2. **back up** whatever is at the key — regeneration overwrites in place, so the
   previous icon is otherwise unrecoverable
3. upload
4. only then write `two_d_icon` and move to `IN_REVIEW`

The status is written LAST so a reviewer is never shown a row pointing at an object
that failed to upload. The design doc makes the same point in §11: do not set
`IN_REVIEW` until the upload has completed.

Unlike ingestion, the terminal state here is `IN_REVIEW`, not `COMPLETED` — every icon
is looked at by a human before any consumer may use it.

**Retries live in the client, not here.** `regenerate_icons.gen_icon` makes at most five
attempts per product and its caller records an error without retrying; this stage matches
that ceiling by treating `IconGenerationError` as terminal. The distinction that matters:
the retry ladder in `stages/base` exists for transient infrastructure failures, whereas a
rejected generation is a verdict on the returned image and will be reached identically
every time.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db.models import Q

from pipeline.categories import ICON_CATEGORIES
from pipeline.clients.icon_generator import IconGenerationError, generate_icon_svg
from pipeline.clients.storage import backup_existing, icon_key, upload_svg
from pipeline.enums import ArtifactType, JobStatus, ReviewStatus
from pipeline.models import Product
from pipeline.services.image_io import ImageIOService
from pipeline.stages.base import Stage
from pipeline.stages.registry import register

logger = logging.getLogger(__name__)


class TerminalIconError(Exception):
    """Will fail identically on every retry — skip the ladder."""


_image_io: ImageIOService | None = None


def _get_image_io() -> ImageIOService:
    global _image_io
    if _image_io is None:
        # No minimum here: the icon stage is not the size gate. Ingestion owns that
        # decision, and a product can legitimately have an icon without a vector.
        _image_io = ImageIOService(
            min_dimension=1,
            max_dimension=settings.IMAGE_MAX_DIMENSION,
            max_file_size_mb=settings.IMAGE_MAX_FILE_SIZE_MB,
        )
    return _image_io


def run_icon_2d(product: Product) -> None:
    """Generate, back up, upload, and record. Raises to signal failure."""
    try:
        photo = _get_image_io().read_from_url(product.image_url)
    except ValueError as exc:
        raise TerminalIconError(f"image unusable: {exc}") from exc

    from io import BytesIO
    buf = BytesIO()
    photo.convert("RGB").save(buf, format="PNG")

    # The key is a pure function of store + product id, so it does not need to be
    # persisted before it is used — and must not be. Writing it up front once looked
    # like orphan protection, but the determinism already provides that: a crash
    # between upload and the DB write leaves an object at the key this product will
    # use anyway, so the retry overwrites it rather than creating a second one.
    #
    # What reserving DID produce was a column asserting a path that holds nothing,
    # which is indistinguishable from a real icon to anything reading the column
    # directly. The column is written after the upload succeeds, so it means what it
    # says: this object exists.
    key = icon_key(product.store.name_english, product.pk)

    # Deliberately NOT wrapped in a retry. `generate_icon_svg` already owns the whole
    # retry policy — 5 attempts covering transient API errors (exponential backoff) and
    # degenerate output, with non-transient errors raising immediately — which is exactly
    # what `regenerate_icons.gen_icon` does. Adding a stage-level ladder on top doubled
    # it: a product whose output was rejected five times got five more, ten paid calls
    # where the source script makes five. `IconGenerationError` is therefore terminal.
    result = generate_icon_svg(buf.getvalue(), product.category, product.name_english)

    backup_existing(key)          # once only; the first version is the one worth keeping
    upload_svg(key, svg=result.svg)

    # Recorded only now that the object is really there. The stage framework writes the
    # status after this returns, so the ordering end to end is: upload, then the path,
    # then IN_REVIEW — no state in which a reviewer, or anything reading `two_d_icon`,
    # is pointed at something that does not exist.
    Product.objects.filter(pk=product.pk).update(two_d_icon=key)
    product.two_d_icon = key
    logger.info("product %s: icon uploaded to %s%s", product.pk, key,
                f" (flagged: {result.warning})" if result.warning else "")

    # Returned so the framework records it on the transition. This is what puts the
    # reason in front of the reviewer instead of only in a log line.
    return f"flagged: {result.warning}" if result.warning else ""


ICON_2D_STAGE = register(Stage(
    name="icon_2d",
    artifact=ArtifactType.ICON_2D,
    status_field="icon_2d_status",
    # Both human gates, not just the category. An icon is only worth drawing for a
    # product that can actually be placed, and `is_layout_ready` requires an approved
    # dimension as well as an approved icon — so generating before the measurements are
    # signed off spends gpt-image-2 on a product that may never be placeable. Waiting
    # also means the reviewer sees the icon and the dimensions as one settled story.
    eligible=Q(
        category_status__in=Product.APPROVED_ENOUGH,
        dimensions_status__in=Product.APPROVED_ENOUGH,
        category__in=sorted(ICON_CATEGORIES),      # the 44 the prompts cover
        icon_2d_status=ReviewStatus.PENDING,
        is_active=True,
    ),
    in_progress=JobStatus.IN_PROGRESS,
    #: A human decides, so success means "ready to look at", not "done".
    on_success=ReviewStatus.IN_REVIEW,
    pending=ReviewStatus.PENDING,
    failed=ReviewStatus.FAILED,
    run=run_icon_2d,
    max_attempts=settings.ICON_MAX_ATTEMPTS,
    # Generation failures are terminal: the client has already exhausted its own five
    # attempts, and every one of them failed the same way. Retrying the ladder cannot
    # change a verdict that is a threshold on the returned image rather than a flaky
    # condition — it only spends again. What `max_attempts` still governs is everything
    # AFTER generation (the S3 upload), where a retry genuinely can succeed.
    terminal_errors=(TerminalIconError, IconGenerationError),
    # ~93% of an icon's wall-clock is spent waiting on gpt-image-2 (measured: ~32-47s of
    # the ~35-50s total), so threads are the only lever that changes throughput by an
    # order of magnitude — the local imaging work is 2.5s and optimising it is noise.
    # The source script ran this 12-wide; 8 keeps rate-limit backoff a safety net rather
    # than a routine path. Every thread claims its own row, so the spend is still one
    # paid image per product.
    concurrency=settings.ICON_CONCURRENCY,
))
