"""
processing.py — your computer-vision logic, platform-agnostic.

Changes from your RunPod file:
  * removed `import runpod`, `initialize_models()`, `handler()`, and the __main__ block
    (model loading now lives in modal_app.py's @modal.enter())
  * `process_image_with_masks` now takes the two models as arguments instead of
    reading them from module globals.

Everything else is byte-for-byte your original logic.
"""

import time
from datetime import datetime
import numpy as np
import cv2
from scipy.signal import savgol_filter
from scipy.ndimage import binary_fill_holes
import torch
from PIL import Image
import warnings

warnings.filterwarnings("ignore", message="torch.meshgrid: in an upcoming release")

PROCESSING_HEIGHT = 1024

# NOTE: This list MUST match the trained checkpoint's class order exactly.
# Taken verbatim from checkpoint_best_ema.pth -> args["class_names"] (65 classes).
# Order changed vs the previous 58-class model (new gym classes inserted
# alphabetically), so do NOT reuse the old list.
CLASS_NAMES = [
    "furniture-ZHtB-hQD1-chaise--tR4f",  # 0
    "2-seater-sofa",                     # 1
    "3-seater-sofa",                     # 2
    "air bike",                          # 3   (new)
    "art-canvas",                        # 4
    "barbell",                           # 5   (new)
    "bed",                               # 6
    "bedspread",                         # 7
    "boxing gloves",                     # 8   (new)
    "candle",                            # 9
    "carpet",                            # 10
    "center-table",                      # 11
    "chair",                             # 12
    "chaise-lounge",                     # 13
    "chandelier",                        # 14
    "chest press machine",               # 15  (new)
    "coffee-maker",                      # 16
    "comforter",                         # 17
    "console",                           # 18
    "cooking-appliance",                 # 19
    "cooking-pot",                       # 20
    "cup",                               # 21
    "decorative-hanger",                 # 22
    "dining-table",                      # 23
    "dressing-table",                    # 24
    "dumbbell",                          # 25
    "elliptical-machine",                # 26
    "floor-stand",                       # 27
    "flower",                            # 28
    "flower-pot-and-plant",              # 29
    "food-processor",                    # 30
    "jump rope",                         # 31  (new)
    "kettlebells",                       # 32
    "l-shape-sofa",                      # 33
    "lampshade",                         # 34
    "laundry-basket",                    # 35
    "leg press machine",                 # 36  (new)
    "lighting",                          # 37
    "mattresses",                        # 38
    "medicine-ball",                     # 39
    "office-chair",                      # 40
    "office-table",                      # 41
    "outdoor-lighting",                  # 42
    "pendant-lighting",                  # 43
    "pillow",                            # 44
    "plate",                             # 45
    "power-rack",                        # 46
    "service-table",                     # 47
    "serving-utensil-and-tray",          # 48
    "shelve",                            # 49
    "side-table",                        # 50
    "sofa",                              # 51
    "stationary-bike",                   # 52
    "statue-and-antique",                # 53
    "storage-box",                       # 54
    "treadmill",                         # 55
    "tv-table",                          # 56
    "vase",                              # 57
    "wall-clock",                        # 58
    "wall-lighting",                     # 59
    "wardrobe",                          # 60
    "weight plates",                     # 61  (new)
    "weight-bench-adjustable",           # 62
    "weight-bench-flat",                 # 63
    "yoga-mat",                          # 64
]
CLASS_NAMES_DICT = {i: name for i, name in enumerate(CLASS_NAMES)}


def ensure_numpy_array(data):
    if isinstance(data, np.ndarray):
        return data
    elif isinstance(data, list):
        if not data:
            return np.array([])
        if isinstance(data[0], np.ndarray):
            return np.stack(data) if len(data) > 1 else data[0]
        return np.array(data)
    elif hasattr(data, "cpu"):
        return data.cpu().numpy()
    elif hasattr(data, "numpy"):
        return data.numpy()
    return np.array(data)


def resize_image_for_processing(image, processing_height=PROCESSING_HEIGHT):
    width, height = image.size
    if height <= processing_height:
        return image, 1.0
    scale_factor = processing_height / height
    new_width = int(width * scale_factor)
    resized = image.resize((new_width, processing_height), Image.Resampling.LANCZOS)
    return resized, scale_factor


def scale_coordinates_to_original(coordinates, processing_scale):
    if processing_scale == 1.0:
        return coordinates
    scale_factor = 1.0 / processing_scale
    scaled = []
    for coord in coordinates:
        if isinstance(coord, list) and len(coord) == 2:
            scaled.append([coord[0] * scale_factor, coord[1] * scale_factor])
    return scaled


