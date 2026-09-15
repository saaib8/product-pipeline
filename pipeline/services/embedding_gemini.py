from __future__ import annotations

import time
from dataclasses import dataclass, field
from io import BytesIO

import numpy as np
from PIL import Image

import logging
logger = logging.getLogger(__name__)


def _l2_normalize(x: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(x) + 1e-12
    return x / n


def _image_to_bytes(img: Image.Image) -> bytes:
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


@dataclass
class GeminiEmbeddingService:
    """
    Image embedding service using Google's gemini-embedding-2-preview model.

    Per object:
      - Sends tight crop + medium crop in ONE API call -> two 3072-dim vectors
      - Averages and L2-normalizes the two vectors

    Retry logic handles transient API rate-limit / server errors.
    """

    api_key: str
    model: str = "models/gemini-embedding-2-preview"
    output_dimensionality: int = 3072
    max_retries: int = 4
    initial_backoff: float = 1.0

    _client: object = field(default=None, init=False, repr=False)
    _loaded: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._loaded = False
        self._client = None

    def load_sync(self) -> None:
        """Synchronous load — for use in Django (no event loop)."""
        if self._loaded:
            return
        try:
            from google import genai
            self._client = genai.Client(api_key=self.api_key)
            self._loaded = True
            logger.info(
                f"GeminiEmbeddingService ready — model: {self.model}, "
                f"dim: {self.output_dimensionality}"
            )
        except ImportError:
            raise RuntimeError(
                "google-genai is not installed. Run: pip install google-genai"
            )

    def embed_crops(self, crops: dict[str, Image.Image]) -> list[float]:
        """
        Embed tight + medium crops in a single API call and return the
        averaged 3072-dim vector.
        """
        if not self._loaded:
            raise RuntimeError("GeminiEmbeddingService not loaded. Call load_sync() first.")

        priority_keys = ["tight", "medium"]
        selected = {k: crops[k] for k in priority_keys if k in crops}

        if not selected:
            selected = {k: v for k, v in list(crops.items())[:2]}
            logger.warning(
                f"Expected 'tight'/'medium' keys, got: {list(crops.keys())}. "
                f"Using first two crops instead."
            )

        embeddings = self._embed_batch(list(selected.values()), list(selected.keys()))

        averaged = _l2_normalize(np.mean(embeddings, axis=0))
        logger.debug(
            f"Embedded {len(embeddings)} crops ({list(selected.keys())}), "
            f"final dim: {len(averaged)}"
        )
        return averaged.astype(np.float32).tolist()

    def _embed_batch(self, images: list[Image.Image], names: list[str]) -> list[np.ndarray]:
        """Send all images in a single Gemini API call."""
        from google.genai import types

        contents = [
            types.Part.from_bytes(data=_image_to_bytes(img), mime_type="image/jpeg")
            for img in images
        ]
        config = types.EmbedContentConfig(
            task_type="SEMANTIC_SIMILARITY",
            output_dimensionality=self.output_dimensionality,
        )

        backoff = self.initial_backoff
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                result = self._client.models.embed_content(
                    model=self.model,
                    contents=contents,
                    config=config,
                )
                vecs = [
                    _l2_normalize(np.array(e.values, dtype=np.float32))
                    for e in result.embeddings
                ]
                logger.debug(
                    f"Batch embedded {len(vecs)} crops {names} "
                    f"(attempt {attempt}), dim={len(vecs[0])}"
                )
                return vecs

            except Exception as e:
                last_error = e
                err_str = str(e).lower()
                is_retryable = any(
                    token in err_str
                    for token in ("rate", "quota", "429", "503", "timeout", "unavailable")
                )

                if not is_retryable or attempt == self.max_retries:
                    logger.error(
                        f"Gemini embed failed for crops {names} "
                        f"(attempt {attempt}/{self.max_retries}): {e}"
                    )
                    raise

                logger.warning(
                    f"Gemini embed attempt {attempt}/{self.max_retries} failed "
                    f"for crops {names}: {e}. Retrying in {backoff:.1f}s..."
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

        raise last_error  # type: ignore[misc]
