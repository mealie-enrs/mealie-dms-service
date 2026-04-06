"""Batch pipeline for curating the raw RecipeNLG CSV into versioned parquet."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

import pyarrow as pa
import pyarrow.parquet as pq

from dms.config import settings
from dms.storage import put_bytes, put_text, require_swift

log = logging.getLogger(__name__)

RAW_CSV_OBJECT_KEY = "recipenlg/RecipeNLG_dataset.csv"
VERSION_PREFIX = "recipenlg_versions"
SPLIT_RATIOS = {"train": 0.70, "val": 0.15, "test": 0.15}


@dataclass
class RecipeNLGStats:
    input_rows: int = 0
    output_rows: int = 0
    dropped_missing_title: int = 0
    dropped_missing_ingredients: int = 0
    dropped_duplicates: int = 0
    train: int = 0
    val: int = 0
    test: int = 0


def _deterministic_split(group_key: str) -> str:
    digest = int(hashlib.sha256(group_key.encode("utf-8")).hexdigest(), 16)
    bucket = (digest % 10000) / 10000.0
    if bucket < SPLIT_RATIOS["train"]:
        return "train"
    if bucket < SPLIT_RATIOS["train"] + SPLIT_RATIOS["val"]:
        return "val"
    return "test"


def _normalize_text(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    return text


def _clean_text(value: str) -> str:
    return _normalize_text(value).lower()


def _parse_count(value: str) -> int:
    if not value:
        return 0
    if value.startswith("[") and value.endswith("]"):
        return value.count(",") + 1 if len(value) > 2 else 0
    return len([part for part in re.split(r"[|\n;]", value) if part.strip()])


def _field(row: dict[str, str], *names: str) -> str:
    lowered = {k.lower(): v for k, v in row.items()}
    for name in names:
        if name.lower() in lowered and lowered[name.lower()] is not None:
            return str(lowered[name.lower()])
    return ""


def compile_recipenlg_dataset(
    *,
    container: str | None = None,
    source_key: str = RAW_CSV_OBJECT_KEY,
) -> tuple[list[dict[str, object]], RecipeNLGStats]:
    container = container or settings.swift_training_container
    conn = require_swift()
    _, data = conn.get_object(container, source_key)
    reader = csv.DictReader(io.StringIO(data.decode("utf-8", errors="replace")))

    seen_keys: set[str] = set()
    rows: list[dict[str, object]] = []
    stats = RecipeNLGStats()

    for raw_row in reader:
        stats.input_rows += 1

        title = _normalize_text(_field(raw_row, "title"))
        ingredients = _normalize_text(_field(raw_row, "ingredients", "ingredients_raw"))
        directions = _normalize_text(_field(raw_row, "directions", "instructions"))
        source = _normalize_text(_field(raw_row, "source"))
        link = _normalize_text(_field(raw_row, "link"))
        ner = _normalize_text(_field(raw_row, "ner"))

        if not title:
            stats.dropped_missing_title += 1
            continue
        if not ingredients:
            stats.dropped_missing_ingredients += 1
            continue

        dedupe_key = f"{_clean_text(title)}||{_clean_text(ingredients)}"
        if dedupe_key in seen_keys:
            stats.dropped_duplicates += 1
            continue
        seen_keys.add(dedupe_key)

        recipe_id = hashlib.sha256(dedupe_key.encode("utf-8")).hexdigest()
        split = _deterministic_split(dedupe_key)

        row = {
            "recipe_id": recipe_id,
            "title": title,
            "ingredients_raw": ingredients,
            "directions_raw": directions,
            "source": source,
            "link": link,
            "ner": ner,
            "clean_title": _clean_text(title),
            "clean_ingredients": _clean_text(ingredients),
            "ingredient_count": _parse_count(ingredients),
            "instruction_count": _parse_count(directions),
            "split": split,
        }
        rows.append(row)
        stats.output_rows += 1
        if split == "train":
            stats.train += 1
        elif split == "val":
            stats.val += 1
        else:
            stats.test += 1

    log.info(
        "RecipeNLG stats: input=%d output=%d missing_title=%d missing_ingredients=%d duplicates=%d",
        stats.input_rows,
        stats.output_rows,
        stats.dropped_missing_title,
        stats.dropped_missing_ingredients,
        stats.dropped_duplicates,
    )
    return rows, stats


def write_recipenlg_outputs(
    rows: list[dict[str, object]],
    stats: RecipeNLGStats,
    *,
    version: str = "v1",
    container: str | None = None,
    source_key: str = RAW_CSV_OBJECT_KEY,
) -> tuple[str, str]:
    container = container or settings.swift_training_container

    table = pa.Table.from_pylist(rows)
    buffer = io.BytesIO()
    pq.write_table(table, buffer)

    parquet_key = f"{VERSION_PREFIX}/{version}/recipes.parquet"
    put_bytes(container, parquet_key, buffer.getvalue(), "application/octet-stream")

    meta_key = f"{VERSION_PREFIX}/{version}/meta.json"
    meta = {
        "dataset": "recipenlg",
        "version": version,
        "source_key": source_key,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "input_rows": stats.input_rows,
        "output_rows": stats.output_rows,
        "dropped_missing_title": stats.dropped_missing_title,
        "dropped_missing_ingredients": stats.dropped_missing_ingredients,
        "dropped_duplicates": stats.dropped_duplicates,
        "split_counts": {"train": stats.train, "val": stats.val, "test": stats.test},
        "columns": [
            "recipe_id",
            "title",
            "ingredients_raw",
            "directions_raw",
            "source",
            "link",
            "ner",
            "clean_title",
            "clean_ingredients",
            "ingredient_count",
            "instruction_count",
            "split",
        ],
    }
    put_text(container, meta_key, json.dumps(meta, indent=2), "application/json")
    return parquet_key, meta_key
