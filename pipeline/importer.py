"""Sheet import — DEV / TEST TOOLING.

**Production upload stays in the Zory backend.** It owns the merchant-facing upload
screen, `ProductFile`, and row creation (including the bilingual-name translation).
This module exists so the pipeline can be developed and tested against a local database
without standing up the backend — and so the row-creation contract is written down
somewhere executable.

It mirrors the backend's two-phase flow deliberately, because whatever the backend
produces is what the stages will consume:

    POST /product/  → validate headers + store, save the file, ProductFile(file_status=1), 202
    S3 event → Lambda → mark file_status=2, spawn a thread that creates the rows

Here the S3/Lambda hop is replaced by a claim on ``file_status=1`` — the same pattern
every stage uses, so a pending upload is picked up by a worker rather than by an
external trigger. The upload response is a 202 with no report; the counts land in
``logs`` once the worker has run, exactly as they do in the source system.

Splitting it isn't only for parity: each row costs one ``gpt-4o-mini`` call to generate
the missing half of its bilingual name, so a 1,000-row sheet cannot be imported inside
a request.

The one deliberate behavioural change: **nothing is silently dropped.** The source
system skips rows whose category isn't in its allow-list and writes a line to a log
nobody reads — which is how six categories rotted there unnoticed. Here an unrecognised
category still creates the row; it lands ``PENDING`` and a reviewer fixes it, which is
work they were going to do anyway since every category is reviewed.

Import creates rows and stops. It runs no stage: every flag starts ``PENDING``, and
nothing becomes eligible until a category is approved. That is exactly what the
backend will do once its ingestion call is removed — the row-creation half of
`run_ingestion_for_file`, with the Modal/Gemini/Pinecone half deleted.
"""

from __future__ import annotations

import logging
import random
import string
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import pandas as pd
from django.core.files.base import File
from django.db import IntegrityError, transaction
from django.utils.timezone import now

from pipeline.categories import resolve
from pipeline.models import Product, ProductFile, Store
from pipeline.translation import bilingual_names

logger = logging.getLogger(__name__)

#: Exactly the headers the existing backend requires.
REQUIRED_HEADERS: tuple[str, ...] = (
    "product_name",
    "price_amount",
    "price_unit",
    "image_url",
    "product_url",
    "category",
)

#: Optional columns copied straight through when present. `name_arabic` is NOT here —
#: the backend derives both names from `product_name`, and so do we.
OPTIONAL_HEADERS: tuple[str, ...] = (
    "length", "width", "height", "dimension_unit",
    "main_color", "secondary_colors", "styles", "product_color",
    "salla_product_id",
)

#: Same extensions the backend accepts.
SUPPORTED_EXTENSIONS = (".csv", ".xlsx", ".json")

PENDING, PROCESSING, FAILED, COMPLETED = 1, 2, 3, 4


class SheetError(Exception):
    """The sheet itself is unusable (unreadable, or missing required headers)."""


@dataclass
class ImportReport:
    total_rows: int = 0
    created: int = 0
    skipped: int = 0
    issues: list[dict[str, Any]] = field(default_factory=list)

    def note(self, row: int, problem: str, detail: str = "") -> None:
        self.issues.append({"row": row, "problem": problem, "detail": detail})

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_rows": self.total_rows,
            "created": self.created,
            "skipped": self.skipped,
            "issues": self.issues,
        }


# ── phase 1: upload ─────────────────────────────────────────────────────────────


def read_sheet(file_obj) -> pd.DataFrame:
    """Parse a .csv/.xlsx/.json sheet and verify the header contract."""
    name = (getattr(file_obj, "name", "") or "").lower()
    suffix = Path(name).suffix
    if suffix not in SUPPORTED_EXTENSIONS:
        raise SheetError(
            f"Unsupported file type {suffix or '(none)'}. "
            f"Use one of: {', '.join(SUPPORTED_EXTENSIONS)}."
        )
    try:
        if suffix == ".csv":
            df = pd.read_csv(file_obj)
        elif suffix == ".json":
            df = pd.read_json(file_obj)
        else:
            df = pd.read_excel(file_obj)
    except Exception as exc:  # noqa: BLE001 — surfaced to the uploader verbatim
        raise SheetError(f"Could not read the file: {exc}") from exc

    df.columns = [str(c).strip().lower() for c in df.columns]
    missing = [h for h in REQUIRED_HEADERS if h not in df.columns]
    if missing:
        raise SheetError(f"Missing required column(s): {', '.join(missing)}")
    return df


def stage_upload(file_obj, *, store: Store, uploaded_by=None) -> ProductFile:
    """Validate the sheet and queue it. Creates NO products.

    Mirrors `_validate_file`: headers are checked up front so a broken sheet is
    rejected while the uploader is still watching, then the file is stored under the
    backend's naming convention and left `PENDING` for a worker.
    """
    read_sheet(file_obj)          # raises SheetError; nothing is saved on a bad sheet
    file_obj.seek(0)

    suffix = Path((getattr(file_obj, "name", "") or "").lower()).suffix
    stamped = f"{store.name_english}_{now().strftime('%Y%m%d%H%M%S')}{suffix}"

    # Wrap so a plain file object works as well as an UploadedFile.
    stored = file_obj if hasattr(file_obj, "_committed") else File(file_obj, name=stamped)
    stored.name = stamped

    return ProductFile.objects.create(
        file=stored,
        file_name=stamped,
        store=store,
        uploaded_by=uploaded_by if (uploaded_by and uploaded_by.is_authenticated) else None,
        file_status=PENDING,
    )


