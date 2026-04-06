#!/usr/bin/env python3
"""Generate deterministic upload traffic against the DMS upload flow.

The script:
1. Calls POST /uploads/init
2. Uploads a deterministically augmented image to Swift using the returned incoming_key
3. Calls POST /uploads/{id}/approval

It runs for a fixed number of iterations and users, so it is reproducible and
safe to use in demos.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib import error, request

from PIL import Image, ImageEnhance, ImageOps

from dms.config import settings
from dms.storage import require_swift

SOURCE_PREFIX = "recipe1m/kaggle-food-images/Food Images/Food Images/"


@dataclass
class GeneratedUpload:
    iteration: int
    user_id: str
    source_key: str
    incoming_key: str
    upload_id: int
    approval_job_id: int
    filename: str


def _http_json(method: str, url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method=method,
    )
    with request.urlopen(req, timeout=60) as resp:  # nosec B310
        return json.loads(resp.read().decode("utf-8"))


def _list_source_keys(container: str, prefix: str, limit: int) -> list[str]:
    conn = require_swift()
    keys: list[str] = []
    marker = ""
    while len(keys) < limit:
        _, objects = conn.get_container(container, prefix=prefix, limit=10000, marker=marker)
        if not objects:
            break
        for obj in objects:
            name = obj["name"]
            if name.lower().endswith((".jpg", ".jpeg", ".png")):
                keys.append(name)
                if len(keys) >= limit:
                    break
        marker = objects[-1]["name"]
    return sorted(keys)


def _download_image(container: str, key: str) -> bytes:
    conn = require_swift()
    _, data = conn.get_object(container, key)
    return data


def _augment_image(raw: bytes, iteration: int) -> bytes:
    image = Image.open(io.BytesIO(raw)).convert("RGB")

    # Deterministic augmentation schedule based only on iteration.
    rotations = [0, 90, 180, 270]
    rotation = rotations[iteration % len(rotations)]
    image = image.rotate(rotation, expand=True)

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
    image.save(out, format="JPEG", quality=90)
    return out.getvalue()


def _swift_put(container: str, key: str, content: bytes) -> None:
    conn = require_swift()
    conn.put_object(container, key, contents=content, content_type="image/jpeg")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate deterministic upload traffic for DMS.")
    parser.add_argument("--base-url", default="http://localhost:8000", help="DMS API base URL")
    parser.add_argument("--iterations", type=int, default=20, help="Number of uploads to generate")
    parser.add_argument("--users", type=int, default=4, help="Number of synthetic users to cycle through")
    parser.add_argument("--interval-seconds", type=float, default=1.0, help="Sleep between iterations")
    parser.add_argument(
        "--source-container",
        default=settings.swift_training_container,
        help="Swift container used as the source of seed images",
    )
    parser.add_argument(
        "--source-prefix",
        default=SOURCE_PREFIX,
        help="Swift prefix containing source images to augment",
    )
    parser.add_argument(
        "--summary-file",
        default="generated_upload_traffic_summary.json",
        help="Local JSON summary output",
    )
    args = parser.parse_args()

    source_keys = _list_source_keys(args.source_container, args.source_prefix, limit=max(args.iterations, 32))
    if not source_keys:
        print("No source images found for upload generation.", file=sys.stderr)
        sys.exit(1)

    results: list[GeneratedUpload] = []
    user_ids = [f"demo-user-{i:02d}" for i in range(1, args.users + 1)]

    for iteration in range(args.iterations):
        user_id = user_ids[iteration % len(user_ids)]
        source_key = source_keys[iteration % len(source_keys)]
        source_raw = _download_image(args.source_container, source_key)
        augmented = _augment_image(source_raw, iteration)

        filename = f"synthetic-{iteration:04d}.jpg"
        init_payload = {"user_id": user_id, "filename": filename}
        init_resp = _http_json("POST", f"{args.base_url.rstrip('/')}/uploads/init", init_payload)

        incoming_key = init_resp["incoming_key"]
        upload_id = int(init_resp["upload_id"])
        _swift_put(settings.swift_user_uploads_container, incoming_key, augmented)

        approval_resp = _http_json(
            "POST",
            f"{args.base_url.rstrip('/')}/uploads/{upload_id}/approval",
            {"approve": True},
        )

        result = GeneratedUpload(
            iteration=iteration,
            user_id=user_id,
            source_key=source_key,
            incoming_key=incoming_key,
            upload_id=upload_id,
            approval_job_id=int(approval_resp["job_id"]),
            filename=filename,
        )
        results.append(result)
        print(
            f"[{iteration + 1}/{args.iterations}] user={user_id} upload_id={upload_id} "
            f"job_id={approval_resp['job_id']} source={Path(source_key).name}"
        )

        if iteration != args.iterations - 1 and args.interval_seconds > 0:
            time.sleep(args.interval_seconds)

    summary = {
        "base_url": args.base_url,
        "iterations": args.iterations,
        "users": args.users,
        "interval_seconds": args.interval_seconds,
        "source_container": args.source_container,
        "source_prefix": args.source_prefix,
        "results": [asdict(item) for item in results],
    }
    Path(args.summary_file).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"summary_file={args.summary_file}")


if __name__ == "__main__":
    main()
