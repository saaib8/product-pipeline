"""The 2D icon stage.

Every external is faked — no model call, no S3, no spend. What's under test is the
ordering, because that's where this stage's real bug lived: generating before reserving
the key left objects in S3 that no row pointed at, and a re-run paid for them again.
"""

from __future__ import annotations

import pytest

from pipeline.clients.icon_generator import IconGenerationError, IconResult
from pipeline.enums import JobStatus, ReviewStatus
from pipeline.models import Product
from pipeline.stages import icon_2d as icon_module
from pipeline.stages.base import claim_one, run_stage
from pipeline.stages.icon_2d import ICON_2D_STAGE, TerminalIconError

pytestmark = pytest.mark.django_db(transaction=True)

APPROVED = ReviewStatus.APPROVED


class FakeImageIO:
    def __init__(self, error: Exception | None = None):
        self.error = error

    def read_from_url(self, url, *a, **k):
        if self.error:
            raise self.error
        from PIL import Image
        return Image.new("RGB", (900, 900), "white")


@pytest.fixture
def fakes(monkeypatch):
    """Record the order of operations, so the sequence itself can be asserted."""
    state = {"calls": [], "gen_error": None, "svg": "<svg/>", "warning": None}

    monkeypatch.setattr(icon_module, "_get_image_io", lambda: FakeImageIO())

    def fake_generate(photo, category, name=None):
        state["calls"].append("generate")
        if state["gen_error"]:
            raise state["gen_error"]
        return IconResult(state["svg"], state["warning"])

    monkeypatch.setattr(icon_module, "generate_icon_svg", fake_generate)
    monkeypatch.setattr(icon_module, "backup_existing",
                        lambda key: state["calls"].append("backup"))
    monkeypatch.setattr(icon_module, "upload_svg",
                        lambda key, svg: state["calls"].append("upload"))
    return state


@pytest.fixture
def ready(product) -> Product:
    """Both reviews passed, icon not yet drawn — exactly what the stage claims."""
    Product.objects.filter(pk=product.pk).update(
        category_status=APPROVED, dimensions_status=APPROVED)
    product.refresh_from_db()
    return product


# ── eligibility ─────────────────────────────────────────────────────────────────


def test_waits_for_both_reviews(product):
    """Category approval alone is not enough any more: an icon is only worth drawing
    for a product that could actually be placed, and placement also needs a signed-off
    measurement. Neither gate on its own releases the row."""
    assert claim_one(ICON_2D_STAGE) is None

    Product.objects.filter(pk=product.pk).update(category_status=APPROVED)
    assert claim_one(ICON_2D_STAGE) is None, "category alone must not release it"

    Product.objects.filter(pk=product.pk).update(
        category_status=ReviewStatus.PENDING, dimensions_status=APPROVED)
    assert claim_one(ICON_2D_STAGE) is None, "dimensions alone must not release it"

    Product.objects.filter(pk=product.pk).update(category_status=APPROVED)
    assert claim_one(ICON_2D_STAGE) is not None


def test_a_rejected_dimension_never_draws(ready):
    """Rejection no longer deactivates the product, so nothing else would stop this."""
    Product.objects.filter(pk=ready.pk).update(dimensions_status=ReviewStatus.REJECTED)
    assert claim_one(ICON_2D_STAGE) is None


def test_categories_outside_the_icon_set_are_never_claimed(ready):
    """A treadmill has no floor-plan icon; drawing one would be spend with no consumer."""
    Product.objects.filter(pk=ready.pk).update(category="treadmill")
    assert claim_one(ICON_2D_STAGE) is None


def test_the_measurements_themselves_are_not_checked(ready):
    """The gate is the reviewer's VERDICT, not the numbers. A reviewer who approves a
    dimension has accepted it; re-deriving that judgement from the columns here would
    let the two disagree."""
    Product.objects.filter(pk=ready.pk).update(length=None, width=None)
    assert claim_one(ICON_2D_STAGE) is not None


def test_inactive_products_are_skipped(ready):
    Product.objects.filter(pk=ready.pk).update(is_active=False)
    assert claim_one(ICON_2D_STAGE) is None


# ── ordering: the orphan bug ────────────────────────────────────────────────────


def test_the_column_is_written_only_after_the_upload(ready, fakes):
    """The column is a receipt, not a reservation.

    It once was written before generation, as orphan protection — but the key is a pure
    function of store + product id, so a crash between upload and the DB write already
    resolves itself: the retry computes the same key and overwrites. All the early write
    achieved was a column asserting a path that held nothing, which reads as "has an
    icon" to anything querying it directly.
    """
    fakes["gen_error"] = RuntimeError("died during generation")

    run_stage(ICON_2D_STAGE, limit=1)

    ready.refresh_from_db()
    assert ready.two_d_icon in ("", None)          # nothing uploaded -> nothing claimed


def test_the_key_does_not_depend_on_the_column(ready, fakes):
    """Which is what makes the late write safe: a retry lands on the same object."""
    from pipeline.clients.storage import icon_key

    run_stage(ICON_2D_STAGE, limit=1)
    ready.refresh_from_db()
    first = ready.two_d_icon

    Product.objects.filter(pk=ready.pk).update(
        icon_2d_status=ReviewStatus.PENDING, two_d_icon="")
    run_stage(ICON_2D_STAGE, limit=1)

    ready.refresh_from_db()
    assert ready.two_d_icon == first == icon_key(ready.store.name_english, ready.pk)


def test_backup_happens_before_upload(ready, fakes):
    """Regeneration overwrites in place, so the previous icon must be copied aside
    first or it is unrecoverable."""
    run_stage(ICON_2D_STAGE, limit=1)
    assert fakes["calls"] == ["generate", "backup", "upload"]


