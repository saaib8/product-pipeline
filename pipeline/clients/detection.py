"""Detection + segmentation, over HTTP.

Two models run remotely on Modal and answer in **one round trip**: the response carries
the bounding box *and* the mask polygon, so there is no second call to segment. The
design doc describes these as two sequential hops (RF-DETR, then SAM2); in practice
they share a container and one request.

This is a deliberate rewrite rather than a port. The source client is 194 lines because
it emulates RunPod's async API — `submit_job` does the work synchronously and stashes
the result, `wait_for_completion` pops it from a dict, and `to_runpod_envelope` re-wraps
Modal's native response into RunPod's shape. None of that scaffolding earns its keep
here, so the endpoint is called directly and parsed into typed objects.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class DetectionError(RuntimeError):
    """The detection service could not be reached, or answered unusably."""


@dataclass(frozen=True)
class Detection:
    """One detected object."""

    label: str
    confidence: float
    #: [x1, y1, x2, y2], derived from the mask rather than the raw box.
    bbox: list[int]
    #: Polygon outline, which is what makes the second (segmentation) call unnecessary.
    mask_polygon: list

    @property
    def area(self) -> int:
        x1, y1, x2, y2 = self.bbox
        return max(0, x2 - x1) * max(0, y2 - y1)

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "Detection | None":
        """Build one from a response entry, or None if it is unusable.

        A detection without a mask or a box cannot be segmented or cropped, so it is no
        use to us even though the model returned it.
        """
        bbox = raw.get("bbox_from_mask")
        polygon = raw.get("mask_polygon")
        if not bbox or not polygon:
            return None
        try:
            return cls(
                label=str(raw.get("label", "")),
                confidence=float(raw.get("confidence", 0.0)),
                bbox=[int(v) for v in bbox],
                mask_polygon=polygon,
            )
        except (TypeError, ValueError):
            return None


def detect(image: bytes, *, confidence_threshold: float | None = None,
           timeout: int | None = None) -> list[Detection]:
    """Run detection + segmentation on one image.

    Raises `DetectionError` on anything that isn't a usable response — the stage turns
    that into a retry.
    """
    url = (getattr(settings, "MODAL_URL", "") or "").strip().rstrip("/")
    key = (getattr(settings, "MODAL_KEY", "") or "").strip()
    secret = (getattr(settings, "MODAL_SECRET", "") or "").strip()
    if not (url and key and secret):
        raise DetectionError("MODAL_URL / MODAL_KEY / MODAL_SECRET are not configured")

    threshold = (confidence_threshold if confidence_threshold is not None
                 else settings.DETECTION_CONFIDENCE_THRESHOLD)
    # Modal caps synchronous HTTP at ~150s, so the client must not wait longer than the
    # platform will. (The source client uses 200s, which can only ever time out later
    # than the server already gave up.)
    timeout = timeout if timeout is not None else settings.DETECTION_TIMEOUT_SECONDS

    payload = {
        "image": base64.b64encode(image).decode("ascii"),
        "confidence_threshold": threshold,
    }
    headers = {"Modal-Key": key, "Modal-Secret": secret, "Content-Type": "application/json"}

    try:
        response = requests.post(url, json=payload, headers=headers, timeout=timeout)
        response.raise_for_status()
        body = response.json()
    except requests.RequestException as exc:
        raise DetectionError(f"detection request failed: {exc}") from exc
    except ValueError as exc:
        raise DetectionError(f"detection returned invalid JSON: {exc}") from exc

    status = str(body.get("status", "")).lower()
    if status and status not in ("success", "completed", "ok"):
        raise DetectionError(f"detection reported {status!r}: {body.get('error')}")

    result = body.get("result", body)
    raw_objects = result.get("detected_objects", []) if isinstance(result, dict) else []
    return [d for d in (Detection.parse(o) for o in raw_objects) if d is not None]