def scale_bbox_to_original(bbox, processing_scale):
    if processing_scale == 1.0:
        return bbox
    s = 1.0 / processing_scale
    return [bbox[0] * s, bbox[1] * s, bbox[2] * s, bbox[3] * s]


def clamp_coordinates_to_image(coordinates, image_width, image_height):
    clamped = []
    for coord in coordinates:
        if isinstance(coord, list) and len(coord) == 2:
            x = max(0, min(coord[0], image_width - 1))
            y = max(0, min(coord[1], image_height - 1))
            clamped.append([x, y])
    return clamped


def get_multi_level_masks_improved(sam_model, image_np, bbox, processing_width,
                                   processing_height, confidence_levels=[0.15]):
    h, w = image_np.shape[:2]
    combined_mask = None
    if isinstance(bbox, list):
        bbox = np.array(bbox)
    for conf in confidence_levels:
        for img_size in [1024]:
            try:
                if torch.cuda.is_available():
                    with torch.amp.autocast("cuda", dtype=torch.float16):
                        results = sam_model.predict(
                            source=image_np,
                            bboxes=bbox.reshape(1, -1) if len(bbox.shape) == 1 else bbox,
                            conf=conf, iou=0.2, imgsz=img_size, retina_masks=True,
                            save=False, verbose=False, augment=True, half=True, device="cuda:0",
                        )
                else:
                    results = sam_model.predict(
                        source=image_np,
                        bboxes=bbox.reshape(1, -1) if len(bbox.shape) == 1 else bbox,
                        conf=conf, iou=0.2, imgsz=img_size, retina_masks=True,
                        save=False, verbose=False, augment=True, half=False, device="cpu",
                    )
                if results and len(results) > 0 and hasattr(results[0], "masks") and results[0].masks is not None:
                    mask = results[0].masks.data.cpu().numpy()[0]
                    if mask.shape != (h, w):
                        mask = cv2.resize(mask.astype(np.float32), (w, h), interpolation=cv2.INTER_CUBIC)
                    mask_binary = (mask > 0.5).astype(np.uint8)
                    if combined_mask is None:
                        combined_mask = mask_binary
                    else:
                        combined_mask = np.logical_or(combined_mask, mask_binary).astype(np.uint8)
            except Exception:
                continue
    if combined_mask is not None:
        return (combined_mask * 255).astype(np.uint8)
    return None


def expand_mask_to_edges(mask, expansion_iterations=3):
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    expanded = cv2.dilate(mask, kernel, iterations=expansion_iterations)
    return cv2.medianBlur(expanded, 5)


def fill_mask_completely(mask):
    filled = binary_fill_holes(mask > 127).astype(np.uint8) * 255
    contours, _ = cv2.findContours(filled, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if len(contours) > 0:
        filled_mask = np.zeros_like(mask)
        cv2.drawContours(filled_mask, contours, -1, 255, -1)
    else:
        filled_mask = filled
    return filled_mask


def refine_mask_universal(mask, processing_width, processing_height):
    if mask.dtype != np.uint8:
        mask = (mask * 255).astype(np.uint8)
    mask = expand_mask_to_edges(mask, expansion_iterations=1)
    mask = fill_mask_completely(mask)
    mask = cv2.bilateralFilter(mask, 15, 80, 80)
    kernel_large = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel_large, iterations=1)
    mask = cv2.GaussianBlur(mask, (7, 7), 1.5)
    _, mask = cv2.threshold(mask, 127, 255, cv2.THRESH_BINARY)
    return mask


def refine_mask_flat_objects(mask, processing_width, processing_height):
    if mask.dtype != np.uint8:
        mask = (mask * 255).astype(np.uint8)
    mask = fill_mask_completely(mask)
    mask = cv2.bilateralFilter(mask, 9, 50, 50)
    kernel_small = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel_small, iterations=1)
    mask = cv2.GaussianBlur(mask, (3, 3), 0.5)
    _, mask = cv2.threshold(mask, 127, 255, cv2.THRESH_BINARY)
    return mask