def test_status_moves_last(ready, fakes):
    """A reviewer must never be shown a row pointing at an object that failed to
    upload, so the status is written after the upload succeeds."""
    fakes["gen_error"] = RuntimeError("upstream down")
    run_stage(ICON_2D_STAGE, limit=1)

    ready.refresh_from_db()
    assert ready.icon_2d_status != ReviewStatus.IN_REVIEW
    assert "upload" not in fakes["calls"]


# ── outcomes ────────────────────────────────────────────────────────────────────


def test_success_parks_for_review_not_completed(ready, fakes):
    """Every icon is looked at by a human before any consumer may use it."""
    run_stage(ICON_2D_STAGE, limit=1)

    ready.refresh_from_db()
    assert ready.icon_2d_status == ReviewStatus.IN_REVIEW
    assert ready.two_d_icon == f"2D_icons/demostore/{ready.pk}.svg"


def test_a_completed_icon_is_not_claimed_again(ready, fakes):
    run_stage(ICON_2D_STAGE, limit=1)
    assert claim_one(ICON_2D_STAGE) is None


def test_an_unusable_image_is_terminal(ready, fakes, monkeypatch):
    """It will be just as unusable next time — retrying only repeats the download."""
    monkeypatch.setattr(icon_module, "_get_image_io",
                        lambda: FakeImageIO(error=ValueError("404 not found")))

    run_stage(ICON_2D_STAGE, limit=1)

    ready.refresh_from_db()
    assert ready.icon_2d_status == ReviewStatus.FAILED
    assert claim_one(ICON_2D_STAGE) is None              # never comes back


def test_a_flagged_icon_still_reaches_the_reviewer(ready, fakes):
    """Exhausting the degeneracy retries must NOT discard the image.

    The check is a contrast heuristic: a pale product drawn correctly on white trips it
    exactly as a blank frame does. Failing outright threw away five paid generations and
    left the reviewer nothing to look at, so the last image is uploaded and flagged and
    a human decides.
    """
    fakes["warning"] = "flat/solid fill (content_std=2.57)"

    run_stage(ICON_2D_STAGE, limit=1)

    ready.refresh_from_db()
    assert ready.icon_2d_status == ReviewStatus.IN_REVIEW      # not FAILED
    assert ready.two_d_icon                                     # uploaded
    assert "upload" in fakes["calls"]


def test_the_flag_reason_is_recorded_for_the_reviewer(ready, fakes):
    """A flag with no reason is just an unexplained warning."""
    from pipeline.enums import ArtifactType
    from pipeline.models import ReviewEvent

    fakes["warning"] = "near-empty (content_frac=0.004)"
    run_stage(ICON_2D_STAGE, limit=1)

    event = ReviewEvent.objects.filter(
        product=ready, artifact_type=ArtifactType.ICON_2D).latest("created_at")
    assert event.decision == ReviewStatus.IN_REVIEW
    assert "near-empty" in event.note
    assert event.note.startswith("flagged:")


def test_a_clean_icon_carries_no_flag(ready, fakes):
    from pipeline.enums import ArtifactType
    from pipeline.models import ReviewEvent

    run_stage(ICON_2D_STAGE, limit=1)

    event = ReviewEvent.objects.filter(
        product=ready, artifact_type=ArtifactType.ICON_2D).latest("created_at")
    assert event.note == ""


def test_a_generation_failure_is_terminal(ready, fakes):
    """Matches `regenerate_icons`: the client makes at most 5 attempts, then the caller
    records an error and moves on. No outer ladder."""
    fakes["gen_error"] = IconGenerationError("degenerate output after retries")

    result = run_stage(ICON_2D_STAGE, limit=1)

    ready.refresh_from_db()
    assert ready.icon_2d_status == ReviewStatus.FAILED   # NOT pending
    assert result.retried == 0
    assert claim_one(ICON_2D_STAGE) is None              # never comes back


def test_generation_is_attempted_once_per_product(ready, fakes):
    """The regression this exists for: a stage-level retry on top of the client's five
    attempts meant ten paid calls where the source script makes five."""
    fakes["gen_error"] = IconGenerationError("degenerate output after retries")

    run_stage(ICON_2D_STAGE, limit=1)
    run_stage(ICON_2D_STAGE, limit=1)      # nothing left to claim

    assert fakes["calls"].count("generate") == 1


def test_a_post_generation_failure_still_retries(ready, fakes, monkeypatch):
    """`max_attempts` still governs what happens AFTER generation — an S3 blip is
    genuinely transient, unlike a verdict on the returned image."""
    def boom(key, svg):
        raise RuntimeError("s3 unavailable")

    monkeypatch.setattr(icon_module, "upload_svg", boom)

    result = run_stage(ICON_2D_STAGE, limit=1)

    ready.refresh_from_db()
    assert ready.icon_2d_status == ReviewStatus.PENDING
    assert result.retried == 1
    assert claim_one(ICON_2D_STAGE) is not None


def test_the_failure_reason_is_recorded(ready, fakes):
    """A FAILED row with a blank note is undiagnosable — this stage had that bug."""
    from pipeline.enums import ArtifactType
    from pipeline.models import ReviewEvent

    fakes["gen_error"] = RuntimeError("content policy violation")
    run_stage(ICON_2D_STAGE, limit=1)

    event = ReviewEvent.objects.filter(
        product=ready, artifact_type=ArtifactType.ICON_2D).first()
    assert event is not None
    assert "content policy" in event.note


def test_nothing_is_uploaded_when_generation_fails(ready, fakes):
    fakes["gen_error"] = RuntimeError("boom")
    run_stage(ICON_2D_STAGE, limit=1)
    assert fakes["calls"] == ["generate"]
