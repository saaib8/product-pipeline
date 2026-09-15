"""Image handling for 2D icons — ported verbatim from the existing scripts.

`prepare_input`, `clear_border_watermarks` and `icon_to_svg` come from
`gpt_image_icons.py`; `_scrub_edges_preserving_content` and `_degenerate_reason` from
`regenerate_icons.py`. Copied rather than rewritten: each one encodes a specific
failure that was observed and fixed.

Worth knowing about the output: **the .svg is not vector.** `gpt-image-2` cannot emit
transparency, so `icon_to_svg` cuts the white background out with connected components
and embeds an RGBA PNG inside an `<svg><image>` wrapper. The icons therefore do not
scale, and are larger than true vectors would be.
"""

from __future__ import annotations

import base64
from io import BytesIO

import cv2
import numpy as np
from PIL import Image

def prepare_input(pil_img: Image.Image, target_size: int = 1024) -> Image.Image:
    """Pad to a white square and resize. The white padding also nudges the
    model toward a clean white background."""
    img = pil_img.convert("RGB")
    w, h = img.size
    max_side = max(w, h)
    canvas = Image.new("RGB", (max_side, max_side), (255, 255, 255))
    canvas.paste(img, ((max_side - w) // 2, (max_side - h) // 2))
    return canvas.resize((target_size, target_size), Image.LANCZOS)

def clear_border_watermarks(pil_img: Image.Image, margin_frac: float = 0.05) -> Image.Image:
    img = np.array(pil_img.convert("RGB"))
    h, w = img.shape[:2]
    my = max(1, int(h * margin_frac))
    mx = max(1, int(w * margin_frac))
    img[:my, :] = 255
    img[h - my:, :] = 255
    img[:, :mx] = 255
    img[:, w - mx:] = 255
    return Image.fromarray(img)   # mode inferred from array (RGB)

def icon_to_svg(pil_img: Image.Image) -> str:
    """White-background removal (connected components) -> crop -> transparent PNG
    wrapped in <svg><image>. gpt-image-2 cannot emit transparency, so this is how
    we get a cut-out icon."""
    img_rgb = np.array(pil_img.convert("RGB"))
    h, w = img_rgb.shape[:2]
    near_white = np.all(img_rgb >= 240, axis=-1).astype(np.uint8)
    _, labels = cv2.connectedComponents(near_white, connectivity=8)
    border_pixels = np.concatenate([labels[0, :], labels[h - 1, :], labels[1:-1, 0], labels[1:-1, w - 1]])
    bg_labels = set(np.unique(border_pixels))
    bg_mask = np.isin(labels, list(bg_labels)) & (near_white > 0)
    alpha = (~bg_mask).astype(np.uint8) * 255

    kernel = np.ones((3, 3), dtype=np.uint8)
    alpha = cv2.morphologyEx(alpha, cv2.MORPH_CLOSE, kernel, iterations=2)
    alpha = cv2.morphologyEx(alpha, cv2.MORPH_OPEN, kernel, iterations=1)

    ys, xs = np.where(alpha > 0)
    if ys.size == 0:
        buf = BytesIO()
        pil_img.save(buf, format="PNG", optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
                f'viewBox="0 0 {w} {h}"><image width="{w}" height="{h}" '
                f'href="data:image/png;base64,{b64}"/></svg>\n')

    y0, y1 = int(ys.min()), int(ys.max())
    x0, x1 = int(xs.min()), int(xs.max())
    crop_rgb = img_rgb[y0:y1 + 1, x0:x1 + 1]
    crop_alpha = alpha[y0:y1 + 1, x0:x1 + 1]

    out_rgba = np.zeros((*crop_rgb.shape[:2], 4), dtype=np.uint8)
    out_rgba[..., :3] = crop_rgb
    out_rgba[..., 3] = crop_alpha

    result = Image.fromarray(out_rgba)   # mode inferred from array (RGBA)
    rw, rh = result.size
    buf = BytesIO()
    result.save(buf, format="PNG", optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{rw}" height="{rh}" '
            f'viewBox="0 0 {rw} {rh}"><image width="{rw}" height="{rh}" '
            f'href="data:image/png;base64,{b64}"/></svg>\n')

def _scrub_edges_preserving_content(out: Image.Image) -> Image.Image:
    """clear_border_watermarks() whitens the outer 5% of the frame to kill edge
    artifacts, but that flat-slices any icon whose content reaches the edge (wide SET
    icons: the end tables get cut off). Pad with white first so the 5% scrub only ever
    hits padding; icon_to_svg then crops back to the full, un-clipped content."""
    w, h = out.size
    pad = int(max(w, h) * 0.08) + 2                    # 8% > the 5% scrub, so content is safe
    canvas = Image.new("RGB", (w + 2 * pad, h + 2 * pad), (255, 255, 255))
    canvas.paste(out, (pad, pad))
    return clear_border_watermarks(canvas)          # 5% of padded frame stays within the pad

def _degenerate_reason(out: Image.Image) -> str | None:
    """Detect the two ways gpt-image-2 silently returns junk: a solid flat blob filling
    the frame (e.g. a black daybed collapsing to a uniform black rectangle) or a near-empty
    white frame. Returns a short reason if degenerate, else None.

    A real icon has tonal variety (cushion seams, shading, outline) plus white margins, so
    its content pixels span a range of tones. A solid fill has ONE tone -> std ~0 (measured
    at 0.0 for the black blobs vs >=8 for the faintest real icon). Empty frames have almost
    no non-white content at all."""
    bright = np.asarray(out.convert("RGB"), dtype=np.float32).mean(axis=2)
    content = bright[bright < 240]                     # non-white (the actual drawing)
    content_frac = float(content.size) / float(bright.size)
    if content_frac < 0.02:
        return f"near-empty (content_frac={content_frac:.3f})"
    content_std = float(content.std())                 # tonal variety of the drawing
    if content_std < 4.0:
        return f"flat/solid fill (content_std={content_std:.2f})"
    return None
