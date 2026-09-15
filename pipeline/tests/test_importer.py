"""Sheet import.

The contract is the existing backend's: six required headers, everything else
optional. The behavioural differences are deliberate and each has a test — nothing is
silently dropped, duplicates really are skipped, and import triggers no stage.
"""

from __future__ import annotations

import io

import pandas as pd
import pytest
from django.urls import reverse

from pipeline.enums import JobStatus, ReviewStatus
from pipeline.importer import REQUIRED_HEADERS, SheetError, process_file, stage_upload
from pipeline.models import Product, ProductFile

pytestmark = pytest.mark.django_db


def sheet(rows: list[dict], name: str = "upload.xlsx") -> io.BytesIO:
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False)
    buf.seek(0)
    buf.name = name
    return buf


def import_sheet(file_obj, *, store, uploaded_by=None):
    """Stage + process in one call, as the worker does. Returns the ProductFile."""
    pf = stage_upload(file_obj, store=store, uploaded_by=uploaded_by)
    process_file(pf)
    pf.refresh_from_db()
    return pf


def row(**overrides) -> dict:
    base = {
        "product_name": "Milano 3 Seater Sofa",
        "price_amount": 1999,
        "price_unit": "SAR",
        "image_url": "https://example.invalid/img/1.jpg",
        "product_url": "https://example.invalid/p/1",
        "category": "3-seater-sofa",
    }
    base.update(overrides)
    return base


# ── header contract ─────────────────────────────────────────────────────────────


def test_required_headers_match_the_existing_backend():
    assert REQUIRED_HEADERS == (
        "product_name", "price_amount", "price_unit",
        "image_url", "product_url", "category",
    )


def test_missing_header_rejects_the_whole_sheet(store):
    bad = row()
    del bad["category"]
    with pytest.raises(SheetError, match="category"):
        import_sheet(sheet([bad]), store=store)
    assert Product.objects.count() == 0


def test_headers_are_case_and_whitespace_insensitive(store):
    df_rows = [{"Product_Name ": "Sofa", "PRICE_AMOUNT": 10, "price_unit": "SAR",
                "image_url": "https://e.invalid/i.jpg", "product_url": "https://e.invalid/p",
                " Category": "sofa"}]
    batch = import_sheet(sheet(df_rows), store=store)
    assert batch.created_count == 1


# ── flags on import ─────────────────────────────────────────────────────────────


def test_imported_rows_start_pending_on_every_flag(store):
    import_sheet(sheet([row()]), store=store)
    p = Product.objects.get()
    assert p.category_status == ReviewStatus.PENDING
    assert p.dimensions_status == ReviewStatus.PENDING
    assert p.icon_2d_status == ReviewStatus.PENDING
    assert p.ingestion_status == JobStatus.PENDING
    assert p.detection is None          # nothing has looked yet


def test_import_makes_nothing_eligible(store):
    """Import creates rows and stops — no stage may run until a category is approved."""
    import_sheet(sheet([row()]), store=store)
    assert not Product.objects.filter(
        category_status=ReviewStatus.APPROVED, ingestion_status=JobStatus.PENDING
    ).exists()


def test_optional_columns_are_copied_through(store):
    import_sheet(sheet([row(length=220, width=95, height=85, dimension_unit="cm",
                           main_color="Beige", styles="Modern")]),
                 store=store)
    p = Product.objects.get()
    assert str(p.length) == "220.00"
    assert p.main_color == "Beige"
    assert p.styles == "Modern"


def test_an_english_sheet_gets_a_generated_arabic_name(store):
    """The sheet carries ONE `product_name`. The other language is generated, exactly
    as the backend does it — so `name_arabic` is never supplied by the merchant."""
    import_sheet(sheet([row(product_name="Milano Sofa")]), store=store)
    p = Product.objects.get()
    assert p.name_english == "Milano Sofa"        # supplied language kept verbatim
    assert p.name_arabic == "AR::Milano Sofa"     # generated (stubbed in conftest)


