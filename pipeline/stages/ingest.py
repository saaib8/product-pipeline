"""Ingestion: image → detection → segmentation → embedding → Pinecone.

Ported from the backend's `_ingest_one`, with the algorithm unchanged: download, detect,
keep detections whose label matches the approved category, take the largest, mask the
tight crop, embed the multi-scale crops, upsert under a namespace named for the category.

Four things are deliberately different, each because the original conflates outcomes
that need to be told apart:

**Failures are typed.** The source catches every exception and writes ``detection=False``
— so a 404, a timeout and a genuine non-detection are indistinguishable afterwards. Here
a permanent problem (image too small, unreadable) raises `TerminalIngestionError` and
goes straight to `FAILED`, while a transient one retries.

**Row first, vector second.** The source writes to Pinecone and *then* creates the row,
so a DB failure leaves an orphan vector pointing at a product that does not exist. Here
the row already exists — import created it — so the vector is written last and a
failure leaves nothing behind.

**Undersized images never reach the model.** The measured size is recorded, so the
revisit list is a query rather than a guess.

**A non-detection is a result, not a failure.** ``detection=False`` with
``ingestion_status=COMPLETED`` means the model looked and found nothing; the product
keeps its row and stays eligible for every other stage.
"""

from __future__ import annotations

import io
import logging
import threading

from django.conf import settings
from django.db.models import Q
from PIL import Image

from pipeline.categories import matches
from pipeline.clients.detection import DetectionError, detect
from pipeline.clients.vectors import upsert_vector
from pipeline.enums import ArtifactType, JobStatus, ReviewStatus
from pipeline.models import Product
from pipeline.services.embedding_gemini import GeminiEmbeddingService
from pipeline.services.image_io import ImageIOService
from pipeline.services.preprocessing import PreprocessingService
from pipeline.services.schemas import BBox
from pipeline.services.segmentation_polygon import PolygonSegmentationService
from pipeline.stages.base import Stage
from pipeline.stages.registry import register

logger = logging.getLogger(__name__)


class TerminalIngestionError(Exception):
    """A problem that will recur identically on every retry.

    An image that is 300px wide will be 300px wide next time too, so retrying it twice
    more only repeats the download. These skip the retry ladder.
    """


# ── services ────────────────────────────────────────────────────────────────────
# Built once per process, not per product: the embedding client holds a session and the
# others are stateless. Lazy so importing this module never needs credentials.

_services: tuple | None = None
_services_lock = threading.Lock()


def _get_services():
    """Build the service set once per process — atomically.

    Assignment happens only after EVERY service is constructed. A half-built cache
    would turn one transient failure (a missing key, a cold Gemini client) into a
    permanent `KeyError` for every product that followed, because the "already built?"
    check would pass while the contents were incomplete.
    """
    global _services
    if _services is not None:
        return _services
    with _services_lock:
        if _services is not None:
            return _services
        if not settings.GOOGLE_API_KEY:
            raise RuntimeError("GOOGLE_API_KEY is not configured — cannot embed")
        image_io = ImageIOService(
            min_dimension=1,                       # measured explicitly below, not here
            max_dimension=settings.IMAGE_MAX_DIMENSION,
            max_file_size_mb=settings.IMAGE_MAX_FILE_SIZE_MB,
        )
        preprocessing = PreprocessingService()
        segmentation = PolygonSegmentationService()
        embedding = GeminiEmbeddingService(
            api_key=settings.GOOGLE_API_KEY,
            model=settings.GEMINI_EMBEDDING_MODEL,
            output_dimensionality=settings.PINECONE_DIMENSION,
        )
        embedding.load_sync()
        _services = (image_io, preprocessing, segmentation, embedding)   # all or nothing
        return _services


def _vector_metadata(product: Product) -> dict:
    """The searchable payload stored alongside the vector.

    Matches the existing index exactly. The backend builds this by taking the whole
    sheet row and adding the store's fields, so the shape is: the product's commerce
    columns plus `store` / `store_id` / `countries` / `is_active`.

    Note there is no `product_id`: the vector's own id IS `Product.pinecone_id`, which
    is what maps a hit back to a row. Adding one would be a second, redundant key that
    the live index does not have.

    `None` values and Decimals are handled by the repository — Pinecone rejects nulls
    and cannot serialise Decimal.
    """
    store = product.store
    return {
        "category": product.category,
        "countries": list(store.countries or []),
        "image_url": product.image_url,
        "is_active": product.is_active,
        "name_arabic": product.name_arabic,
        "name_english": product.name_english,
        "price_amount": product.price_amount,
        "price_unit": product.price_unit,
        "product_url": product.product_url,
        "store": store.name_english,
        "store_id": store.id,
    }


