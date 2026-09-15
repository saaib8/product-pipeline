from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from pipeline.models import Product, Store


@pytest.fixture
def store(db) -> Store:
    return Store.objects.create(name_english="Demo Store", provider="OTHER")


@pytest.fixture
def reviewer(db):
    return get_user_model().objects.create_user(username="reviewer", password="x")


@pytest.fixture
def product(db, store) -> Product:
    """A freshly imported product: every flag PENDING, nothing processed."""
    return Product.objects.create(
        store=store,
        name_english="Milano 3 Seater Sofa",
        name_arabic="أريكة ميلانو",
        image_url="https://example.invalid/img/1.jpg",
        product_url="https://example.invalid/p/1",
        category="3-seater-sofa",
        length=220, width=95, height=85, dimension_unit="cm",
        price_amount=1999, price_unit="SAR",
    )


@pytest.fixture
def api(reviewer):
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_authenticate(reviewer)
    return client


@pytest.fixture(autouse=True)
def no_translation(monkeypatch):
    """Names are derived with one gpt-4o-mini call per row. Tests must never make it.

    The stub mirrors the real contract: the supplied language is kept verbatim and the
    other side is marked, so tests can assert which way round the detection went.
    """
    import pipeline.importer as importer

    def fake(product_name: str) -> tuple[str, str]:
        from pipeline.translation import is_arabic

        name = str(product_name).strip()
        return (f"EN::{name}", name) if is_arabic(name) else (name, f"AR::{name}")

    monkeypatch.setattr(importer, "bilingual_names", fake)
