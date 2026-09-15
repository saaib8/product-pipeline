"""Admin is an operator tool, not the reviewer UI (that's the Next.js app).

Statuses are shown but read-only: every status change must go through `transition()`
so it lands with an audit row. Editing one here would create state with no history.
"""

from django.contrib import admin

from pipeline.models import Product, ReviewEvent, Store


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ("id", "name_english", "provider", "active_status")
    search_fields = ("name_english",)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "id", "name_english", "store", "category",
        "category_status", "dimensions_status", "ingestion_status",
        "icon_2d_status", "is_active",
    )
    list_filter = (
        "category_status", "dimensions_status", "ingestion_status",
        "icon_2d_status", "is_active", "store", "category",
    )
    search_fields = ("name_english", "name_arabic", "external_id", "pinecone_id")
    readonly_fields = (
        "uuid", "category_status", "dimensions_status", "ingestion_status",
        "detection", "icon_2d_status",
        "model_3d_status", "time_created", "time_updated",
    )


@admin.register(ReviewEvent)
class ReviewEventAdmin(admin.ModelAdmin):
    list_display = ("id", "product", "artifact_type", "decision", "reviewer", "created_at")
    list_filter = ("artifact_type", "decision")
    readonly_fields = tuple(f.name for f in ReviewEvent._meta.fields)

    def has_add_permission(self, request):
        return False  # append-only, written by transition()