def test_an_arabic_sheet_gets_a_generated_english_name(store):
    """Detection is by script, so an Arabic sheet works the other way round."""
    import_sheet(sheet([row(product_name="أريكة ميلانو")]), store=store)
    p = Product.objects.get()
    assert p.name_arabic == "أريكة ميلانو"
    assert p.name_english == "EN::أريكة ميلانو"


def test_every_row_gets_a_unique_pinecone_id(store):
    import_sheet(sheet([row(), row(product_url="https://e.invalid/2")]), store=store)
    ids = list(Product.objects.values_list("pinecone_id", flat=True))
    assert len(ids) == 2 and all(ids) and len(set(ids)) == 2

# ── rows that are not silently dropped ──────────────────────────────────────────


def test_unknown_category_is_kept_and_flagged_not_dropped(store):
    """The source system skips these to a log nobody reads. Here a reviewer sees it."""
    batch = import_sheet(sheet([row(category="garden gnome")]), store=store)
    assert batch.created_count == 1
    assert Product.objects.get().category == "garden gnome"
    assert any(i["problem"] == "unknown_category" for i in batch.issues)


def test_category_is_stored_canonically(store):
    import_sheet(sheet([row(category="Leg Press Machine")]), store=store)
    assert Product.objects.get().category == "leg press machine"


def test_missing_required_value_skips_only_that_row(store):
    batch = import_sheet(sheet([row(), row(product_url="https://e.invalid/2", image_url=None)]),
                         store=store)
    assert (batch.created_count, batch.skipped_count) == (1, 1)
    assert batch.issues[0]["problem"] == "missing_required"


def test_unparseable_price_skips_the_row(store):
    batch = import_sheet(sheet([row(price_amount="ask us")]), store=store)
    assert batch.created_count == 0
    assert batch.issues[0]["problem"] == "bad_price"


# ── duplicates actually skip ────────────────────────────────────────────────────


def test_duplicate_within_the_sheet_is_skipped(store):
    """The source detects this, logs it, then ingests it anyway — a missing `continue`."""
    batch = import_sheet(sheet([row(), row()]), store=store)
    assert (batch.created_count, batch.skipped_count) == (1, 1)
    assert Product.objects.count() == 1


def test_re_uploading_the_same_sheet_creates_nothing(store):
    import_sheet(sheet([row()]), store=store)
    second = import_sheet(sheet([row()]), store=store)
    assert second.created_count == 0
    assert second.skipped_count == 1
    assert Product.objects.count() == 1


# ── endpoint ────────────────────────────────────────────────────────────────────


def test_upload_queues_the_sheet_without_creating_products(api, store):
    """Matches the backend: upload validates and stores, a worker does the rest."""
    res = api.post(reverse("pipeline:import-upload"),
                   {"file": sheet([row(), row(category="garden gnome",
                                             product_url="https://e.invalid/2")]),
                    "store": store.id},
                   format="multipart")
    assert res.status_code == 202
    assert res.json()["file_status"] == 1          # pending
    assert Product.objects.count() == 0            # nothing yet


def test_worker_drains_the_queue_and_writes_the_report(api, store):
    from pipeline.importer import process_pending

    api.post(reverse("pipeline:import-upload"),
             {"file": sheet([row(), row(category="garden gnome",
                                        product_url="https://e.invalid/2")]),
              "store": store.id}, format="multipart")
    process_pending()

    pf = ProductFile.objects.get()
    assert pf.file_status == 4                     # completed
    assert (pf.total_rows, pf.created_count) == (2, 2)
    assert any(i["problem"] == "unknown_category" for i in pf.issues)
    assert Product.objects.count() == 2


def test_upload_requires_a_valid_store(api):
    res = api.post(reverse("pipeline:import-upload"),
                   {"file": sheet([row()]), "store": 99999}, format="multipart")
    assert res.status_code == 400


def test_upload_without_a_file_is_refused(api, store):
    res = api.post(reverse("pipeline:import-upload"), {"store": store.id}, format="multipart")
    assert res.status_code == 400


