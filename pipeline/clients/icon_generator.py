"""Generate one 2D icon with `gpt-image-2`.

Ported from `regenerate_icons.gen_icon`, keeping both of its retry conditions:

* **transient API errors** — rate limits, timeouts, 5xx — with exponential backoff, and
* **degenerate output** — the model silently returning a solid blob or a blank frame.

That second one matters more than it sounds. `gpt-image-2` returns HTTP 200 with an
image that is a uniform black rectangle often enough to need catching, and a reviewer
should never be shown one. It is the cheapest quality gate available: rejecting junk
before a human looks at it is worth far more than any API saving.
"""

from __future__ import annotations

import base64
import logging
import time
from io import BytesIO
from typing import NamedTuple

from django.conf import settings
from PIL import Image

from pipeline.services.icon_imaging import (
    _degenerate_reason,
    _scrub_edges_preserving_content,
    icon_to_svg,
    prepare_input,
)
from pipeline.services.icon_prompts import build_prompt

logger = logging.getLogger(__name__)

#: Errors worth waiting out rather than giving up on.
_TRANSIENT = (
    "rate limit", "429", "timeout", "timed out", "connection",
    "500", "502", "503", "504", "overloaded",
)


class IconGenerationError(RuntimeError):
    """No image could be obtained at all — the API never returned one."""


class IconResult(NamedTuple):
    """A generated icon, and why it is suspect if it is.

    ``warning`` is ``None`` for a clean pass. When set, the image failed the degeneracy
    heuristic on every attempt and is being handed to a human anyway — the check cannot
    tell a correctly-drawn pale product from a blank frame, and a reviewer can.
    """

    svg: str
    warning: str | None


def _client():
    from openai import OpenAI

    if not settings.OPENAI_API_KEY:
        raise IconGenerationError("OPENAI_API_KEY is not configured")
    return OpenAI(api_key=settings.OPENAI_API_KEY)


def generate_icon_svg(photo: bytes, category: str, name: str | None = None) -> IconResult:
    """Product photo -> `IconResult(svg, warning)`.

    Pure: no database, no S3, no status. That keeps the prompt and imaging logic
    testable on its own, and lets the stage own everything stateful.

    Raises only when no image was obtained at all. Output the degeneracy check rejects
    is returned with a `warning` rather than raised, so the decision reaches a human.
    """
    with Image.open(BytesIO(photo)) as pil:
        model_input = prepare_input(pil, settings.ICON_INPUT_SIZE)

    buf = BytesIO()
    model_input.save(buf, format="PNG")
    png_in = buf.getvalue()

    prompt = build_prompt(category, name)
    client = _client()
    delay, last_error = 3.0, None
    #: The most recent image the model returned, and why it was rejected. Kept so that
    #: exhausting the attempts can hand the last one over for a human to judge rather
    #: than throwing away work already paid for.
    last_out, last_bad = None, None

    for attempt in range(settings.ICON_GENERATION_RETRIES):
        try:
            fh = BytesIO(png_in)
            fh.name = "input.png"                  # the OpenAI client requires a filename
            response = client.images.edit(
                model=settings.ICON_MODEL,
                image=fh,
                prompt=prompt,
                size=settings.ICON_SIZE,
                quality=settings.ICON_QUALITY,
                n=1,
            )
        except Exception as exc:  # noqa: BLE001 — classified below, not swallowed
            last_error = exc
            message = str(exc).lower()
            if not any(s in message for s in _TRANSIENT):
                raise IconGenerationError(f"icon generation failed: {exc}") from exc
            if attempt == settings.ICON_GENERATION_RETRIES - 1:
                break
            logger.warning("icon generation transient error, retrying in %.0fs: %s", delay, exc)
            time.sleep(delay)
            delay = min(delay * 2, 40)
            continue

        out = Image.open(BytesIO(base64.b64decode(response.data[0].b64_json))).convert("RGB")
        # Scrub edge artifacts WITHOUT clipping a wide icon: pad first, so the 5% scrub
        # only ever touches padding, then let icon_to_svg crop back to real content.
        out = _scrub_edges_preserving_content(out)

        bad = _degenerate_reason(out)
        if bad is None:
            return IconResult(icon_to_svg(out), None)

        last_out, last_bad = out, bad
        last_error = RuntimeError(f"degenerate output: {bad}")
        logger.warning("icon generation returned %s, regenerating", bad)
        time.sleep(1.0)

    # Attempts exhausted. If the model DID return an image, hand the last one over
    # flagged instead of discarding it: the check is a contrast heuristic, and a pale
    # product drawn correctly on white trips it exactly as a blank frame would. A human
    # can tell those apart; a threshold cannot. Failing here also threw away five paid
    # generations and left the reviewer nothing to look at.
    if last_out is not None:
        logger.warning("icon generation exhausted retries (%s); flagging for review", last_bad)
        return IconResult(icon_to_svg(last_out), last_bad)

    # Nothing was ever returned — a persistent API failure. There is no image to judge.
    raise IconGenerationError(f"icon generation failed after retries: {last_error}")
