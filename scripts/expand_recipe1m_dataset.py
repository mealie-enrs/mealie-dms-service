#!/usr/bin/env python3
"""Expand the Recipe1M training split with deterministic synthetic images.

This script reads an existing versioned manifest, augments only the training
split to avoid leakage, uploads the synthetic images back to Swift, and writes
an expanded versioned manifest and metadata JSON.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from PIL import Image, ImageEnhance, ImageOps

from dms.config import settings
from dms.storage import get_bytes, put_bytes, put_text, require_swift


def _augment_image(raw: bytes, iteration: int) -> bytes:
    image = Image.open(io.BytesIO(raw)).convert("RGB")

    rotations = [0, 90, 180, 270]
    image = image.rotate(rotations[iteration % len(rotations)], expand=True)

    if iteration % 2 == 0:
        image = ImageOps.mirror(image)

    width, height = image.size
    crop_margin_w = int(width * (0.04 + 0.01 * (iteration % 3)))
    crop_margin_h = int(height * (0.04 + 0.01 * ((iteration + 1) % 3)))
    left = min(crop_margin_w, max(0, width // 8))
    top = min(crop_margin_h, max(0, height // 8))
    right = max(width - left, left + 1)
    bottom = max(height - top, top + 1)
    image = image.crop((left, top, right, bottom)).resize((width, height))

    brightness = 0.9 + 0.05 * (iteration % 5)
    image = ImageEnhance.Brightness(image).enhance(brightness)

    out = io.BytesIO()
    image.save(out, format="JPEG", quality=95)
    return out.getvalue()


def _container_bytes_used(container: str) -> int:
    headers = require_swift().head_container(container)
    return int(headers.get("x-container-bytes-used", "0"))


def _synthetic_object_key(prefix: str, source_key: str, iteration: int, version: str) -> str:
    source_name = Path(source_key).stem
    digest = hashlib.sha256(f"{version}|{source_key}|{iteration}".encode("utf-8")).hexdigest()
    return f"{prefix.rstrip('/')}/{digest[0:2]}/{digest[2:4]}/{source_name}-aug-{iteration:06d}.jpg"


def main() -> None:
    parser = argparse.ArgumentParser(description="Expand Recipe1M training data with deterministic augmentations.")
    parser.add_argument("--container", default=settings.swift_training_container)
    parser.add_argument("--source-manifest-key", default="recipe1m_versions/v1/manifest.parquet")
    parser.add_argument("--output-version", default="v1_augmented")
    parser.add_argument("--target-total-gb", type=float, default=5.1)
    parser.add_argument("--max-new-images", type=int, default=10000)
    parser.add_argument(
        "--synthetic-prefix",
        default=None,
        help="Swift prefix for synthetic image objects. Defaults to recipe1m_augmented/{output-version}",
    )
    args = parser.parse_args()

    synthetic_prefix = args.synthetic_prefix or f"recipe1m_augmented/{args.output_version}"
    target_total_bytes = int(args.target_total_gb * 1_000_000_000)

    before_bytes = _container_bytes_used(args.container)
    raw_manifest = get_bytes(args.container, args.source_manifest_key)
    source_table = pq.read_table(io.BytesIO(raw_manifest))
    base_rows = source_table.to_pylist()

    train_rows = sorted(
        [row for row in base_rows if str(row.get("split") or "") == "train"],
        key=lambda row: str(row["object_key"]),
    )
    if not train_rows:
        raise SystemExit("No train rows found in source manifest.")

    current_bytes = before_bytes
    generated_rows: list[dict] = []
    generated_bytes = 0
    iteration = 0

    while current_bytes < target_total_bytes and iteration < args.max_new_images:
        source_row = train_rows[iteration % len(train_rows)]
        source_key = str(source_row["object_key"])
        augmented = _augment_image(get_bytes(args.container, source_key), iteration)
        object_key = _synthetic_object_key(synthetic_prefix, source_key, iteration, args.output_version)
        put_bytes(args.container, object_key, augmented, "image/jpeg")

        generated_bytes += len(augmented)
        current_bytes += len(augmented)

        new_row = dict(source_row)
        new_row["object_key"] = object_key
        new_row["image_name"] = f"{Path(source_key).stem}-aug-{iteration:06d}"
        new_row["size_bytes"] = len(augmented)
        new_row["split"] = "train"
        new_row["synthetic"] = True
        new_row["source_object_key"] = source_key
        new_row["augmentation_index"] = iteration
        generated_rows.append(new_row)

        iteration += 1
        print(
            f"[{iteration}] synthetic={Path(object_key).name} "
            f"source={Path(source_key).name} bytes_added={generated_bytes}"
        )

    expanded_rows = []
    for row in base_rows:
        expanded = dict(row)
        expanded.setdefault("synthetic", False)
        expanded.setdefault("source_object_key", str(row.get("object_key") or ""))
        expanded.setdefault("augmentation_index", None)
        expanded_rows.append(expanded)
    expanded_rows.extend(generated_rows)

    manifest_key = f"recipe1m_versions/{args.output_version}/manifest.parquet"
    meta_key = f"recipe1m_versions/{args.output_version}/meta.json"

    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(expanded_rows), buffer)
    put_bytes(args.container, manifest_key, buffer.getvalue(), "application/octet-stream")

    after_bytes = _container_bytes_used(args.container)
    meta = {
        "dataset": "recipe1m",
        "version": args.output_version,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "source_manifest_key": args.source_manifest_key,
        "synthetic_prefix": synthetic_prefix,
        "container": args.container,
        "container_bytes_before": before_bytes,
        "container_bytes_after": after_bytes,
        "target_total_bytes": target_total_bytes,
        "target_total_gb": args.target_total_gb,
        "base_row_count": len(base_rows),
        "synthetic_row_count": len(generated_rows),
        "total_row_count": len(expanded_rows),
        "synthetic_bytes_added": generated_bytes,
        "max_new_images": args.max_new_images,
        "leakage_avoidance": "only training-split rows are augmented; validation and test rows are unchanged",
        "augmentation_policy": {
            "rotation": [0, 90, 180, 270],
            "mirror_every_other_image": True,
            "crop_resize": True,
            "brightness_schedule": "0.9 + 0.05 * (iteration % 5)",
        },
    }
    put_text(args.container, meta_key, json.dumps(meta, indent=2), "application/json")
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
