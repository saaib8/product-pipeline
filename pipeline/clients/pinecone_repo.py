from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import logging
from pinecone import Pinecone, ServerlessSpec


def _clean_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Coerce a metadata dict into Pinecone-accepted types.

    Pinecone metadata values may only be str, int, float, bool, or list of
    str. Decimals (e.g. product prices) raise "Unable to prepare type Decimal
    for serialization", and null values are rejected, so we convert Decimals
    to float and drop None values.
    """
    if not metadata:
        return {}

    cleaned: dict[str, Any] = {}
    for key, value in metadata.items():
        if value is None:
            continue
        if isinstance(value, Decimal):
            cleaned[key] = float(value)
        elif isinstance(value, list):
            cleaned[key] = [
                float(item) if isinstance(item, Decimal) else item
                for item in value
                if item is not None
            ]
        else:
            cleaned[key] = value
    return cleaned


# Configure logging (same approach as pinecone_helper.py)
def setup_logging():
    """
    Set up logging configuration for Pinecone repository.
    """
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)

    # Only add handler if it doesn't already have one
    if not logger.handlers:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    return logger


logger = setup_logging()


@dataclass
class PineconeVectorRepository:
    api_key: str
    index_name: str
    cloud: str
    region: str
    dimension: int

    def __post_init__(self) -> None:
        self._pc = Pinecone(api_key=self.api_key)
        self._index = None

    async def ensure_index(self) -> None:
        existing = {i["name"] for i in self._pc.list_indexes()}
        if self.index_name not in existing:
            logger.info(f"Creating Pinecone index: {self.index_name}")
            self._pc.create_index(
                name=self.index_name,
                dimension=self.dimension,
                metric="cosine",
                spec=ServerlessSpec(cloud=self.cloud, region=self.region),
            )
        self._index = self._pc.Index(self.index_name)

    def upsert(
            self,
            *,
            vector_id: str,
            vector: list[float],
            metadata: dict[str, Any],
            namespace: str | None = None,
    ) -> None:
        """
        Upsert vector to Pinecone.

        Args:
            vector_id: Unique ID for the vector
            vector: Embedding vector
            metadata: Metadata dict (must include 'category')
            namespace: Optional namespace (defaults to category from metadata)
        """
        assert self._index is not None

        # Use category as namespace for fast category-based search
        # This allows us to query only within a specific category
        if namespace is None:
            category = metadata.get("category", "default")
            namespace = self._normalize_namespace(category)

        logger.debug(f"Upserting to namespace: {namespace}, vector_id: {vector_id}")

        self._index.upsert(
            vectors=[
                {
                    "id": vector_id,
                    "values": vector,
                    "metadata": _clean_metadata(metadata),
                }
            ],
            namespace=namespace,
        )

    def query(
            self,
            *,
            vector: list[float],
            top_k: int,
            namespace: str | None = None,
            category: str | None = None,
            metadata_filter: dict[str, Any] | None = None,
            include_metadata: bool = True,
            include_values: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Query vectors from Pinecone.

        Args:
            vector: Query embedding vector
            top_k: Number of results to return
            namespace: Optional explicit namespace
            category: Category to search in (converted to namespace)
            metadata_filter: Additional metadata filters
            include_metadata: Whether to include metadata in results

        Returns:
            List of matching vectors with metadata
        """
        assert self._index is not None

        # Use category as namespace for fast category-based search
        if namespace is None and category:
            namespace = self._normalize_namespace(category)
        elif namespace is None:
            namespace = "default"

        logger.debug(f"Querying namespace: {namespace}, top_k: {top_k}")

        res = self._index.query(
            vector=vector,
            top_k=top_k,
            namespace=namespace,
            filter=metadata_filter,
            include_metadata=include_metadata,
            include_values=include_values,  # vectors needed for near-duplicate bundling
        )
        matches = res.get("matches") or []

        logger.debug(f"Retrieved {len(matches)} matches from namespace '{namespace}'")

        return matches

    def get_stats(self, category: str | None = None) -> dict[str, Any]:
        """
        Get index statistics including vector count.
        Used to estimate catalog size for adaptive candidate retrieval.

        Args:
            category: Optional category to get stats for specific namespace

        Returns:
            Dictionary with stats including:
            - total_vector_count: Number of vectors in namespace/index
            - dimension: Vector dimension
        """
        assert self._index is not None

        try:
            stats = self._index.describe_index_stats()

            if category:
                # Get stats for specific namespace
                namespace = self._normalize_namespace(category)
                namespace_stats = stats.get('namespaces', {}).get(namespace, {})
                vector_count = namespace_stats.get('vector_count', 0)
                logger.debug(f"Namespace '{namespace}' has {vector_count:,} vectors")
            else:
                # Total across all namespaces
                vector_count = stats.get('total_vector_count', 0)
                logger.debug(f"Total index has {vector_count:,} vectors")

            return {
                'total_vector_count': vector_count,
                'dimension': stats.get('dimension', self.dimension),
            }

        except Exception as e:
            logger.warning(f"Failed to get index stats: {e}")
            # Return default values on error
            return {
                'total_vector_count': 0,
                'dimension': self.dimension,
            }

    def update_metadata(
        self,
        *,
        vector_id: str,
        namespace: str,
        fields: dict[str, Any],
    ) -> None:
        """
        Partially update metadata fields on an existing vector without
        changing the vector values or re-embedding.

        Args:
            vector_id: The Pinecone vector ID to update.
            namespace: The namespace (category) the vector lives in.
            fields: Dict of metadata fields to set/overwrite.
        """
        assert self._index is not None
        self._index.update(
            id=vector_id,
            set_metadata=_clean_metadata(fields),
            namespace=namespace,
        )
        logger.debug(f"Updated metadata for {vector_id} in namespace '{namespace}'")

    def delete(
        self,
        *,
        vector_ids: list[str],
        namespace: str,
    ) -> None:
        """
        Delete one or more vectors from a namespace.

        Args:
            vector_ids: List of Pinecone vector IDs to delete.
            namespace: The namespace (category) to delete from.
        """
        assert self._index is not None
        self._index.delete(ids=vector_ids, namespace=namespace)
        logger.debug(f"Deleted {len(vector_ids)} vector(s) from namespace '{namespace}'")

    def fetch_values(
        self,
        *,
        vector_id: str,
        namespace: str,
    ) -> list[float] | None:
        """
        Fetch the raw embedding values for a single vector.
        Used when a vector needs to be moved between namespaces (category change)
        without re-running the embedding pipeline.

        Args:
            vector_id: The Pinecone vector ID to fetch.
            namespace: The namespace (category) to fetch from.

        Returns:
            List of floats (the embedding vector), or None if not found.
        """
        assert self._index is not None
        result = self._index.fetch(ids=[vector_id], namespace=namespace)
        vectors = result.get("vectors", {})
        entry = vectors.get(vector_id)
        if entry is None:
            logger.warning(f"Vector {vector_id} not found in namespace '{namespace}'")
            return None
        return entry.get("values")

    def fetch_values_many(
        self,
        *,
        vector_ids: list[str],
        namespace: str,
    ) -> dict[str, list[float]]:
        """
        Fetch raw embedding values for many vectors in a single namespace.
        Batches the underlying Pinecone fetch (which caps ids per call) and
        returns a {vector_id: values} map for the ids that were found. Missing
        ids are simply absent from the map (no error) so callers can degrade
        gracefully.

        Args:
            vector_ids: Pinecone vector IDs to fetch.
            namespace: The namespace (category) to fetch from.

        Returns:
            Dict mapping each found vector_id to its embedding values.
        """
        assert self._index is not None
        out: dict[str, list[float]] = {}
        if not vector_ids:
            return out

        # Pinecone fetch accepts a bounded number of ids per call; chunk to stay
        # well under the limit.
        BATCH = 1000
        for start in range(0, len(vector_ids), BATCH):
            chunk = vector_ids[start:start + BATCH]
            try:
                result = self._index.fetch(ids=chunk, namespace=namespace)
            except Exception as exc:
                logger.warning(
                    f"Pinecone batch fetch failed for namespace '{namespace}' "
                    f"({len(chunk)} ids): {exc}"
                )
                continue
            vectors = result.get("vectors", {}) or {}
            for vid, entry in vectors.items():
                values = (entry or {}).get("values")
                if values:
                    out[vid] = values

        logger.debug(
            f"Fetched {len(out)}/{len(vector_ids)} vectors from namespace '{namespace}'"
        )
        return out

    def fetch_metadata_many(
        self,
        *,
        vector_ids: list[str],
        namespace: str,
    ) -> dict[str, dict[str, Any]]:
        """
        Fetch the metadata for many vectors in a single namespace.

        Used when a product exists in the vector index but has no Postgres
        mirror row, so the metadata is the only description of it we have.
        Missing ids are absent from the result rather than raising, so callers
        can degrade gracefully.

        Args:
            vector_ids: Pinecone vector IDs to fetch.
            namespace: The namespace (category) to fetch from.

        Returns:
            Dict mapping each found vector_id to its metadata dict.
        """
        assert self._index is not None
        out: dict[str, dict[str, Any]] = {}
        if not vector_ids:
            return out

        BATCH = 1000
        for start in range(0, len(vector_ids), BATCH):
            chunk = vector_ids[start:start + BATCH]
            try:
                result = self._index.fetch(ids=chunk, namespace=namespace)
            except Exception as exc:
                logger.warning(
                    f"Pinecone metadata fetch failed for namespace '{namespace}' "
                    f"({len(chunk)} ids): {exc}"
                )
                continue
            vectors = result.get("vectors", {}) or {}
            for vid, entry in vectors.items():
                metadata = (entry or {}).get("metadata")
                if metadata:
                    out[vid] = dict(metadata)

        logger.debug(
            f"Fetched metadata for {len(out)}/{len(vector_ids)} vectors "
            f"from namespace '{namespace}'"
        )
        return out

    def list_namespaces(self) -> dict[str, int]:
        """Return {namespace: vector_count} for every namespace in the index.

        The namespace IS the product category, and it is assigned at index time
        from the detected/resolved category — which makes it the authoritative
        category, unlike the DB column.
        """
        assert self._index is not None
        try:
            stats = self._index.describe_index_stats()
        except Exception as exc:
            logger.warning(f"Failed to list namespaces: {exc}")
            return {}

        namespaces = stats.get("namespaces") or {}
        out: dict[str, int] = {}
        for name, info in namespaces.items():
            if not name:
                continue
            count = (info or {}).get("vector_count", 0)
            out[name] = count
        return out

    # Pinecone caps top_k at 10_000 for a single query.
    MAX_TOP_K = 10_000

    def list_metadata(
        self,
        *,
        namespace: str,
        expected_count: int,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Return every vector's metadata in a namespace.

        Uses a single query with include_values=False rather than list+fetch:
        fetch always ships the full embedding, so reading ~1k vectors that way
        moved ~25 MB and took minutes, versus seconds for the metadata alone.
        The probe vector is arbitrary — with top_k >= the namespace size the
        ordering is irrelevant, every vector comes back.

        Each entry is the raw metadata plus the vector's own "id" and the
        "namespace" it came from. Returns [] if the namespace is empty.

        Args:
            namespace: The namespace (category) to read.
            expected_count: Vector count for the namespace (from list_namespaces).
            metadata_filter: Optional Pinecone metadata filter applied by the
                index itself, so narrowing happens during the read instead of
                afterwards in Python.
        """
        assert self._index is not None

        if expected_count <= 0:
            return []

        # Headroom for vectors added since the stats snapshot.
        top_k = min(int(expected_count * 1.1) + 10, self.MAX_TOP_K)
        if expected_count > self.MAX_TOP_K:
            logger.warning(
                f"Namespace '{namespace}' holds {expected_count} vectors but a query "
                f"returns at most {self.MAX_TOP_K}; the listing is truncated"
            )

        query_kwargs: dict[str, Any] = {
            "vector": [0.001] * self.dimension,
            "top_k": top_k,
            "namespace": namespace,
            "include_metadata": True,
            "include_values": False,
        }
        if metadata_filter:
            query_kwargs["filter"] = metadata_filter

        try:
            result = self._index.query(**query_kwargs)
        except Exception as exc:
            logger.warning(f"Failed to read namespace '{namespace}': {exc}")
            return []

        out: list[dict[str, Any]] = []
        for match in (result.get("matches") or []):
            metadata = dict(match.get("metadata") or {})
            metadata["id"] = match.get("id")
            metadata["namespace"] = namespace
            out.append(metadata)

        logger.debug(f"Read {len(out)}/{expected_count} vectors from namespace '{namespace}'")
        return out

    def _normalize_namespace(self, category: str) -> str:
        """
        Normalize category name to valid Pinecone namespace.
        Namespaces must be alphanumeric with hyphens/underscores.
        """
        if not category:
            return "default"

        # Convert to lowercase, replace spaces with hyphens
        normalized = category.lower().strip().replace(" ", "-")

        # Remove any non-alphanumeric characters except hyphens and underscores
        normalized = "".join(c for c in normalized if c.isalnum() or c in "-_")

        # Ensure it's not empty
        if not normalized:
            return "default"

        return normalized
