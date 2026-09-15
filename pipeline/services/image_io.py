from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

import numpy as np
import cv2
import requests
from PIL import Image
import logging

from .constants import (
    SMALL_CATEGORIES,
    MEDIUM_CATEGORIES,
    LARGE_CATEGORIES,
    MAX_FILE_SIZE_MB,
    MIN_DIMENSION_UNIVERSAL,
    MAX_DIMENSION,
)

logger = logging.getLogger(__name__)


@dataclass
class ImageIOService:

    min_dimension: int = MIN_DIMENSION_UNIVERSAL
    max_dimension: int = MAX_DIMENSION
    max_file_size_mb: int = MAX_FILE_SIZE_MB

    # def get_category_requirements(self, category: str) -> dict:
    #     """
    #     Get quality requirements based on product category (3-tier system).
    #
    #     Different tiers have different quality standards:
    #     - Small objects (cups, plates) - blur ≥ 30, ROA ≥ 8%
    #     - Medium objects (chairs, lamps) - blur ≥ 25, ROA ≥ 12%
    #     - Large objects (sofas, beds) - blur ≥ 20, ROA ≥ 15%
    #
    #     Args:
    #         category: Product category (e.g., "cup", "chair", "bed")
    #
    #     Returns:
    #         Dictionary with blur_threshold, blur_warning, category_size
    #
    #     Raises:
    #         ValueError: If category is not in predefined lists
    #     """
    #     category_lower = category.lower().strip()
    #
    #     if category_lower in SMALL_CATEGORIES:
    #         return {
    #             "blur_threshold": BLUR_THRESHOLDS["small"]["reject"],
    #             "blur_warning": BLUR_THRESHOLDS["small"]["warn"],
    #             "category_size": "small"
    #         }
    #     elif category_lower in MEDIUM_CATEGORIES:
    #         return {
    #             "blur_threshold": BLUR_THRESHOLDS["medium"]["reject"],
    #             "blur_warning": BLUR_THRESHOLDS["medium"]["warn"],
    #             "category_size": "medium"
    #         }
    #     elif category_lower in LARGE_CATEGORIES:
    #         return {
    #             "blur_threshold": BLUR_THRESHOLDS["large"]["reject"],
    #             "blur_warning": BLUR_THRESHOLDS["large"]["warn"],
    #             "category_size": "large"
    #         }
    #     else:
    #         # Fail fast for unknown categories - forces data quality
    #         raise ValueError(
    #             f"Unknown category '{category}'. "
    #             f"Category must be one of {len(SMALL_CATEGORIES)} small, "
    #             f"{len(MEDIUM_CATEGORIES)} medium, or {len(LARGE_CATEGORIES)} large categories. "
    #             f"Please check your data source."
    #         )

    # def compute_blur_score(self, img: Image.Image) -> float:
    #     """
    #     Compute blur score using Laplacian variance method.
    #
    #     Higher score = sharper image
    #     Lower score = blurrier image
    #
    #     Typical ranges:
    #     - < 30: Extremely blurry (unusable)
    #     - 30-40: Very blurry (reject for small objects)
    #     - 40-100: Acceptable sharpness
    #     - 100-200: Good sharpness
    #     - > 200: Excellent sharpness
    #
    #     Args:
    #         img: PIL Image in RGB mode
    #
    #     Returns:
    #         Blur score (variance of Laplacian operator)
    #     """
    #     # Convert PIL to numpy array
    #     img_array = np.array(img)
    #
    #     # Convert to grayscale for edge detection
    #     gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
    #
    #     # Apply Laplacian operator (detects edges)
    #     # Higher variance = more edges = sharper image
    #     laplacian = cv2.Laplacian(gray, cv2.CV_64F)
    #     blur_score = laplacian.var()
    #
    #     return blur_score

    def validate_image_quality(self, img: Image.Image) -> None:
        if img.mode != "RGB":
            img = img.convert("RGB")

        w, h = img.size
        if w < self.min_dimension or h < self.min_dimension:
            raise ValueError({
                "error": "This image is too small. Minimum size is 500 × 500 pixels.",
                "errorArabic": "هذه الصورة صغيرة جدًا. الحد الأدنى لحجمها هو 500 × 500 بكسل"
            })

        # try:
        #     blur_score = self.compute_blur_score(img)
        #     blur_threshold = BLUR_THRESHOLDS["default"]["reject"]
        #
        #     if blur_score < blur_threshold:
        #         raise ValueError(
        #             f"Image too blurry for reliable detection. "
        #             f"Please use a sharper, higher-quality image."
        #         )
        # except ValueError:
        #     raise
        # except Exception as e:
        #     raise ValueError(f"Failed to validate image blur: {str(e)}")

        # img_array = np.array(img)
        # std_dev = img_array.std()
        #
        # if std_dev < CONTRAST_THRESHOLDS["reject_min"]:
        #     raise ValueError({
        #         "error": "This image appears blank or invalid. Please upload a better image",
        #         "errorArabic": "هذه الصورة فارغة أو غير صالحة. يرجى تحميل صورة صحيحة"
        #     })

    def read_from_url(
            self,
            image_url: str,
            category: str | None = None,
            allow_downscale: bool = True,
    ) -> Image.Image:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
        }

        try:
            response = requests.get(image_url, headers=headers, timeout=30)
            response.raise_for_status()
            data = response.content
        except requests.RequestException as e:
            raise ValueError(
                f"Failed to download image from URL: {str(e)}. "
                f"Please ensure the URL is accessible."
            )

        size_mb = len(data) / (1024 * 1024)
        if size_mb > self.max_file_size_mb:
            raise ValueError(
                f"Image file too large ({size_mb:.1f}MB). "
                f"Maximum allowed: {self.max_file_size_mb}MB. "
                f"Please compress or resize your image."
            )

        try:
            img = Image.open(BytesIO(data))
        except Exception as e:
            raise ValueError(
                f"Invalid or corrupted image file: {str(e)}. "
                f"Please ensure the file is a valid image (JPEG, PNG, WebP)."
            )

        if img.mode != "RGB":
            img = img.convert("RGB")

        w, h = img.size

        if w < self.min_dimension or h < self.min_dimension:
            raise ValueError(
                f"Image too small ({w}x{h} pixels). "
                f"Minimum dimension: {self.min_dimension}px."
            )

        # Only downscale when the caller can tolerate the image grid changing.
        # The bbox/mask_polygon search path passes allow_downscale=False so the
        # loaded image stays in the exact pixel space the polygon coordinates were
        # computed in — resizing here without rescaling the polygon would land the
        # crop/mask on the wrong pixels.
        if allow_downscale and (w > self.max_dimension or h > self.max_dimension):
            img.thumbnail((self.max_dimension, self.max_dimension), Image.Resampling.LANCZOS)

        return img

    def pil_to_numpy_rgb(self, img: Image.Image) -> np.ndarray:
        return np.array(img, dtype=np.uint8)