from django.urls import path

from pipeline import auth_views, streaming, views

app_name = "pipeline"

urlpatterns = [
    path("auth/csrf/", auth_views.csrf, name="csrf"),
    path("auth/login/", auth_views.login_view, name="login"),
    path("auth/logout/", auth_views.logout_view, name="logout"),
    path("auth/me/", auth_views.me, name="me"),
    path("events/", streaming.event_stream, name="event-stream"),
    path("review/category/", views.CategoryQueueView.as_view(), name="category-queue"),
    path("review/category/<int:pk>/decide/", views.decide_category, name="category-decide"),
    path("review/dimensions/", views.DimensionQueueView.as_view(), name="dimension-queue"),
    path("review/dimensions/<int:pk>/decide/", views.decide_dimensions, name="dimension-decide"),
    path("review/icons/", views.IconQueueView.as_view(), name="icon-queue"),
    path("review/icons/<int:pk>/decide/", views.decide_icon, name="icon-decide"),
    path("review/counts/", views.queue_counts, name="queue-counts"),
    path("vocabulary/", views.vocabulary, name="vocabulary"),
    path("import/", views.upload_sheet, name="import-upload"),
    path("import/batches/", views.ProductFileListView.as_view(), name="import-batches"),
    path("import/template/", views.sheet_template, name="import-template"),
]
