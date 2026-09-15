from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image
import logging

from .schemas import BBox
from .constants import MEDIUM_PADDING_RATIO

logger = logging.getLogger(__name__)


@dataclass
class PreprocessingService:
    """
    Image preprocessing service.

    Features:
    - Two-crop generation (tight, medium) with fixed 15% padding
    - Mask application with white background
    - Bbox validation and clamping
    """
    
    def clamp_bbox(self, bbox: BBox, w: int, h: int) -> BBox:
        x1 = max(0, min(w - 1, bbox.x1))
        y1 = max(0, min(h - 1, bbox.y1))
        x2 = max(0, min(w, bbox.x2))
        y2 = max(0, min(h, bbox.y2))
        if x2 <= x1 or y2 <= y1:
            raise ValueError("Invalid bbox after clamping")
        return BBox(x1=x1, y1=y1, x2=x2, y2=y2)

    def crop_base(self, img: Image.Image, bbox: BBox) -> tuple[dict[str, Image.Image], dict[str, BBox]]:
        """
        Generate tight and medium crops without augmentation.

        - Tight: Exact bbox crop (pure object)
        - Medium: Bbox + fixed 15% padding (object with context)

        Args:
            img: Input image
            bbox: Bounding box for object

        Returns:
            Tuple of (crops_dict, bboxes_dict)
        """
        w, h = img.size

        obj_w = bbox.x2 - bbox.x1
        obj_h = bbox.y2 - bbox.y1
        obj_size = max(obj_w, obj_h)

        tight_crop = img.crop((bbox.x1, bbox.y1, bbox.x2, bbox.y2))
        tight_bbox = bbox

        pad = int(MEDIUM_PADDING_RATIO * obj_size)
        mx1 = max(0, bbox.x1 - pad)
        my1 = max(0, bbox.y1 - pad)
        mx2 = min(w, bbox.x2 + pad)
        my2 = min(h, bbox.y2 + pad)

        medium_crop = img.crop((mx1, my1, mx2, my2))
        medium_bbox = BBox(x1=mx1, y1=my1, x2=mx2, y2=my2)

        logger.debug(
            f"Crops generated: tight={obj_w:.0f}x{obj_h:.0f}px, "
            f"medium={mx2-mx1:.0f}x{my2-my1:.0f}px (15% padding)"
        )

        crops = {"tight": tight_crop, "medium": medium_crop}
        bboxes = {"tight": tight_bbox, "medium": medium_bbox}

        return crops, bboxes

    def apply_mask_on_crop(
        self, 
        crop: Image.Image, 
        mask: np.ndarray | None,
        bbox: BBox | None = None
    ) -> Image.Image:
        """
        Apply mask to crop image, removing background for cleaner embeddings.
        
        Uses polygon mask to isolate the product from background clutter.
        Background is replaced with pure white (255, 255, 255) for consistency.
        
        Benefits of white background:
        - Removes color bias (same product in different rooms = same background)
        - Product-catalog style (like Amazon/e-commerce)
        - Consistent embeddings across all products
        
        Args:
            crop: The cropped image
            mask: Full image mask (H, W) as boolean array from polygon segmentation
            bbox: Bounding box used for crop (to extract mask region)
            
        Returns:
            Masked crop image with white background
        """
        if mask is None:
            logger.debug("No mask provided, returning original crop")
            return crop
        
        try:
            crop_array = np.array(crop)
            
            if bbox is not None:
                crop_mask = mask[bbox.y1:bbox.y2, bbox.x1:bbox.x2]
                if crop_mask.shape[:2] != crop_array.shape[:2]:
                    from PIL import Image as PILImage
                    mask_img = PILImage.fromarray(crop_mask.astype(np.uint8) * 255)
                    mask_img = mask_img.resize(
                        (crop_array.shape[1], crop_array.shape[0]),
                        Image.Resampling.NEAREST
                    )
                    crop_mask = np.array(mask_img) > 128
            else:
                crop_mask = mask
                if crop_mask.shape[:2] != crop_array.shape[:2]:
                    logger.warning(
                        f"Mask size mismatch: {crop_mask.shape} vs crop {crop_array.shape}, "
                        f"returning original crop"
                    )
                    return crop
            masked_array = crop_array.copy()
            if crop_mask.sum() > 0:
                # Replace background with white (255, 255, 255)
                masked_array[~crop_mask] = [255, 255, 255]  # Pure white RGB
                
                coverage = crop_mask.sum() / crop_mask.size * 100
                logger.debug(f"Applied mask with white background ({coverage:.1f}% foreground coverage)")
            else:
                logger.warning("Empty mask, returning original crop")
                return crop
            
            return Image.fromarray(masked_array)
            
        except Exception as e:
            logger.error(f"Error applying mask to crop: {e}")
            return crop
