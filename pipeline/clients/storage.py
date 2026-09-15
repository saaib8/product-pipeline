"""S3 for generated assets.

The key is **stable and logical**: `2D_icons/<store>/<product_id>.svg`. Regenerating a
product's icon overwrites the same key, so `Product.two_d_icon` never needs updating
after the first write and no consumer's URL goes stale — which is what the design doc
asks for in §4.4.

The product id is used rather than `{category}-{id}` (one of the two conventions in the
existing scripts) so that correcting a category never orphans an icon.
"""

from __future__ import annotations

import logging
import re
import threading

import boto3
from botocore.exceptions import ClientError
from django.conf import settings

logger = logging.getLogger(__name__)

_client = None
_lock = threading.Lock()


def get_client():
    """The process-wide S3 client (boto3 clients are thread-safe once built)."""
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is None:
            kwargs = {"region_name": settings.AWS_REGION}
            if settings.AWS_ACCESS_KEY_ID and settings.AWS_SECRET_ACCESS_KEY:
                kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
                kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
            if settings.AWS_S3_ENDPOINT_URL:          # MinIO or another S3-compatible store
                kwargs["endpoint_url"] = settings.AWS_S3_ENDPOINT_URL
            _client = boto3.client("s3", **kwargs)
    return _client


def store_folder(store_name: str) -> str:
    """Folder segment for a store — same squashing the source script uses."""
    return re.sub(r"[^a-z0-9]", "", (store_name or "").lower()) or "store"


def icon_key(store_name: str, product_id: int) -> str:
    """The stable logical key an icon lives at, for its whole life."""
    return f"{settings.S3_ICON_PREFIX}/{store_folder(store_name)}/{product_id}.svg"


def upload_svg(key: str, svg: str) -> str:
    """Write (or overwrite) an SVG. Returns the key."""
    get_client().put_object(
        Bucket=settings.S3_BUCKET,
        Key=key,
        Body=svg.encode("utf-8"),
        ContentType="image/svg+xml",
    )
    return key


def backup_existing(key: str) -> str:
    """Copy the current object aside before it is overwritten — once only.

    Regeneration is destructive at a stable key, so the previous icon would otherwise
    be unrecoverable. Never clobbers an existing backup: the FIRST version is the one
    worth keeping, since later ones are already regenerations.

    The live key is checked FIRST because that is the common case by a wide margin —
    most calls are a product's first generation, where nothing exists to preserve and
    the answer is one round trip instead of two. Checking the backup first spent two
    round trips (~1.6s) on every icon to answer a question that only matters when there
    is something to back up.

    Note the column cannot be used to shortcut this: the key is derived from the product
    id, so an object can exist at it while `two_d_icon` is empty — that is exactly how
    the orphaned icons arose before the key was reserved ahead of generation.
    """
    client = get_client()
    backup = f"{settings.S3_ICON_BACKUP_PREFIX}/{key}"
    try:
        client.head_object(Bucket=settings.S3_BUCKET, Key=key)
    except ClientError:
        return "nothing-to-back-up"          # first generation: the common path
    try:
        client.head_object(Bucket=settings.S3_BUCKET, Key=backup)
        return "already-backed-up"
    except ClientError:
        pass
    client.copy_object(
        Bucket=settings.S3_BUCKET, Key=backup,
        CopySource={"Bucket": settings.S3_BUCKET, "Key": key},
    )
    return "backed-up"


def public_url(key: str) -> str:
    """Browser-visible URL for a key, for the review UI."""
    if settings.S3_PUBLIC_BASE_URL:
        return f"{settings.S3_PUBLIC_BASE_URL.rstrip('/')}/{key}"
    return presigned_url(key)


def presigned_url(key: str, expires: int = 3600) -> str:
    """Time-limited URL. Used when the bucket is private, which it should be."""
    return get_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.S3_BUCKET, "Key": key},
        ExpiresIn=expires,
    )


def reset_client() -> None:
    """Drop the cached client. For tests and after a config change."""
    global _client
    with _lock:
        _client = None
