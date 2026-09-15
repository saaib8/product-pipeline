"""Qwen3-VL metadata generation, via OpenRouter.

Replaces a self-hosted Qwen2.5-VL-32B on Modal. That deployment was abandoned for one
reason: bf16 weights are ~65 GB, and loading them from storage took 25+ minutes, so every
cold start cost more than an hour of A100 time would have been worth. A hosted endpoint
removes the problem rather than working around it — no GPU, no snapshot, no submit+poll.

The call is therefore **synchronous**, the same shape as `clients/detection.py`. The
submit + poll machinery existed only to survive a cold start that no longer happens.

What did NOT change, deliberately: the prompt, the controlled vocabularies and the
parsing all still come from `services/metadata_prompt.py` and `metadata_vocab.py`. The
model is a swappable detail; the palette a product is allowed to be is not.

OpenRouter speaks the OpenAI chat-completions dialect, so the image travels as a base64
data URI in a `image_url` content part. We send bytes rather than the source URL on
purpose — many merchant CDNs 403 datacenter IPs, and we have already downloaded the
image successfully by this point.
"""

from __future__ import annotations

import base64
import logging

import requests
from django.conf import settings

from pipeline.services.metadata_prompt import build_prompt, parse_metadata

logger = logging.getLogger(__name__)

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


class MetadataError(RuntimeError):
    """The metadata service could not be reached, or answered unusably."""


def _headers() -> dict[str, str]:
    key = (getattr(settings, "OPENROUTER_API_KEY", "") or "").strip()
    if not key:
        raise MetadataError("OPENROUTER_API_KEY is not configured")
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        # OpenRouter attributes usage to these; harmless if unset.
        "HTTP-Referer": getattr(settings, "OPENROUTER_SITE_URL", "") or "",
        "X-Title": "zory-product-pipeline",
    }


def generate_metadata(image: bytes, category: str | None = None) -> dict[str, str]:
    """One product photo -> `{main_color, secondary_colors, styles}` as text.

    `category` names the object in the prompt, so a photo of a styled room is described
    as the product rather than as whatever else is in frame.

    Returns empty strings rather than raising when the model answers off-palette or
    unparseably — that is a *result* the stage retries, not a transport failure. Raises
    `MetadataError` only when no answer was obtained at all.
    """
    data_uri = "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")
    payload = {
        "model": settings.OPENROUTER_METADATA_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": build_prompt(category)},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ],
        }],
        "max_tokens": settings.METADATA_MAX_NEW_TOKENS,
        # Deterministic, matching the notebook's `do_sample=False`. Two runs over the
        # same catalogue should not disagree with each other.
        "temperature": 0,
    }

    try:
        response = requests.post(ENDPOINT, json=payload, headers=_headers(),
                                 timeout=settings.METADATA_TIMEOUT_SECONDS)
        response.raise_for_status()
        body = response.json()
    except requests.RequestException as exc:
        raise MetadataError(f"metadata request failed: {exc}") from exc
    except ValueError as exc:
        raise MetadataError(f"metadata returned invalid JSON: {exc}") from exc

    # OpenRouter reports upstream provider failures in-band with HTTP 200.
    if isinstance(body.get("error"), dict):
        raise MetadataError(f"metadata provider error: {body['error'].get('message')}")

    choices = body.get("choices") or []
    if not choices:
        raise MetadataError(f"metadata returned no choices: {str(body)[:200]}")

    raw = (choices[0].get("message") or {}).get("content") or ""
    if not isinstance(raw, str):
        raise MetadataError(f"metadata returned no text (got {type(raw).__name__})")

    usage = body.get("usage") or {}
    logger.debug("metadata via %s: %s prompt + %s completion tokens",
                 body.get("model"), usage.get("prompt_tokens"),
                 usage.get("completion_tokens"))
    return parse_metadata(raw)
