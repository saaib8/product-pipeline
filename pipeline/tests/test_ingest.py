"""The ingestion stage.

Everything external is faked: no image is downloaded, no model is called, no vector is
written. What's under test is the decision-making — which outcomes are results, which
are retryable, and which are permanent — because that is precisely what the source
system collapses into a single `detection=False`.
"""

from __future__ import annotations

import pytest
from django.db.models import Q
from PIL import Image

from pipeline.clients.detection import Detection, DetectionError
from pipeline.enums import ArtifactType, JobStatus, ReviewStatus
from pipeline.models import Product, ReviewEvent
from pipeline.stages import ingest as ingest_module
from pipeline.stages.base import claim_one, run_stage
from pipeline.stages.ingest import INGEST_STAGE, run_ingestion

pytestmark = pytest.mark.django_db(transaction=True)

APPROVED = ReviewStatus.APPROVED


# ── fakes ───────────────────────────────────────────────────────────────────────


class FakeImageIO:
    def __init__(self, size=(800, 800), error: Exception | None = None):
        self.size, self.error = size, error
        self.downloads: list[str] = []

    def read_from_url(self, url, *a, **k):
        self.downloads.append(url)
        if self.error:
            raise self.error
        return Image.new("RGB", self.size, "white")


class FakePreprocessing:
    def crop_base(self, img, bbox):
        return {"tight": img, "wide": img}, {"tight": (0, 0, 10, 10)}

    def apply_mask_on_crop(self, crop, mask, bbox=None):
        return crop


class FakeSegment:
    mask = object()


class FakeSegmentation:
    def segment(self, img, bbox, mask_polygon=None):
        return FakeSegment()


class FakeEmbedding:
    def embed_crops(self, crops):
        return [0.1] * 8


@pytest.fixture
def fakes(monkeypatch):
    """Swap every external for a fake, and record what the stage tried to do."""
    state = {
        "image_io": FakeImageIO(),
        "detections": [],
        "detect_error": None,
        "detect_calls": [],
        "upserts": [],
    }

    monkeypatch.setattr(ingest_module, "_get_services", lambda: (
        state["image_io"], FakePreprocessing(), FakeSegmentation(), FakeEmbedding(),
    ))

    def fake_detect(image_bytes, **kwargs):
        state["detect_calls"].append(image_bytes)
        if state["detect_error"]:
            raise state["detect_error"]
        return state["detections"]

    monkeypatch.setattr(ingest_module, "detect", fake_detect)
    monkeypatch.setattr(ingest_module, "upsert_vector",
                        lambda **kw: state["upserts"].append(kw))
    return state


def detection(label="3-seater-sofa", confidence=0.9, bbox=(0, 0, 100, 100)):
    return Detection(label=label, confidence=confidence,
                     bbox=list(bbox), mask_polygon=[[0, 0], [1, 1]])


@pytest.fixture
def approved(product) -> Product:
    Product.objects.filter(pk=product.pk).update(
        category_status=APPROVED, pinecone_id="123456789012")
    product.refresh_from_db()
    return product


# ── eligibility ─────────────────────────────────────────────────────────────────


def test_only_runs_once_the_category_is_approved(product):
    assert claim_one(INGEST_STAGE) is None
    Product.objects.filter(pk=product.pk).update(category_status=APPROVED)
    assert claim_one(INGEST_STAGE) is not None


def test_dimensions_are_irrelevant_to_eligibility(approved):
    """Dimensions gate the layout feature at read time — never this stage."""
    Product.objects.filter(pk=approved.pk).update(
        dimensions_status=ReviewStatus.PENDING, length=None, width=None)
    assert claim_one(INGEST_STAGE) is not None


def test_inactive_products_are_skipped(approved):
    Product.objects.filter(pk=approved.pk).update(is_active=False)
    assert claim_one(INGEST_STAGE) is None


# ── the size gate ───────────────────────────────────────────────────────────────


def test_undersized_image_never_reaches_the_model(approved, fakes, settings):
    settings.IMAGE_MIN_DIMENSION = 400
    fakes["image_io"] = FakeImageIO(size=(300, 900))     # smaller side is 300

    result = run_stage(INGEST_STAGE, limit=1)

    assert fakes["detect_calls"] == []                   # the model was never called
    approved.refresh_from_db()
    assert approved.image_min_dimension == 300           # ...but the size was recorded
    assert approved.ingestion_status == JobStatus.FAILED
    assert result.failed == 1


def test_undersized_image_is_terminal_not_retried(approved, fakes, settings):
    """It will be the same size next time, so retrying only repeats the download."""
    settings.IMAGE_MIN_DIMENSION = 400
    fakes["image_io"] = FakeImageIO(size=(100, 100))

    run_stage(INGEST_STAGE, limit=1)
    approved.refresh_from_db()
    assert approved.ingestion_status == JobStatus.FAILED     # not PENDING
    assert claim_one(INGEST_STAGE) is None                   # never comes back


def test_the_reason_is_recorded_so_the_row_is_diagnosable(approved, fakes, settings):
    settings.IMAGE_MIN_DIMENSION = 400
    fakes["image_io"] = FakeImageIO(size=(120, 120))

    run_stage(INGEST_STAGE, limit=1)
    event = ReviewEvent.objects.filter(
        product=approved, artifact_type=ArtifactType.INGESTION).first()
    assert "too small" in event.note