# ── phase 2: processing ─────────────────────────────────────────────────────────


def claim_pending() -> ProductFile | None:
    """Atomically take one queued upload. None when there is nothing to do."""
    with transaction.atomic():
        pf = (
            ProductFile.objects.filter(file_status=PENDING)
            .select_for_update(skip_locked=True)
            .order_by("uploaded_at", "id")
            .first()
        )
        if pf is None:
            return None
        pf.file_status = PROCESSING
        pf.save(update_fields=["file_status"])
        return pf


def process_file(product_file: ProductFile) -> ImportReport:
    """Create products from an already-staged sheet, then record the report."""
    try:
        product_file.file.open("rb")
        df = read_sheet(product_file.file)
    except Exception as exc:  # noqa: BLE001
        product_file.logs = {"total_rows": 0, "created": 0, "skipped": 0,
                             "issues": [{"row": 0, "problem": "unreadable", "detail": str(exc)}]}
        product_file.file_status = FAILED
        product_file.save(update_fields=["logs", "file_status"])
        raise
    finally:
        try:
            product_file.file.close()
        except Exception:  # noqa: BLE001
            pass

    report = _import_rows(df, product_file)

    product_file.logs = report.as_dict()
    product_file.file_status = COMPLETED if report.created else FAILED
    product_file.save(update_fields=["logs", "file_status"])
    return report


def process_pending(limit: int = 10) -> list[ImportReport]:
    """Drain queued uploads. This is what a scheduled worker calls."""
    reports = []
    for _ in range(limit):
        pf = claim_pending()
        if pf is None:
            break
        try:
            reports.append(process_file(pf))
        except Exception:  # noqa: BLE001 — one bad sheet must not stop the queue
            logger.exception("import failed for ProductFile %s", pf.pk)
    return reports


def _import_rows(df: pd.DataFrame, product_file: ProductFile) -> ImportReport:
    store = product_file.store
    report = ImportReport(total_rows=int(df.shape[0]))
    seen_urls: set[str] = set()

    for offset, raw in enumerate(df.to_dict("records"), start=2):  # +2: header is row 1
        row = {k: _clean(v) for k, v in raw.items()}

        missing = [h for h in REQUIRED_HEADERS if not row.get(h)]
        if missing:
            report.skipped += 1
            report.note(offset, "missing_required", ", ".join(missing))
            continue

        product_url = str(row["product_url"])

        # Dedupe within the sheet AND against what's stored. The source system detects a
        # duplicate, logs it, and then ingests it anyway (a missing `continue`).
        if product_url in seen_urls:
            report.skipped += 1
            report.note(offset, "duplicate_in_sheet", product_url)
            continue
        if Product.objects.filter(store=store, product_url=product_url).exists():
            report.skipped += 1
            report.note(offset, "already_imported", product_url)
            continue
        seen_urls.add(product_url)

        price = _decimal(row["price_amount"])
        if price is None:
            report.skipped += 1
            report.note(offset, "bad_price", str(row["price_amount"]))
            continue

        raw_category = str(row["category"])
        canonical = resolve(raw_category)
        if canonical is None:
            report.note(offset, "unknown_category", raw_category)   # kept, not dropped

        # One OpenAI call per row: the sheet gives one name, we store both languages.
        name_english, name_arabic = bilingual_names(row["product_name"])

        try:
            with transaction.atomic():
                Product.objects.create(
                    store=store,
                    file=product_file,
                    pinecone_id=_new_pinecone_id(),
                    name_english=name_english,
                    name_arabic=name_arabic,
                    image_url=str(row["image_url"]),
                    product_url=product_url,
                    category=canonical or raw_category,
                    price_amount=price,
                    price_unit=str(row["price_unit"]),
                    length=_decimal(row.get("length")),
                    width=_decimal(row.get("width")),
                    height=_decimal(row.get("height")),
                    dimension_unit=str(row.get("dimension_unit") or "cm"),
                    main_color=row.get("main_color") or None,
                    secondary_colors=row.get("secondary_colors") or None,
                    styles=row.get("styles") or None,
                    product_color=row.get("product_color") or None,
                    salla_product_id=row.get("salla_product_id") or None,
                    # Every flag keeps its model default: PENDING. Import triggers nothing.
                )
        except IntegrityError as exc:
            report.skipped += 1
            report.note(offset, "duplicate_pinecone_id", str(exc)[:120])
            continue
        report.created += 1

    return report


# ── helpers ─────────────────────────────────────────────────────────────────────


#: How many times to re-roll a colliding id before giving up on the row.
_PINECONE_ID_TRIES = 10


def _new_pinecone_id() -> str:
    """A 12-digit vector id — same generator and same retry loop as the backend.

    The loop only closes the common case (an id already stored). It cannot close the
    race between the check and the insert, which is why the column is also UNIQUE
    here: two importers landing on the same id get an IntegrityError rather than
    silently sharing a vector. The caller turns that into a skipped row.
    """
    for _ in range(_PINECONE_ID_TRIES):
        candidate = "".join(random.choices(string.digits, k=12))
        if not Product.objects.filter(pinecone_id=candidate).exists():
            return candidate
    raise IntegrityError(
        f"could not find a free pinecone_id in {_PINECONE_ID_TRIES} attempts"
    )


def _clean(value: Any) -> Any:
    """pandas NaN and whitespace-only cells both mean 'absent'."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if not isinstance(value, (list, dict)) and pd.isna(value):
        return None
    if isinstance(value, str):
        return value.strip() or None
    return value


def _decimal(value: Any) -> Decimal | None:
    value = _clean(value)
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
