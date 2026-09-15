"""`backup_existing` — round trips, and what must never be clobbered.

Two S3 `head_object` calls cost ~1.6s of every icon's ~35s. Most calls are a product's
first generation, where nothing exists to preserve, so the live key is checked first and
that case answers in one round trip. The tests below pin both halves: the saving, and
the guarantee it must not cost — an original is never overwritten by a regeneration.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from pipeline.clients import storage


class FakeS3:
    """Records every call so round trips can be counted, not assumed."""

    def __init__(self, existing: set[str] | None = None):
        self.objects = set(existing or ())
        self.head_calls: list[str] = []
        self.copies: list[tuple[str, str]] = []

    def head_object(self, Bucket, Key):  # noqa: N803 — boto3's signature
        self.head_calls.append(Key)
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentLength": 1}

    def copy_object(self, Bucket, Key, CopySource):  # noqa: N803
        self.copies.append((CopySource["Key"], Key))
        self.objects.add(Key)


@pytest.fixture
def s3(monkeypatch, settings):
    settings.S3_BUCKET = "test-bucket"
    settings.S3_ICON_BACKUP_PREFIX = "_backup"
    fake = FakeS3()
    monkeypatch.setattr(storage, "get_client", lambda: fake)
    return fake


KEY = "2D_icons/demostore/1.svg"
BACKUP = "_backup/2D_icons/demostore/1.svg"


def test_first_generation_costs_one_round_trip(s3):
    """The common path. Two head calls here was ~1.6s wasted on every icon."""
    assert storage.backup_existing(KEY) == "nothing-to-back-up"

    assert s3.head_calls == [KEY]          # the backup was never asked about
    assert s3.copies == []


def test_a_regeneration_preserves_the_original(s3):
    s3.objects.add(KEY)

    assert storage.backup_existing(KEY) == "backed-up"
    assert s3.copies == [(KEY, BACKUP)]


def test_the_first_version_is_never_clobbered(s3):
    """Later versions are already regenerations — the original is the one worth keeping."""
    s3.objects.update({KEY, BACKUP})

    assert storage.backup_existing(KEY) == "already-backed-up"
    assert s3.copies == []                 # the existing backup survives


def test_repeated_regeneration_backs_up_once_only(s3):
    s3.objects.add(KEY)

    storage.backup_existing(KEY)           # -> backed up
    storage.backup_existing(KEY)           # -> already backed up
    storage.backup_existing(KEY)

    assert len(s3.copies) == 1


def test_a_missing_key_with_a_stray_backup_copies_nothing(s3):
    """Someone deleted the live icon but left its backup. Nothing to preserve, and
    nothing must be destroyed."""
    s3.objects.add(BACKUP)

    assert storage.backup_existing(KEY) == "nothing-to-back-up"
    assert s3.copies == []
    assert BACKUP in s3.objects