def _to_jpeg(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


# ── the stage ───────────────────────────────────────────────────────────────────


def run_ingestion(product: Product) -> None:
    """Ingest one product. Raises to signal failure; the framework handles the status."""
    image_io, preprocessing, segmentation, embedding = _get_services()

    # 1. Fetch. A dead URL is transient (the merchant may fix it); a corrupt file is not.
    try:
        img = image_io.read_from_url(product.image_url)
    except ValueError as exc:
        raise TerminalIngestionError(f"image unusable: {exc}") from exc

    # 2. Measure and gate BEFORE the model is called.
    #    `image_min_dimension` is a FLAG: it is written only when the image fails, so
    #    "is not null" IS the revisit list. A product that passes leaves it null, and a
    #    previously-flagged product whose image the merchant has since replaced gets it
    #    cleared — otherwise the list would keep reporting a problem that is fixed.
    width, height = img.size
    smallest = min(width, height)
    if smallest < settings.IMAGE_MIN_DIMENSION:
        Product.objects.filter(pk=product.pk).update(image_min_dimension=smallest)
        product.image_min_dimension = smallest
        raise TerminalIngestionError(
            f"image too small ({width}x{height}); "
            f"minimum is {settings.IMAGE_MIN_DIMENSION}px"
        )
    if product.image_min_dimension is not None:
        Product.objects.filter(pk=product.pk).update(image_min_dimension=None)
        product.image_min_dimension = None

    # 3. Detect + segment — one round trip; the response carries box AND mask.
    detections = detect(_to_jpeg(img))

    # Label matching is separator-insensitive on both sides: the detector's training
    # data spells a handful of classes with spaces, and without normalising here those
    # classes would silently never match.
    threshold = settings.DETECTION_CONFIDENCE_THRESHOLD
    candidates = [
        d for d in detections
        if matches(d.label, product.category) and d.confidence >= threshold
    ]

    if not candidates:
        # A RESULT, not a failure: the model looked and found nothing of this category.
        # The row stays, no vector is written, and every other stage is unaffected.
        found = sorted({d.label for d in detections})
        logger.info("product %s: no %r detected (saw: %s)",
                    product.pk, product.category, ", ".join(found) or "nothing")
        Product.objects.filter(pk=product.pk).update(detection=False)
        product.detection = False
        return

    # 4. Largest match wins — a product photo may contain several of the same thing.
    best = max(candidates, key=lambda d: d.area)
    if len(candidates) > 1:
        logger.info("product %s: %d %r detected, taking the largest",
                    product.pk, len(candidates), product.category)

    x1, y1, x2, y2 = best.bbox
    bbox = BBox(x1=x1, y1=y1, x2=x2, y2=y2)

    # 5. Crop at several scales, then mask the tight one so the embedding sees the
    #    product rather than its background.
    crops, crop_boxes = preprocessing.crop_base(img, bbox)
    segment = segmentation.segment(img, bbox, mask_polygon=best.mask_polygon)
    crops["tight"] = preprocessing.apply_mask_on_crop(
        crops["tight"], segment.mask, bbox=crop_boxes["tight"]
    )

    # 6. Embed, then index. The vector is written LAST: the row already exists, so a
    #    failure here leaves no orphan — the opposite of the source ordering.
    vector = embedding.embed_crops(crops)
    upsert_vector(
        vector_id=product.pinecone_id,
        vector=vector,
        namespace=product.category,
        metadata=_vector_metadata(product),
    )

    Product.objects.filter(pk=product.pk).update(detection=True)
    product.detection = True


INGEST_STAGE = register(Stage(
    name="ingest",
    artifact=ArtifactType.INGESTION,
    status_field="ingestion_status",
    # Category only — dimensions gate the layout feature at read time, not this.
    eligible=Q(
        category_status__in=(ReviewStatus.APPROVED, ReviewStatus.GRANDFATHERED),
        ingestion_status=JobStatus.PENDING,
        is_active=True,
    ),
    in_progress=JobStatus.IN_PROGRESS,
    on_success=JobStatus.COMPLETED,
    pending=JobStatus.PENDING,
    failed=JobStatus.FAILED,
    run=run_ingestion,
    max_attempts=settings.INGESTION_MAX_ATTEMPTS,
    terminal_errors=(TerminalIngestionError,),
    # Ingestion is almost entirely waiting — Modal, then Gemini, then Pinecone. Threads
    # turn that idle time into throughput, which is what the backend does with its
    # `ThreadPoolExecutor(max_workers=8)`. The difference is where the concurrency sits:
    # there, a whole sheet is fanned out in one process and dying mid-run loses whatever
    # was in flight; here each thread claims its own row, so the same crash strands one
    # row per thread and `sweep_stuck` returns them.
    concurrency=settings.INGESTION_CONCURRENCY,
))
