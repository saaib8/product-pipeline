"""Bilingual product names.

Mirrors the existing backend (`core/utils/helpers.py`). A merchant sheet carries ONE
`product_name` column; which language it is gets detected, and the other side is
generated. So `name_arabic` is almost never supplied — it is produced here.

Two behaviours are deliberately copied rather than improved on:

* **A translation failure returns the original text.** It never raises. An OpenAI
  outage degrades to an untranslated name; it does not fail the import.
* **A fallback API key is tried** before giving up.

The cost is one `gpt-4o-mini` call per product, which is why import runs in a worker
rather than inside the upload request.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

MODEL = "gpt-4o-mini"

#: Arabic, Arabic Supplement, and Arabic Extended-A unicode blocks.
_ARABIC = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿ]")


def is_arabic(text: str | None) -> bool:
    """Does this text contain Arabic characters?"""
    return bool(text) and bool(_ARABIC.search(str(text)))


def _translate(text: str, target: str) -> str:
    """One chat completion. Returns `text` unchanged if translation is unavailable."""
    messages = [
        {"role": "system", "content": "You are a translator."},
        {
            "role": "user",
            "content": (
                f"Translate this text into {target} but if this is already in "
                f"{target.lower()} give same text back: {text}"
            ),
        },
    ]
    # Read through Django settings rather than `os.environ`. `settings.env()` strips
    # surrounding quotes, which a `.env` line like KEY='sk-...' leaves in place — and a
    # key carrying literal quotes is rejected with a 401 that looks exactly like an
    # expired credential. Every other client in the pipeline goes through settings; this
    # one did not, so it alone failed while icon generation kept working.
    from django.conf import settings

    for key_name in ("OPENAI_API_KEY", "FALLBACK_OPENAI_API_KEY"):
        api_key = getattr(settings, key_name, None) or os.environ.get(key_name)
        if not api_key:
            continue
        try:
            from openai import OpenAI

            client = OpenAI(api_key=api_key)
            response = client.chat.completions.create(model=MODEL, messages=messages)
            return (response.choices[0].message.content or text).strip()
        except Exception as exc:  # noqa: BLE001 — never fail an import over a name
            logger.warning("translation via %s failed: %s", key_name, exc)
    return text


def translate_to_arabic(text: str) -> str:
    return _translate(text, "Arabic")


def translate_to_english(text: str) -> str:
    return _translate(text, "English")


def bilingual_names(product_name: str) -> tuple[str, str]:
    """`product_name` -> `(name_english, name_arabic)`.

    Whichever language the sheet supplied is kept verbatim; the other is generated.
    """
    name = str(product_name).strip()
    if is_arabic(name):
        return translate_to_english(name), name
    return name, translate_to_arabic(name)
