"""Serializers for the review API.

Note what is deliberately absent: a blanket ``fields = "__all__"``. Status columns are
never client-writable — a reviewer changes them through a decision endpoint that audits
the change, not by PATCHing a field. (The source system's product serializer is
``__all__``, which is exactly how a merchant could overwrite enrichment.)
"""

from __future__ import annotations

import logging

from rest_framework import serializers

from pipeline.categories import CATEGORIES, resolve, wants_icon
from pipeline.clients.storage import public_url
from pipeline.enums import ArtifactType, ReviewStatus
from pipeline.models import Product, ProductFile, Store

logger = logging.getLogger(__name__)


class StoreSerializer(serializers.ModelSerializer):
    class Meta:
        model = Store
        fields = ("id", "name_english", "name_arabic", "provider")


class FlagsField(serializers.Serializer):
    """Every pipeline flag on one product, so the UI can show pipeline state at a glance."""

    def to_representation(self, product: Product) -> dict:
        return {
            "category_status": product.category_status,
            "dimensions_status": product.dimensions_status,
            "ingestion_status": product.ingestion_status,
            "detection": product.detection,          # None = ingestion hasn't run
            "icon_2d_status": product.icon_2d_status,
            "model_3d_status": product.model_3d_status,
            "has_icon": bool(product.two_d_icon),
            "has_metadata": bool(product.main_color and product.styles),
            "wants_icon": wants_icon(product.category),
        }


class _BaseReviewSerializer(serializers.ModelSerializer):
    """Fields common to both queues."""

    store_name = serializers.CharField(source="store.name_english", read_only=True)
    flags = FlagsField(source="*", read_only=True)

    class Meta:
        model = Product
        fields = (
            "id", "uuid", "store", "store_name",
            "name_english", "name_arabic",
            "image_url", "product_url",
            "category", "is_active",
            "flags", "time_created", "time_updated",
        )
        read_only_fields = fields


class CategoryReviewSerializer(_BaseReviewSerializer):
    """What a reviewer needs to judge a scraped category."""

    allowed_categories = serializers.SerializerMethodField()

    class Meta(_BaseReviewSerializer.Meta):
        fields = _BaseReviewSerializer.Meta.fields + ("allowed_categories",)
        read_only_fields = fields

    def get_allowed_categories(self, _obj) -> list[str]:
        return list(CATEGORIES)


class DimensionReviewSerializer(_BaseReviewSerializer):
    """Everything the category tab shows, plus the measurements under review."""

    class Meta(_BaseReviewSerializer.Meta):
        fields = _BaseReviewSerializer.Meta.fields + (
            "length", "width", "height", "dimension_unit",
        )
        read_only_fields = fields


class IconReviewSerializer(_BaseReviewSerializer):
    """The generated icon alongside the photo it was drawn from.

    Judging an icon means comparing the two, so `icon_url` and `image_url` are both
    required — the stored `two_d_icon` is an S3 key, not something a browser can load.
    """

    icon_url = serializers.SerializerMethodField()
    flagged = serializers.SerializerMethodField()

    class Meta(_BaseReviewSerializer.Meta):
        fields = _BaseReviewSerializer.Meta.fields + ("two_d_icon", "icon_url", "flagged")
        read_only_fields = fields

    def get_flagged(self, product: Product) -> str:
        """Why this icon wants a closer look, if anything.

        Set when the degeneracy heuristic rejected every attempt and the last image was
        handed over anyway. The reviewer needs to know that — a pale product drawn
        correctly trips the same check as a blank frame, and only a human can tell them
        apart.
        """
        event = (product.review_events
                 .filter(artifact_type=ArtifactType.ICON_2D)
                 .order_by("-created_at")
                 .first())
        return event.note if event and event.note.startswith("flagged:") else ""

    def get_icon_url(self, product: Product) -> str:
        if not product.two_d_icon:
            return ""
        # Never let a storage hiccup blank the whole queue: a row with no URL still
        # renders, and the reviewer can see which one is broken.
        try:
            return public_url(product.two_d_icon)
        except Exception:                                   # noqa: BLE001
            logger.warning("could not build icon URL for product %s", product.pk)
            return ""


# ── decisions ───────────────────────────────────────────────────────────────────


class DecisionSerializer(serializers.Serializer):
    """A reviewer's verdict, plus any correction made along the way."""

    decision = serializers.ChoiceField(
        choices=[ReviewStatus.APPROVED, ReviewStatus.REJECTED]
    )
    note = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        return attrs


class CategoryDecisionSerializer(DecisionSerializer):
    """Approve the scraped category, or correct it and approve in one action."""

    category = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_category(self, value: str) -> str:
        if not value:
            return ""
        canonical = resolve(value)
        if canonical is None:
            raise serializers.ValidationError(
                f"{value!r} is not a category the detection model knows."
            )
        # Store the detector's exact spelling, whatever the reviewer typed.
        return canonical

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if attrs["decision"] != ReviewStatus.APPROVED or attrs.get("category"):
            return attrs
        # Approving "as-is" is only valid if what's already there is a category the
        # detector knows. Otherwise the product would sit APPROVED forever while every
        # detection silently fails to match its label.
        current = self.context.get("current_category")
        if not current:
            raise serializers.ValidationError(
                {"category": "This product has no category — pick one to approve it."}
            )
        if resolve(current) is None:
            raise serializers.ValidationError(
                {"category": f"{current!r} is not a category the detection model knows — "
                             f"pick a valid one to approve this product."}
            )
        return attrs


class DimensionDecisionSerializer(DecisionSerializer):
    """Approve the scraped dimensions, or correct them and approve."""

    length = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, allow_null=True)
    width = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, allow_null=True)
    height = serializers.DecimalField(max_digits=10, decimal_places=2, required=False, allow_null=True)
    dimension_unit = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        attrs = super().validate(attrs)
        for field in ("length", "width", "height"):
            value = attrs.get(field)
            if value is not None and value <= 0:
                raise serializers.ValidationError({field: "Must be greater than zero."})
        if attrs["decision"] != ReviewStatus.APPROVED:
            return attrs
        # The layout engine requires positive length AND width; approving without them
        # would create a row that can never be placed.
        merged = {
            k: attrs.get(k) if attrs.get(k) is not None else self.context.get(k)
            for k in ("length", "width")
        }
        if not merged["length"] or not merged["width"]:
            raise serializers.ValidationError(
                {"length": "Length and width must both be positive to approve dimensions."}
            )
        return attrs


class ProductFileSerializer(serializers.ModelSerializer):
    """The upload report.

    Field NAMES here are the API contract and stay stable; the model underneath now
    mirrors `core_productfile` (`file_name`, `uploaded_at`, report inside `logs`), so
    the mapping lives in this one place instead of in the frontend.
    """

    store_name = serializers.CharField(source="store.name_english", read_only=True)
    uploaded_by_name = serializers.CharField(source="uploaded_by.username", read_only=True,
                                             default=None)
    filename = serializers.CharField(source="file_name", read_only=True)
    created_at = serializers.DateTimeField(source="uploaded_at", read_only=True)
    # Derived from `logs`, so they can never disagree with the stored report.
    total_rows = serializers.ReadOnlyField()
    created_count = serializers.ReadOnlyField()
    skipped_count = serializers.ReadOnlyField()
    issues = serializers.ReadOnlyField()

    class Meta:
        model = ProductFile
        fields = ("id", "filename", "store", "store_name", "uploaded_by_name",
                  "file_status", "total_rows", "created_count", "skipped_count",
                  "issues", "created_at")
        read_only_fields = fields
