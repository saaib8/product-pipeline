"""Do N products actually reach the detection model at the same time?

This is the only test that exercises the REAL `INGEST_STAGE` under concurrency.
`test_concurrency.py` builds a synthetic stage, so it proves the *framework* fans out;
this proves the *ingest stage is wired into it*. Without this, a change to
`run_ingestion` that serialised the model call — a module-level lock, a shared client
that is not thread-safe — would leave every other concurrency test green.

Completion order in a log is suggestive but not proof: it could equally come from
sequential calls with varying latency. So this measures the thing directly — the
interval each product spends inside `detect()` — and counts overlapping intervals.
Deliberately NOT a wall-clock assertion, which is the same measurement with a loaded
CI box added as noise.
"""

from __future__ import annotations

import threading
import time

import pytest
from PIL import Image

from pipeline.enums import JobStatus, ReviewStatus
from pipeline.models import Product, Store
from pipeline.stages import ingest as ingest_module
from pipeline.stages.base import run_stage
from pipeline.stages.ingest import INGEST_STAGE

pytestmark = pytest.mark.django_db(transaction=True)

DETECT_SECONDS = 0.4


class FakeImageIO:
    def read_from_url(self, url, *a, **k):
        return Image.new("RGB", (900, 900), "white")


class FakePreprocessing:
    def crop_base(self, img, bbox):
        return {"tight": img, "wide": img}, {"tight": (0, 0, 10, 10)}

    def apply_mask_on_crop(self, crop, mask, bbox=None):
        return crop


class FakeSegmentation:
    def segment(self, img, bbox, mask_polygon=None):
        return type("S", (), {"mask": object()})()


class FakeEmbedding:
    def embed_crops(self, crops):
        return [0.1] * 8


@pytest.fixture
def six_products(db) -> list[Product]:
    store = Store.objects.create(name_english="Demo Store", provider="OTHER")
    products = [
        Product.objects.create(
            store=store, name_english=f"Floor stand {i}", name_arabic="—",
            image_url=f"https://example.invalid/{i}.jpg",
            product_url=f"https://example.invalid/p/{i}",
            category="floor-stand", length=1, width=1, height=1,
            price_amount=1, price_unit="SAR",
            pinecone_id=f"{100000000000 + i}",
        )
        for i in range(6)
    ]
    Product.objects.update(category_status=ReviewStatus.APPROVED,
                           ingestion_status=JobStatus.PENDING)
    return products


@pytest.fixture
def spans(monkeypatch):
    """Record (start, end) for every trip into the detection model."""
    recorded: list[tuple[float, float]] = []
    lock = threading.Lock()

    monkeypatch.setattr(ingest_module, "_get_services", lambda: (
        FakeImageIO(), FakePreprocessing(), FakeSegmentation(), FakeEmbedding(),
    ))
    monkeypatch.setattr(ingest_module, "upsert_vector", lambda **kw: None)

    def fake_detect(image_bytes, **kwargs):
        start = time.monotonic()
        time.sleep(DETECT_SECONDS)               # stand in for the round trip
        end = time.monotonic()
        with lock:
            recorded.append((start, end))
        return []                                # no match: a result, not a failure

    monkeypatch.setattr(ingest_module, "detect", fake_detect)
    return recorded


def max_overlap(spans: list[tuple[float, float]]) -> int:
    """Greatest number of intervals open at any instant."""
    edges = [(s, 1) for s, _ in spans] + [(e, -1) for _, e in spans]
    edges.sort()
    peak = live = 0
    for _, delta in edges:
        live += delta
        peak = max(peak, live)
    return peak


def test_all_six_are_inside_the_model_together(six_products, spans):
    result = run_stage(INGEST_STAGE, limit=50)

    assert result.processed == 6
    assert len(spans) == 6
    assert max_overlap(spans) == 6, (
        f"only {max_overlap(spans)} of 6 detection calls overlapped — "
        f"the stage is not fanning out as configured"
    )


def test_concurrency_caps_the_fan_out(six_products, spans, monkeypatch):
    """More products than threads must queue, not all pile onto the model at once."""
    import dataclasses

    capped = dataclasses.replace(INGEST_STAGE, concurrency=2)
    result = run_stage(capped, limit=50)

    assert result.processed == 6
    assert max_overlap(spans) <= 2, "exceeded the configured thread count"
