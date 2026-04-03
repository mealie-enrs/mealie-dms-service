#!/usr/bin/env python3
"""
Download a tiny Recipe1M sample and stage it in object storage raw prefix.

Example:
python scripts/ingest_recipe1m_sample.py \
  --manifest-source https://example.com/recipe1m-sample.jsonl \
  --sample-size 1000 \
  --container proj26-obj-store \
  --raw-prefix raw/recipe1m/v1
"""

import argparse
import json
from datetime import datetime

from dms.recipe1m import iter_sample_image_urls, upload_raw_sample
from dms.storage import put_text


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage Recipe1M sample data to object store raw prefix.")
    parser.add_argument("--manifest-source", required=True, help="JSONL file path or URL with image entries")
    parser.add_argument("--sample-size", type=int, default=1000, help="Number of images to stage")
    parser.add_argument("--container", default="proj26-obj-store", help="Target object store container")
    parser.add_argument("--raw-prefix", default="raw/recipe1m/v1", help="Raw prefix path in container")
    args = parser.parse_args()

    staged = []
    failures = 0
    for idx, image_url in enumerate(iter_sample_image_urls(args.manifest_source, args.sample_size), start=1):
        try:
            raw_key, _ = upload_raw_sample(
                image_url=image_url,
                container=args.container,
                raw_prefix=args.raw_prefix,
                item_idx=idx,
            )
            staged.append({"idx": idx, "image_url": image_url, "raw_key": raw_key})
        except Exception as exc:  # broad for robust batch staging
            failures += 1
            print(f"[warn] failed to stage {image_url}: {exc}")

    summary = {
        "manifest_source": args.manifest_source,
        "sample_size_requested": args.sample_size,
        "staged_count": len(staged),
        "failed_count": failures,
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "items": staged,
    }
    summary_key = f"{args.raw_prefix.rstrip('/')}/staging_summary.json"
    put_text(args.container, summary_key, json.dumps(summary, indent=2), "application/json")

    print(f"staged_count={len(staged)} failed_count={failures}")
    print(f"summary_key={args.container}/{summary_key}")


if __name__ == "__main__":
    main()