def smooth_contour_advanced(contour, smoothing_factor=0.005):
    points = contour.reshape(-1, 2).astype(np.float64)
    if len(points) < 10:
        return contour
    distances = np.zeros(len(points))
    for i in range(1, len(points)):
        distances[i] = distances[i - 1] + np.linalg.norm(points[i] - points[i - 1])
    total_distance = distances[-1] + np.linalg.norm(points[0] - points[-1])
    num_points = max(50, len(points))
    new_distances = np.linspace(0, total_distance, num_points, endpoint=False)
    from scipy.interpolate import interp1d
    points_wrapped = np.vstack([points, points[0]])
    distances_wrapped = np.append(distances, total_distance)
    fx = interp1d(distances_wrapped, points_wrapped[:, 0], kind="cubic")
    fy = interp1d(distances_wrapped, points_wrapped[:, 1], kind="cubic")
    smooth_points = np.column_stack((fx(new_distances), fy(new_distances)))
    window_length = min(15, len(smooth_points) if len(smooth_points) % 2 == 1 else len(smooth_points) - 1)
    window_length = max(7, window_length)
    x_smooth = savgol_filter(smooth_points[:, 0], window_length, 3, mode="wrap")
    y_smooth = savgol_filter(smooth_points[:, 1], window_length, 3, mode="wrap")
    final_points = np.column_stack((x_smooth, y_smooth)).astype(np.int32)
    return final_points.reshape(-1, 1, 2)