def test_uploaded_products_appear_in_both_queues(api, store):
    from pipeline.importer import process_pending

    api.post(reverse("pipeline:import-upload"),
             {"file": sheet([row()]), "store": store.id}, format="multipart")
    process_pending()
    counts = api.get(reverse("pipeline:queue-counts")).json()
    assert counts["category"] == 1
    assert counts["dimensions"] == 1


def test_batch_history_is_listed(api, store):
    api.post(reverse("pipeline:import-upload"),
             {"file": sheet([row()], name="june.xlsx"), "store": store.id}, format="multipart")
    listing = api.get(reverse("pipeline:import-batches")).json()
    # Stored under the backend's convention: {store}_{YYYYMMDDHHMMSS}{ext}
    assert listing["results"][0]["filename"].startswith(f"{store.name_english}_")
    assert listing["results"][0]["filename"].endswith(".xlsx")
    assert ProductFile.objects.count() == 1


def test_template_endpoint_exposes_the_contract(api):
    body = api.get(reverse("pipeline:import-template")).json()
    assert body["required"] == list(REQUIRED_HEADERS)
    assert "length" in body["optional"]


# ── pinecone_id ─────────────────────────────────────────────────────────────────


def test_pinecone_id_is_twelve_digits_like_the_backend(store):
    import_sheet(sheet([row()]), store=store)
    pid = Product.objects.get().pinecone_id
    assert len(pid) == 12 and pid.isdigit()


def test_colliding_pinecone_id_is_retried(store, monkeypatch):
    """The backend re-rolls a taken id rather than failing. So do we."""
    import pipeline.importer as importer

    import_sheet(sheet([row()]), store=store)
    taken = Product.objects.get().pinecone_id

    rolls = iter([taken, taken, "999999999999"])   # two collisions, then free
    monkeypatch.setattr(importer.random, "choices",
                        lambda *a, **k: list(next(rolls)))

    import_sheet(sheet([row(product_url="https://e.invalid/2")]), store=store)
    assert Product.objects.filter(pinecone_id="999999999999").exists()


def test_unresolvable_collision_skips_only_that_row(store, monkeypatch):
    """The retry loop can't close the check-then-act race — the UNIQUE column does.
    A row that still collides is skipped and reported, never silently sharing an id."""
    import pipeline.importer as importer

    import_sheet(sheet([row()]), store=store)
    taken = Product.objects.get().pinecone_id
    monkeypatch.setattr(importer.random, "choices", lambda *a, **k: list(taken))

    batch = import_sheet(sheet([row(product_url="https://e.invalid/2")]), store=store)
    assert batch.created_count == 0
    assert batch.skipped_count == 1
    assert batch.issues[0]["problem"] == "duplicate_pinecone_id"
    assert Product.objects.count() == 1        # the original is untouched


# ── credential plumbing ─────────────────────────────────────────────────────────


def test_translation_reads_the_key_through_settings(settings, monkeypatch):
    """Regression: `translation` read `os.environ` directly while everything else went
    through settings. `settings.env()` strips surrounding quotes, so a `.env` line like
    KEY='sk-...' produced a key carrying literal quotes — rejected with a 401 that reads
    exactly like an expired credential, while icon generation kept working.
    """
    import os

    import pipeline.translation as translation

    settings.OPENAI_API_KEY = "stripped-key"
    monkeypatch.setenv("OPENAI_API_KEY", "'quoted-key'")     # what a .env line leaves

    seen = {}

    class FakeClient:
        def __init__(self, api_key):
            seen["api_key"] = api_key
            self.chat = self

        @property
        def completions(self):
            return self

        def create(self, **kw):
            class R:
                choices = [type("C", (), {"message": type("M", (), {"content": "ok"})()})()]
            return R()

    import openai
    monkeypatch.setattr(openai, "OpenAI", FakeClient)

    translation._translate("chair", "Arabic")

    assert seen["api_key"] == "stripped-key", (
        "translation used the raw environment value instead of the settings value"
    )
