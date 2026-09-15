"""Pinecone access.

Thin wrapper over the repository lifted from the backend, so the stage doesn't carry
connection setup. One repository per process — the client holds a connection pool and
rebuilding it per product would be wasteful.

Note the namespace is the **category**. That is the source system's scheme and it is
kept, but it has a consequence worth remembering: correcting a product's category means
its old vector must be deleted from the old namespace, not just overwritten. Nothing
does that yet; it belongs with the category-correction flow.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from django.conf import settings

from pipeline.clients.pinecone_repo import PineconeVectorRepository

logger = logging.getLogger(__name__)

_repo: PineconeVectorRepository | None = None
_lock = threading.Lock()


def get_repository() -> PineconeVectorRepository:
    """The process-wide repository, built on first use (thread-safe)."""
    global _repo
    if _repo is not None:
        return _repo
    with _lock:
        if _repo is None:
            if not settings.PINECONE_API_KEY:
                raise RuntimeError("PINECONE_API_KEY is not configured")
            repo = PineconeVectorRepository(
                api_key=settings.PINECONE_API_KEY,
                index_name=settings.PINECONE_INDEX_NAME,
                cloud=settings.PINECONE_CLOUD,
                region=settings.PINECONE_REGION,
                dimension=settings.PINECONE_DIMENSION,
            )
            repo._index = repo._pc.Index(settings.PINECONE_INDEX_NAME)
            logger.info("pinecone: connected to index %r", settings.PINECONE_INDEX_NAME)
            _repo = repo
    return _repo


def upsert_vector(*, vector_id: str, vector: list[float], namespace: str,
                  metadata: dict[str, Any]) -> None:
    """Write one vector. The namespace is the product's category."""
    get_repository().upsert(
        vector_id=vector_id, vector=vector, metadata=metadata, namespace=namespace
    )


def delete_vector(*, vector_id: str, namespace: str) -> None:
    """Remove one vector — needed when a category correction moves it namespaces."""
    get_repository().delete(vector_ids=[vector_id], namespace=namespace)


def reset_repository() -> None:
    """Drop the cached repository. For tests and after a config change."""
    global _repo
    with _lock:
        _repo = None