def mask_to_polygon_complete(mask, processing_width, processing_height, class_name=None):
    flat_object_categories = ["art-canvas", "carpet"]
    is_flat_object = class_name is not None and class_name in flat_object_categories
    if is_flat_object:
        mask_refined = refine_mask_flat_objects(mask, processing_width, processing_height)
    else:
        mask_refined = refine_mask_universal(mask, processing_width, processing_height)
    contours, _ = cv2.findContours(mask_refined, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if len(contours) == 0:
        return []
    min_area = 100
    processed_contours = []
    for contour in contours:
        if cv2.contourArea(contour) > min_area:
            if is_flat_object:
                perimeter = cv2.arcLength(contour, True)
                simplified = cv2.approxPolyDP(contour, 0.002 * perimeter, True)
            else:
                smoothed = smooth_contour_advanced(contour)
                perimeter = cv2.arcLength(smoothed, True)
                simplified = cv2.approxPolyDP(smoothed, 0.001 * perimeter, True)
                if len(simplified) < 40:
                    from scipy.interpolate import splprep, splev
                    points = simplified.reshape(-1, 2).T
                    points = np.column_stack([points, points[:, 0]])
                    try:
                        tck, u = splprep(points, s=0, per=True)
                        u_new = np.linspace(0, 1, 60)
                        smooth_points = splev(u_new, tck)
                        simplified = np.array(smooth_points).T.astype(np.int32).reshape(-1, 1, 2)
                    except Exception:
                        pass
            processed_contours.append(simplified)
    if processed_contours:
        largest_contour = max(processed_contours, key=cv2.contourArea)
        polygon = largest_contour.reshape(-1, 2).tolist()
        return clamp_coordinates_to_image(polygon, processing_width, processing_height)
    return []


def sort_detected_objects_by_area(detected_objects):
    return sorted(detected_objects, key=lambda obj: obj.get("area", 0), reverse=True)


def process_image_with_masks(rf_detr_model, sam_model, image, confidence_threshold=0.25):
    """CHANGED SIGNATURE: models are now passed in (were module globals on RunPod)."""
    start_time = time.time()
    original_width, original_height = image.size
    processing_image, processing_scale = resize_image_for_processing(image, PROCESSING_HEIGHT)
    processing_width, processing_height = processing_image.size
    image_np = np.array(processing_image)

    if torch.cuda.is_available():
        with torch.amp.autocast("cuda", dtype=torch.float16):
            with torch.no_grad():
                detections = rf_detr_model.predict(processing_image, threshold=confidence_threshold)
    else:
        with torch.no_grad():
            detections = rf_detr_model.predict(processing_image, threshold=confidence_threshold)

    detected_objects = []
    try:
        if hasattr(detections, "boxes") and hasattr(detections.boxes, "xyxy"):
            bboxes = detections.boxes.xyxy.cpu().numpy() if detections.boxes.xyxy.is_cuda else detections.boxes.xyxy.numpy()
            if isinstance(bboxes, list):
                bboxes = np.array(bboxes) if bboxes else np.array([]).reshape(0, 4)
            elif len(bboxes.shape) == 1 and len(bboxes) > 0:
                bboxes = bboxes.reshape(1, -1)
        elif hasattr(detections, "xyxy"):
            bboxes = ensure_numpy_array(detections.xyxy)
        elif hasattr(detections, "bboxes"):
            bboxes = ensure_numpy_array(detections.bboxes)
        else:
            bboxes = np.array([])

        if hasattr(detections, "class_id"):
            class_ids = ensure_numpy_array(detections.class_id)
        elif hasattr(detections, "cls"):
            class_ids = ensure_numpy_array(detections.cls)
        else:
            class_ids = np.array([])

        if hasattr(detections, "confidence"):
            confidences = ensure_numpy_array(detections.confidence)
        elif hasattr(detections, "conf"):
            confidences = ensure_numpy_array(detections.conf)
        else:
            confidences = np.array([])

        if len(bboxes) > 0:
            for i, bbox in enumerate(bboxes):
                if i < len(class_ids) and i < len(confidences):
                    mask = get_multi_level_masks_improved(
                        sam_model, image_np, bbox, processing_width, processing_height
                    )
                    if mask is None:
                        sam_params = {
                            "imgsz": 1024, "retina_masks": True, "conf": 0.15,
                            "iou": 0.3, "max_det": 300, "augment": True,
                        }
                        if torch.cuda.is_available():
                            with torch.amp.autocast("cuda", dtype=torch.float16):
                                with torch.no_grad():
                                    sam_results = sam_model.predict(
                                        source=image_np, bboxes=bbox.reshape(1, -1),
                                        save=False, device="cuda:0", verbose=False, half=True, **sam_params,
                                    )
                        else:
                            with torch.no_grad():
                                sam_results = sam_model.predict(
                                    source=image_np, bboxes=bbox.reshape(1, -1),
                                    save=False, device="cpu", verbose=False, half=False, **sam_params,
                                )
                        if sam_results and len(sam_results) > 0:
                            result = sam_results[0]
                            if hasattr(result, "masks") and result.masks is not None:
                                mask_data = result.masks.data.cpu().numpy()[0]
                                h, w = image_np.shape[:2]
                                if mask_data.shape != (h, w):
                                    mask = cv2.resize(mask_data.astype(np.float32), (w, h), interpolation=cv2.INTER_CUBIC)
                                    mask = cv2.GaussianBlur(mask, (3, 3), 0.5)
                                    mask = (mask > 0.5).astype(np.uint8) * 255
                                else:
                                    mask = (mask_data > 0.5).astype(np.uint8) * 255

                    if mask is not None:
                        mask_indices = np.where(mask > 0)
                        if len(mask_indices[0]) > 0:
                            area = np.sum(mask > 0)
                            center_y = float(np.mean(mask_indices[0]))
                            center_x = float(np.mean(mask_indices[1]))
                            y_min, y_max = np.min(mask_indices[0]), np.max(mask_indices[0])
                            x_min, x_max = np.min(mask_indices[1]), np.max(mask_indices[1])
                            class_id = int(class_ids[i])
                            class_name = CLASS_NAMES_DICT.get(class_id, f"Unknown_{class_id}")
                            polygon = mask_to_polygon_complete(mask, processing_width, processing_height, class_name=class_name)
                            if polygon:
                                scaled_polygon = scale_coordinates_to_original(polygon, processing_scale)
                                clamped_polygon = clamp_coordinates_to_image(scaled_polygon, original_width, original_height)
                                scaled_bbox = scale_bbox_to_original([x_min, y_min, x_max, y_max], processing_scale)
                                scaled_center_x = center_x / processing_scale
                                scaled_center_y = center_y / processing_scale
                                scaled_area = int(area / (processing_scale ** 2))
                                clamped_bbox = [
                                    max(0, min(scaled_bbox[0], original_width - 1)),
                                    max(0, min(scaled_bbox[1], original_height - 1)),
                                    max(0, min(scaled_bbox[2], original_width - 1)),
                                    max(0, min(scaled_bbox[3], original_height - 1)),
                                ]
                                clamped_center_x = max(0, min(scaled_center_x, original_width - 1))
                                clamped_center_y = max(0, min(scaled_center_y, original_height - 1))
                                detected_objects.append({
                                    "label": class_name,
                                    "confidence": float(confidences[i]),
                                    "mask_polygon": clamped_polygon,
                                    "polygon_points": len(clamped_polygon),
                                    "bbox_from_mask": [float(x) for x in clamped_bbox],
                                    "area": scaled_area,
                                    "center": [clamped_center_x, clamped_center_y],
                                })
    except Exception as e:
        print(f"Error processing detections: {e}")
        import traceback
        traceback.print_exc()

    processing_time = time.time() - start_time
    sorted_objects = sort_detected_objects_by_area(detected_objects)
    return {
        "detected_objects": sorted_objects,
        "processing_time": round(processing_time, 3),
        "frame_size": [original_width, original_height],
        "original_dimensions": [original_width, original_height],
        "processing_dimensions": [processing_width, processing_height],
        "timestamp": datetime.now().isoformat(),
    }
