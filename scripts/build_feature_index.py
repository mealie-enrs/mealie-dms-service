#!/usr/bin/env python3
from __future__ import annotations

import argparse
import io
import json
from datetime import datetime

import numpy as np
import pyarrow.parquet as pq

from dms.config import settings
from dms.inference import compute_embedding, save_index_bytes, save_index_meta
from dms.storage import get_bytes


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a feature index from a DMS versioned manifest.")
    parser.add_argument("--container", default=settings.swift_training_container)
    parser.add_argument("--manifest-key", default=settings.inference_manifest_key)
    parser.add_argument("--index-key", default=settings.inference_index_key)
    parser.add_argument("--meta-key", default=settings.inference_index_meta_key)
    parser.add_argument("--max-items", type=int, default=None, help="Optional cap for demo-sized builds")
    args = parser.parse_args()

    settings.swift_training_container = args.container
    settings.inference_index_key = args.index_key
    settings.inference_index_meta_key = args.meta_key

    raw_manifest = get_bytes(args.container, args.manifest_key)
    table = pq.read_table(io.BytesIO(raw_manifest))
    rows = table.to_pylist()
    if args.max_items is not None:
        rows = rows[: args.max_items]

    embeddings: list[np.ndarray] = []
    object_keys: list[str] = []
    titles: list[str] = []
    ingredients: list[str] = []
    splits: list[str] = []

    for idx, row in enumerate(rows, start=1):
        object_key = str(row["object_key"])
        raw_image = get_bytes(args.container, object_key)
        embedding = compute_embedding(raw_image)

        embeddings.append(embedding)
        object_keys.append(object_key)
        titles.append(str(row.get("title") or ""))
        ingredients.append(str(row.get("ingredients") or ""))
        splits.append(str(row.get("split") or ""))
        print(f"[{idx}/{len(rows)}] indexed {object_key}")

    matrix = np.vstack(embeddings).astype(np.float32) if embeddings else np.zeros((0, 2048), dtype=np.float32)
    buf = io.BytesIO()
    np.savez_compressed(
        buf,
        embeddings=matrix,
        object_keys=np.array(object_keys, dtype=str),
        titles=np.array(titles, dtype=str),
        ingredients=np.array(ingredients, dtype=str),
        splits=np.array(splits, dtype=str),
        source_manifest_key=np.array([args.manifest_key], dtype=str),
    )
    save_index_bytes(buf.getvalue())

    meta = {
        "created_at": datetime.utcnow().isoformat() + "Z",
        "container": args.container,
        "source_manifest_key": args.manifest_key,
        "index_key": args.index_key,
        "items": len(object_keys),
        "embedding_dim": int(matrix.shape[1]) if matrix.size else 0,
        "model": settings.inference_model_name,
    }
    save_index_meta(meta)
    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
