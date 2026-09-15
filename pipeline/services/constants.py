SMALL_CATEGORIES = [
    "cup",
    "plate",
    "cooking-pot",
    "coffee-maker",
    "cooking-appliance",
    "food-processor",
    "candle",
    "vase",
    "flower",
    "statue-and-antique",
]

MEDIUM_CATEGORIES = [
    "chair",
    "office-chair",
    "side-table",
    "console",
    "center-table",
    "service-table",
    "tv-table",
    "storage-box",
    "shelve",
    "pillow",
    "lighting",
    "lampshade",
    "wall-lighting",
    "outdoor-lighting",
    "chandelier",
    "pendant-lighting",
    "floor-stand",
    "decorative-hanger",
    "wall-clock",
    "art-canvas",
    "laundry-basket",
    "serving-utensil-and-tray",
    "flower-pot-and-plant",
]
LARGE_CATEGORIES = [
    "3-seater-sofa",
    "2-seater-sofa",
    "l-shape-sofa",
    "sofa",
    "chaise-lounge",
    "bed",
    "bedspread",
    "mattresses",
    "comforter",
    "dressing-table",
    "office-table",
    "dining-table",
    "carpet",
    "wardrobe",
]

# === IMAGE QUALITY THRESHOLDS ===

# File & Dimension Limits
MAX_FILE_SIZE_MB = 10
MIN_DIMENSION_UNIVERSAL = 500  # Both width AND height for ALL categories
MAX_DIMENSION = 4096

# # Blur Detection (Laplacian Variance) - 3-TIER SYSTEM
# BLUR_THRESHOLDS = {
#     "small": {
#         "reject": 30,  # Small items - moderate (was 40)
#         "warn": 70,
#     },
#     "medium": {
#         "reject": 25,  # Medium items - lenient
#         "warn": 65,
#     },
#     "large": {
#         "reject": 20,  # Large items - very lenient (textiles)
#         "warn": 60,
#     },
#     "default": {
#         "reject": 25,  # Universal threshold for retrieval (when category unknown)
#         "warn": 65,
#     },
# }



# CONTRAST_THRESHOLDS = {
#     "reject_min": 1.0,  # Blank/uniform image - hard rejection
#     "warn_min": 10.0,  # Low contrast - warning
# }

# === STAGE 2: ROA (Ratio of Area) THRESHOLDS - 3-TIER SYSTEM ===
# NOTE: ROA validation only for INGESTION, NOT for retrieval
# Retrieval skips ROA to be user-friendly (room photo framing varies)

ROA_THRESHOLDS = {
    "small": {
        "min": 0.06,  # 6% minimum - Small objects (cups, plates, candles)

    },
    "medium": {
        "min": 0.08,  # 8% minimum - Medium objects (chairs, lamps, small tables)

    },
    "large": {
        "min": 0.12,  # 12% minimum - Large objects (sofas, beds, dining tables)

    },
}



# Medium crop padding (fixed ratio)
MEDIUM_PADDING_RATIO = 0.15  # 15% padding around object

# Augmentation settings
ROTATION_ANGLES = [-5, 5]
ENABLE_HORIZONTAL_FLIP = True
ENABLE_VERTICAL_FLIP = False

# Crop strategy per tier
CROP_STRATEGY = {
    "small": ["tight"],                  # Small: tight only (4 crops)
    "medium": ["tight"],                 # Medium: tight only (4 crops)
    "large": ["tight", "medium", "full"],  # Large: all crops (9 crops)
}
