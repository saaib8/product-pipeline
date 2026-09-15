"""Review API.

Django is API-only; the reviewer UI is the Next.js app in ``frontend/``.

A queue is just a filtered query on the products table. There is no separate work
table, so a product appears the moment its columns satisfy the filter — whether it got
there via import, an API call, or a raw SQL update.
"""

from __future__ import annotations

from django.db.models import Count, Q, QuerySet
from rest_framework import status as http
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.generics import ListAPIView
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from pipeline.categories import CATEGORIES, wants_icon
from pipeline.enums import ArtifactType, JobStatus, ReviewStatus
from pipeline.importer import OPTIONAL_HEADERS, REQUIRED_HEADERS, SheetError, stage_upload
from pipeline.models import Product, ProductFile, Store
from pipeline.serializers import (
    CategoryDecisionSerializer,
    CategoryReviewSerializer,
    DecisionSerializer,
    DimensionDecisionSerializer,
    DimensionReviewSerializer,
    IconReviewSerializer,
    ProductFileSerializer,
    StoreSerializer,
)
from pipeline.transitions import StaleDecision, transition

#: Statuses that still need a human. REJECTED is terminal until something re-queues it.
OPEN = (ReviewStatus.PENDING, ReviewStatus.IN_REVIEW)


#: Statuses a stage has not started on, and so may be freely re-pointed between PENDING
#: and NOT_APPLICABLE. Anything else means work exists that a reviewer or a worker owns.
_ICON_UNSTARTED = (ReviewStatus.PENDING, ReviewStatus.NOT_APPLICABLE)
_META_UNSTARTED = (JobStatus.PENDING, JobStatus.NOT_APPLICABLE)


def _scope_applicability(product: Product, corrected: str | None) -> dict:
    """Mark on approval whether the icon and metadata stages will ever run.

    Both are scoped to the same 44 categories, so one predicate decides both: metadata
    serves the layout engine, and the layout catalog drops any product without an icon.

    Decided from the FINAL category, so correcting `treadmill` to `chair` on the way
    through enables both stages, and correcting the other way disables them.

    Only touched while a stage has not started. Once an icon is drawn that row belongs
    to a reviewer, and no category decision may quietly retire it — the interaction
    where rejecting strands an already-generated icon is a known gap, deliberately not
    widened here.
    """
    in_scope = wants_icon(corrected or product.category)
    changes: dict = {}

    if product.icon_2d_status in _ICON_UNSTARTED:
        want = ReviewStatus.PENDING if in_scope else ReviewStatus.NOT_APPLICABLE
        if product.icon_2d_status != want:
            changes["icon_2d_status"] = want

    if product.metadata_status in _META_UNSTARTED:
        want = JobStatus.PENDING if in_scope else JobStatus.NOT_APPLICABLE
        if product.metadata_status != want:
            changes["metadata_status"] = want

    return changes


def _conflict(exc: StaleDecision) -> Response:
    """409 for a decision that lost a race with another reviewer.

    The current value is returned so the UI can say who won and refresh in place rather
    than making the reviewer guess why their click did nothing.
    """
    return Response(
        {
            "detail": "Someone else already decided this while your page was open.",
            "field": exc.field,
            "current": exc.actual,
        },
        status=http.HTTP_409_CONFLICT,
    )


def category_queue(store_id: str | int | None = None) -> QuerySet[Product]:
    qs = Product.objects.filter(is_active=True, category_status__in=OPEN)
    return _by_store(qs, store_id).select_related("store").order_by("time_created", "id")


def dimension_queue(store_id: str | int | None = None) -> QuerySet[Product]:
    """Independent of the category queue — a reviewer can work either, in any order."""
    qs = Product.objects.filter(is_active=True, dimensions_status__in=OPEN)
    return _by_store(qs, store_id).select_related("store").order_by("time_created", "id")


