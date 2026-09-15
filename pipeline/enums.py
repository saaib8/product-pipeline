"""Status vocabularies.

Two deliberately separate families:

* :class:`ReviewStatus` — artifacts a human signs off on.
* :class:`JobStatus` — machine-only stage state.

Keeping them apart matters operationally: a machine ``FAILED`` is retryable by a
worker, whereas a human ``REJECTED`` is a decision. Collapsing the two would route
GPU jobs to reviewers and reviewer verdicts back to the GPU.

Metadata needs no sign-off, so it carries a :class:`JobStatus` rather than a
:class:`ReviewStatus` — a machine column with no queue and no reviewer. It still needs
*a* column: claiming a row is what stops two workers processing it at once, and a claim
has to write something the eligibility filter reads.
"""

from __future__ import annotations

from django.db import models


class ReviewStatus(models.TextChoices):
    """Human-gated artifacts (category, dimensions, 2D icon, 3D model)."""

    PENDING = "PENDING", "Pending review"
    IN_REVIEW = "IN_REVIEW", "In review"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    FAILED = "FAILED", "Failed"
    #: This artifact will never exist for this product, and that is correct — a
    #: `treadmill` has no floor-plan icon, a `chandelier` hangs off it. Distinct from
    #: PENDING so that PENDING means exactly one thing: queued, and going to happen.
    #: Without it, "how many icons are outstanding?" counts gym equipment forever.
    NOT_APPLICABLE = "NOT_APPLICABLE", "Not applicable"
    #: Trusted because the data was already in production before review existed —
    #: never because a human looked at it. Unused on a greenfield database; it exists
    #: so that migrating real rows later is a data decision, not a schema change.
    GRANDFATHERED = "GRANDFATHERED", "Grandfathered (unreviewed)"


class JobStatus(models.TextChoices):
    """Machine-only stages with no human sign-off."""

    PENDING = "PENDING", "Pending"
    IN_PROGRESS = "IN_PROGRESS", "In progress"
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed"
    #: This stage will never run for this product, and that is correct — metadata is
    #: scoped to the 44 icon categories, so a `treadmill` is out of scope by design.
    #: Distinct from PENDING so that PENDING means exactly one thing: queued.
    NOT_APPLICABLE = "NOT_APPLICABLE", "Not applicable"




class ArtifactType(models.TextChoices):
    """What a :class:`~pipeline.models.ReviewEvent` decision was about."""

    CATEGORY = "category", "Category"
    DIMENSIONS = "dimensions", "Dimensions"
    INGESTION = "ingestion", "Ingestion"
    ICON_2D = "icon_2d", "2D icon"
    METADATA = "metadata", "Metadata"
    MODEL_3D = "model_3d", "3D model"