def test_a_passing_image_leaves_the_flag_unset(approved, fakes, settings):
    """The column is a flag: only a FAILING image sets it, so "is not null" is the
    revisit list without needing to know the threshold."""
    settings.IMAGE_MIN_DIMENSION = 400
    fakes["image_io"] = FakeImageIO(size=(1000, 800))
    fakes["detections"] = [detection()]

    run_stage(INGEST_STAGE, limit=1)
    approved.refresh_from_db()
    assert approved.image_min_dimension is None


def test_the_flag_is_cleared_once_the_image_is_fixed(approved, fakes, settings):
    """A merchant replacing a tiny image must drop off the revisit list, not linger."""
    settings.IMAGE_MIN_DIMENSION = 400
    fakes["image_io"] = FakeImageIO(size=(200, 200))
    run_stage(INGEST_STAGE, limit=1)
    approved.refresh_from_db()
    assert approved.image_min_dimension == 200          # flagged

    # merchant uploads a proper image; the row is reset and re-ingested
    Product.objects.filter(pk=approved.pk).update(ingestion_status=JobStatus.PENDING)
    fakes["image_io"] = FakeImageIO(size=(1024, 1024))
    fakes["detections"] = [detection()]
    run_stage(INGEST_STAGE, limit=1)

    approved.refresh_from_db()
    assert approved.image_min_dimension is None         # off the list


def test_the_revisit_list_is_a_query(approved, fakes, settings):
    settings.IMAGE_MIN_DIMENSION = 400
    fakes["image_io"] = FakeImageIO(size=(250, 250))
    run_stage(INGEST_STAGE, limit=1)

    revisit = Product.objects.filter(image_min_dimension__lt=400)
    assert list(revisit.values_list("pk", flat=True)) == [approved.pk]


# ── detection outcomes ──────────────────────────────────────────────────────────


def test_a_match_is_embedded_and_indexed(approved, fakes):
    fakes["detections"] = [detection(label="3-seater-sofa")]

    run_stage(INGEST_STAGE, limit=1)

    approved.refresh_from_db()
    assert approved.ingestion_status == JobStatus.COMPLETED
    assert approved.detection is True
    assert len(fakes["upserts"]) == 1
    upsert = fakes["upserts"][0]
    assert upsert["vector_id"] == "123456789012"
    assert upsert["namespace"] == "3-seater-sofa"      # namespace IS the category


def test_no_match_is_a_result_not_a_failure(approved, fakes):
    """The model looked and found nothing. The row survives, other stages continue."""
    fakes["detections"] = [detection(label="chair")]    # wrong category

    result = run_stage(INGEST_STAGE, limit=1)

    approved.refresh_from_db()
    assert approved.ingestion_status == JobStatus.COMPLETED   # NOT failed
    assert approved.detection is False
    assert fakes["upserts"] == []                             # nothing indexed
    assert (result.succeeded, result.failed) == (1, 0)


def test_low_confidence_detections_are_ignored(approved, fakes, settings):
    settings.DETECTION_CONFIDENCE_THRESHOLD = 0.5
    fakes["detections"] = [detection(confidence=0.2)]

    run_stage(INGEST_STAGE, limit=1)
    approved.refresh_from_db()
    assert approved.detection is False


def test_label_matching_ignores_separator_style(approved, fakes):
    """A handful of the detector's classes are annotated with spaces. Without
    normalising both sides those classes would silently never match."""
    Product.objects.filter(pk=approved.pk).update(category="leg press machine")
    approved.refresh_from_db()
    fakes["detections"] = [detection(label="leg-press-machine")]   # hyphenated

    run_stage(INGEST_STAGE, limit=1)
    approved.refresh_from_db()
    assert approved.detection is True


def test_the_largest_match_wins(approved, fakes):
    fakes["detections"] = [
        detection(bbox=(0, 0, 10, 10)),        # small
        detection(bbox=(0, 0, 300, 300)),      # large
        detection(bbox=(0, 0, 50, 50)),
    ]
    run_stage(INGEST_STAGE, limit=1)
    approved.refresh_from_db()
    assert approved.detection is True
    assert len(fakes["upserts"]) == 1


# ── transient failure ───────────────────────────────────────────────────────────


def test_a_detection_outage_is_retried(approved, fakes):
    """Unlike an undersized image, this may well succeed next time."""
    fakes["detect_error"] = DetectionError("modal timed out")

    result = run_stage(INGEST_STAGE, limit=1)

    approved.refresh_from_db()
    assert approved.ingestion_status == JobStatus.PENDING
    assert result.retried == 1
    assert claim_one(INGEST_STAGE) is not None       # comes back for another go


def test_retries_stop_at_the_cap(approved, fakes, settings):
    fakes["detect_error"] = DetectionError("still down")
    stage = INGEST_STAGE

    run_stage(stage, limit=1)      # -> PENDING
    run_stage(stage, limit=1)      # -> FAILED (max_attempts=2)

    approved.refresh_from_db()
    assert approved.ingestion_status == JobStatus.FAILED


def test_nothing_is_indexed_when_detection_fails(approved, fakes):
    """The vector is written last, so a failure leaves no orphan pointing at a row
    that was never successfully ingested."""
    fakes["detect_error"] = DetectionError("boom")
    run_stage(INGEST_STAGE, limit=1)
    assert fakes["upserts"] == []
