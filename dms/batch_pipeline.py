"""Batch pipeline: compile versioned training / evaluation datasets.

Reads the Kaggle food-images CSV from Swift, matches images to labels,
applies candidate selection, splits by recipe identity to avoid leakage,
and writes a versioned Parquet manifest back to Swift.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
from dataclasses import dataclass, field

import pyarrow as pa
import pyarrow.parquet as pq

from dms.config import settings
from dms.storage import put_bytes, put_text, require_swift

log = logging.getLogger(__name__)

SPLIT_RATIOS = {"train": 0.70, "val": 0.15, "test": 0.15}
MIN_IMAGE_BYTES = 1024  # skip images smaller than 1 KB (likely corrupt/placeholder)

CSV_OBJECT_KEY = (
    "recipe1m/kaggle-food-images/"
    "Food Ingredients and Recipe Dataset with Image Name Mapping.csv"
)
IMAGE_PREFIX = "recipe1m/kaggle-food-images/Food Images/Food Images/"


@dataclass
class RecipeRecord:
    title: str
    ingredients: str
    cleaned_ingredients: str
    image_name: str
    object_key: str
    size_bytes: int
    split: str = ""


def _deterministic_split(group_key: str) -> str:
    """Hash-based deterministic split so the same recipe always lands in the
    same partition, preventing data leakage across splits."""
    digest = int(hashlib.sha256(group_key.encode()).hexdigest(), 16)
    bucket = (digest % 10000) / 10000.0
    if bucket < SPLIT_RATIOS["train"]:
        return "train"
    elif bucket < SPLIT_RATIOS["train"] + SPLIT_RATIOS["val"]:
        return "val"
    return "test"


def _load_csv_from_swift(container: str) -> list[dict]:
    """Download the label CSV from Swift and parse it."""
    conn = require_swift()
    _, data = conn.get_object(container, CSV_OBJECT_KEY)
    text = data.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    log.info("Loaded %d rows from CSV", len(rows))
    return rows


def _list_images_from_swift(container: str) -> dict[str, int]:
    """List all images under the kaggle prefix and return {filename: size_bytes}."""
    conn = require_swift()
    image_map: dict[str, int] = {}
    marker = ""
    while True:
        _, objects = conn.get_container(
            container, prefix=IMAGE_PREFIX, limit=10000, marker=marker
        )
        if not objects:
            break
        for obj in objects:
            name = obj["name"]
            if name.lower().endswith((".jpg", ".jpeg", ".png")):
                filename = name[len(IMAGE_PREFIX):]
                image_map[filename] = int(obj.get("bytes", 0))
        marker = objects[-1]["name"]
    log.info("Found %d images in Swift", len(image_map))
    return image_map


@dataclass
class PipelineStats:
    csv_rows: int = 0
    images_in_swift: int = 0
    matched: int = 0
    skipped_no_image: int = 0
    skipped_too_small: int = 0
    skipped_duplicate: int = 0
    train: int = 0
    val: int = 0
    test: int = 0


def compile_dataset(
    container: str | None = None,
    version: str = "v1",
) -> tuple[list[RecipeRecord], PipelineStats]:
    """Run the full batch pipeline and return records + stats."""
    container = container or settings.swift_training_container
    stats = PipelineStats()

    csv_rows = _load_csv_from_swift(container)
    stats.csv_rows = len(csv_rows)

    image_map = _list_images_from_swift(container)
    stats.images_in_swift = len(image_map)

    seen_titles: set[str] = set()
    records: list[RecipeRecord] = []

    for row in csv_rows:
        title = (row.get("Title") or "").strip()
        image_name = (row.get("Image_Name") or "").strip()
        ingredients = (row.get("Ingredients") or "").strip()
        cleaned = (row.get("Cleaned_Ingredients") or "").strip()

        if not image_name or not title:
            stats.skipped_no_image += 1
            continue

        filename = f"{image_name}.jpg"
        if filename not in image_map:
            stats.skipped_no_image += 1
            continue

        size = image_map[filename]
        if size < MIN_IMAGE_BYTES:
            stats.skipped_too_small += 1
            continue

        norm_title = title.lower().strip()
        if norm_title in seen_titles:
            stats.skipped_duplicate += 1
            continue
        seen_titles.add(norm_title)

        split = _deterministic_split(norm_title)
        object_key = f"{IMAGE_PREFIX}{filename}"

        records.append(
            RecipeRecord(
                title=title,
                ingredients=ingredients,
                cleaned_ingredients=cleaned,
                image_name=image_name,
                object_key=object_key,
                size_bytes=size,
                split=split,
            )
        )
        stats.matched += 1

    for r in records:
        if r.split == "train":
            stats.train += 1
        elif r.split == "val":
            stats.val += 1
        else:
            stats.test += 1

    log.info(
        "Pipeline stats: matched=%d train=%d val=%d test=%d "
        "skipped_no_image=%d skipped_small=%d skipped_dup=%d",
        stats.matched, stats.train, stats.val, stats.test,
        stats.skipped_no_image, stats.skipped_too_small, stats.skipped_duplicate,
    )
    return records, stats


def write_manifest(
    records: list[RecipeRecord],
    stats: PipelineStats,
    container: str | None = None,
    version: str = "v1",
) -> tuple[str, str]:
    """Write Parquet manifest and meta.json to Swift. Returns (manifest_key, meta_key)."""
    import json
    from datetime import datetime

    container = container or settings.swift_training_container

    rows = [
        {
            "object_key": r.object_key,
            "title": r.title,
            "ingredients": r.ingredients,
            "cleaned_ingredients": r.cleaned_ingredients,
            "image_name": r.image_name,
            "split": r.split,
            "size_bytes": r.size_bytes,
        }
        for r in records
    ]

    table = pa.Table.from_pylist(rows)
    buf = io.BytesIO()
    pq.write_table(table, buf)

    manifest_key = f"versions/{version}/manifest.parquet"
    put_bytes(container, manifest_key, buf.getvalue(), "application/octet-stream")
    log.info("Wrote manifest: %s (%d rows)", manifest_key, len(rows))

    meta = {
        "version": version,
        "dataset": "kaggle-food-images",
        "created_at": datetime.utcnow().isoformat() + "Z",
        "total_records": stats.matched,
        "split_counts": {"train": stats.train, "val": stats.val, "test": stats.test},
        "candidate_selection": {
            "csv_rows": stats.csv_rows,
            "images_in_swift": stats.images_in_swift,
            "skipped_no_image": stats.skipped_no_image,
            "skipped_too_small": stats.skipped_too_small,
            "skipped_duplicate": stats.skipped_duplicate,
        },
        "split_strategy": "deterministic hash on recipe title (70/15/15)",
        "leakage_avoidance": "split by recipe identity — same recipe always in same split",
    }
    meta_key = f"versions/{version}/meta.json"
    put_text(container, meta_key, json.dumps(meta, indent=2), "application/json")
    log.info("Wrote meta: %s", meta_key)

    return manifest_key, meta_key