def icon_queue(store_id: str | int | None = None) -> QuerySet[Product]:
    """Icons waiting on a human — `IN_REVIEW` only.

    Deliberately NOT `OPEN`. For the reviewed-by-hand artifacts, `PENDING` means "no one
    has looked yet"; for icons it means "the worker has not drawn it yet", so there is
    nothing on screen to judge. Showing those would put rows in the queue whose only
    honest action is to wait. This matches what `queue_counts` already counts.
    """
    qs = Product.objects.filter(is_active=True, icon_2d_status=ReviewStatus.IN_REVIEW)
    return _by_store(qs, store_id).select_related("store").order_by("time_updated", "id")


def _by_store(qs: QuerySet[Product], store_id) -> QuerySet[Product]:
    """Store filtering is how the team already thinks about this work."""
    return qs.filter(store_id=store_id) if store_id else qs


class CategoryQueueView(ListAPIView):
    serializer_class = CategoryReviewSerializer

    def get_queryset(self):
        return category_queue(self.request.query_params.get("store"))


class DimensionQueueView(ListAPIView):
    serializer_class = DimensionReviewSerializer

    def get_queryset(self):
        return dimension_queue(self.request.query_params.get("store"))


class IconQueueView(ListAPIView):
    serializer_class = IconReviewSerializer

    def get_queryset(self):
        return icon_queue(self.request.query_params.get("store"))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def decide_category(request, pk: int):
    """Approve (optionally correcting the category first) or reject.

    Approving makes the product eligible for ingestion, icon generation and metadata —
    all three fan out from this one column.

    Rejecting **deactivates the product**, the same way rejecting an icon does. A
    category the reviewer will not accept and will not correct leaves nothing the
    pipeline can do with the row: it can never be ingested (the namespace is the
    category), never drawn, never placed. Leaving it active meant it stayed in the
    dimensions queue, so a second reviewer could spend time measuring a product the
    first had already discarded.

    `is_active` travels in `changes` so the flag and the status move in one transaction
    and the audit row records the prior value — the rejection is reversible from the
    trail alone.
    """
    product = _get(pk)
    if product is None:
        return Response({"detail": "Not found."}, status=http.HTTP_404_NOT_FOUND)

    form = CategoryDecisionSerializer(
        data=request.data, context={"current_category": product.category}
    )
    form.is_valid(raise_exception=True)
    data = form.validated_data

    changes: dict = {}
    if data.get("category"):
        changes["category"] = data["category"]
    if data["decision"] == ReviewStatus.REJECTED:
        changes["is_active"] = False
    else:
        changes.update(_scope_applicability(product, data.get("category")))

    try:
        updated = transition(
            product,
            ArtifactType.CATEGORY,
            data["decision"],
            reviewer=request.user,
            note=data.get("note", ""),
            changes=changes or None,
            expect=OPEN,
        )
    except StaleDecision as exc:
        return _conflict(exc)
    return Response(CategoryReviewSerializer(updated).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def decide_dimensions(request, pk: int):
    """Approve (optionally correcting the dimensions first) or reject.

    Triggers nothing: dimensions gate the layout feature at read time only, so a
    correction here never restarts a stage.
    """
    product = _get(pk)
    if product is None:
        return Response({"detail": "Not found."}, status=http.HTTP_404_NOT_FOUND)

    form = DimensionDecisionSerializer(
        data=request.data,
        context={"length": product.length, "width": product.width},
    )
    form.is_valid(raise_exception=True)
    data = form.validated_data

    changes = {
        field: data[field]
        for field in ("length", "width", "height", "dimension_unit")
        if data.get(field) not in (None, "")
    } or None

    try:
        updated = transition(
            product,
            ArtifactType.DIMENSIONS,
            data["decision"],
            reviewer=request.user,
            note=data.get("note", ""),
            changes=changes,
            expect=OPEN,
        )
    except StaleDecision as exc:
        return _conflict(exc)
    return Response(DimensionReviewSerializer(updated).data)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def decide_icon(request, pk: int):
    """Accept the generated icon, or reject it.

    Rejection is scoped to the icon and nothing else. It used to also set
    `is_active=False`, retiring the product everywhere; that was too broad. A bad icon
    says the drawing is wrong, not that the product is — the row is still a real
    product that is still worth listing and still worth recommending, and the icon is
    the one thing about it that failed.

    So rejecting writes `icon_2d_status` and the audit row, and touches nothing else.
    The consequence is read-time only, via `is_layout_ready`, which requires an
    approved icon: the product simply stops being placeable in a layout. It stays in
    the category and dimension queues and in every stage's `eligible` filter.

    Note this is still a one-way door — `expect` is `IN_REVIEW` only, so a rejected
    icon cannot be re-decided through this endpoint, and there is no regeneration
    ladder. What changed is the blast radius, not the finality.
    """
    product = _get(pk)
    if product is None:
        return Response({"detail": "Not found."}, status=http.HTTP_404_NOT_FOUND)

    form = DecisionSerializer(data=request.data)
    form.is_valid(raise_exception=True)
    data = form.validated_data

    # Approving "nothing" would mark the product icon-complete while no object exists at
    # the key — every downstream consumer would then 404 on it.
    if data["decision"] == ReviewStatus.APPROVED and not product.two_d_icon:
        return Response(
            {"detail": "This product has no generated icon to approve."},
            status=http.HTTP_400_BAD_REQUEST,
        )

    try:
        updated = transition(
            product,
            ArtifactType.ICON_2D,
            data["decision"],
            reviewer=request.user,
            note=data.get("note", ""),
            # Narrower than OPEN: an icon is only decidable once it has been drawn.
            expect=(ReviewStatus.IN_REVIEW,),
        )
    except StaleDecision as exc:
        return _conflict(exc)
    return Response(IconReviewSerializer(updated).data)


def _get(pk: int) -> Product | None:
    return Product.objects.select_related("store").filter(pk=pk).first()


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def queue_counts(request):
    """How much work is waiting — what the UI badges show."""
    store_id = request.query_params.get("store")
    qs = _by_store(Product.objects.filter(is_active=True), store_id)
    return Response(qs.aggregate(
        category=Count("pk", filter=Q(category_status__in=OPEN)),
        dimensions=Count("pk", filter=Q(dimensions_status__in=OPEN)),
        icon_2d=Count("pk", filter=Q(icon_2d_status=ReviewStatus.IN_REVIEW)),
    ))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def vocabulary(request):
    """Reference data the UI needs: valid categories and the store filter list."""
    return Response({
        "categories": list(CATEGORIES),
        "stores": StoreSerializer(Store.objects.filter(active_status=True), many=True).data,
    })


# ── import ──────────────────────────────────────────────────────────────────────


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def upload_sheet(request):
    """Queue an uploaded sheet. Creates NO products.

    Matches the backend's two-phase flow: headers and store are validated while the
    uploader is watching, then the file is stored `PENDING` and a worker creates the
    rows. So this returns **202 with no counts** — they land in `logs` once the worker
    runs, which the upload list shows.

    It has to be split: each row costs one translation call to derive the missing half
    of its bilingual name, so a large sheet cannot be imported inside a request.
    """
    file_obj = request.FILES.get("file")
    if file_obj is None:
        return Response({"detail": "No file uploaded (field name: 'file')."},
                        status=http.HTTP_400_BAD_REQUEST)

    store_id = request.data.get("store")
    store = Store.objects.filter(pk=store_id).first() if store_id else None
    if store is None:
        return Response({"detail": "A valid 'store' id is required."},
                        status=http.HTTP_400_BAD_REQUEST)

    try:
        product_file = stage_upload(file_obj, store=store, uploaded_by=request.user)
    except SheetError as exc:
        return Response({"detail": str(exc)}, status=http.HTTP_400_BAD_REQUEST)

    return Response(ProductFileSerializer(product_file).data,
                    status=http.HTTP_202_ACCEPTED)


class ProductFileListView(ListAPIView):
    """Recent uploads, so a reviewer can see what landed and what was skipped."""

    serializer_class = ProductFileSerializer

    def get_queryset(self):
        qs = ProductFile.objects.select_related("store", "uploaded_by")
        return _by_store(qs, self.request.query_params.get("store"))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def sheet_template(request):
    """The column contract, so the UI can show it before someone uploads."""
    return Response({"required": list(REQUIRED_HEADERS), "optional": list(OPTIONAL_HEADERS)})
