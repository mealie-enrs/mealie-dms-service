"""
Synthetic data expansion using Albumentations.

For datasets under 5 GB, we augment training images to produce more diverse
training examples without collecting new data. Each image produces N variants
using photometric and geometric transforms that are realistic for food images.

Augmentation is applied ONLY to the train split to avoid leakage into
validation / test sets.
"""
from __future__ import annotations

import io
import logging

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

# Number of augmented copies to generate per training image.
AUGMENT_FACTOR = 3


def _build_pipeline(min_dim: int = 224):
    """Build the Albumentations augmentation pipeline for food images."""
    import albumentations as A

    # SmallestMaxSize ensures the image is at least min_dim on its shortest side
    # before any crop — prevents RandomCrop from exceeding image dimensions.
    return A.Compose([
        A.SmallestMaxSize(max_size=min_dim, p=1.0),
        A.HorizontalFlip(p=0.5),
        A.RandomBrightnessContrast(brightness_limit=0.2, contrast_limit=0.2, p=0.7),
        A.HueSaturationValue(hue_shift_limit=10, sat_shift_limit=20, val_shift_limit=10, p=0.5),
        A.RandomRotate90(p=0.3),
        A.ShiftScaleRotate(shift_limit=0.05, scale_limit=0.1, rotate_limit=15, p=0.4),
        A.GaussianBlur(blur_limit=(3, 5), p=0.2),
        A.CLAHE(p=0.2),
        A.RandomCrop(height=min_dim, width=min_dim, p=0.3),
        A.Resize(height=min_dim, width=min_dim, p=1.0),
    ])


_PIPELINE = None


def get_pipeline():
    global _PIPELINE
    if _PIPELINE is None:
        _PIPELINE = _build_pipeline()
    return _PIPELINE


def augment_image_bytes(raw_bytes: bytes, n: int = AUGMENT_FACTOR) -> list[bytes]:
    """
    Take raw image bytes and return n augmented variants as JPEG bytes.
    Used during batch pipeline to expand training data.
    """
    try:
        image = np.array(Image.open(io.BytesIO(raw_bytes)).convert("RGB"))
    except Exception as exc:
        log.warning("Failed to decode image for augmentation: %s", exc)
        return []

    pipeline = get_pipeline()
    results = []
    for _ in range(n):
        try:
            augmented = pipeline(image=image)["image"]
            buf = io.BytesIO()
            Image.fromarray(augmented).save(buf, format="JPEG", quality=90)
            results.append(buf.getvalue())
        except Exception as exc:
            log.warning("Augmentation step failed: %s", exc)

    return results


def augment_record(
    raw_bytes: bytes,
    base_payload: dict,
    n: int = AUGMENT_FACTOR,
) -> list[dict]:
    """
    Augment a single image and return a list of {bytes, payload} dicts
    where payload is a copy of base_payload with augmentation metadata added.
    """
    variants = augment_image_bytes(raw_bytes, n)
    return [
        {
            "bytes": v,
            "payload": {
                **base_payload,
                "augmented": True,
                "augment_index": i,
                "source_object_key": base_payload.get("object_key", ""),
            },
        }
        for i, v in enumerate(variants)
    ]
