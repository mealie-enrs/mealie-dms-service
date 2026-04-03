import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from dms.storage import make_object_key_from_bytes, put_bytes


def _iter_jsonl_lines(path_or_url: str) -> Iterable[dict[str, Any]]:
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        req = Request(path_or_url, headers={"User-Agent": "dms-recipe1m-ingest/1.0"})
        with urlopen(req, timeout=60) as resp:  # nosec B310
            for raw in resp:
                line = raw.decode("utf-8").strip()
                if line:
                    yield json.loads(line)
        return

    with open(path_or_url, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def _extract_image_url(record: dict[str, Any]) -> str | None:
    if isinstance(record.get("image_url"), str):
        return record["image_url"]

    images = record.get("images")
    if isinstance(images, list) and images:
        first = images[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            for key in ("url", "image_url"):
                if isinstance(first.get(key), str):
                    return first[key]
    return None


def iter_sample_image_urls(manifest_source: str, sample_size: int) -> Iterable[str]:
    count = 0
    for record in _iter_jsonl_lines(manifest_source):
        image_url = _extract_image_url(record)
        if not image_url:
            continue
        yield image_url
        count += 1
        if count >= sample_size:
            break


def download_image_bytes(image_url: str) -> bytes:
    req = Request(image_url, headers={"User-Agent": "dms-recipe1m-ingest/1.0"})
    with urlopen(req, timeout=45) as resp:  # nosec B310
        return resp.read()


def infer_extension(image_url: str, default: str = "jpg") -> str:
    parsed = urlparse(image_url)
    basename = os.path.basename(parsed.path)
    if "." not in basename:
        return default
    ext = basename.split(".")[-1].lower()
    if len(ext) > 8:
        return default
    return ext


def upload_raw_sample(
    *,
    image_url: str,
    container: str,
    raw_prefix: str,
    item_idx: int,
) -> tuple[str, bytes]:
    raw_bytes = download_image_bytes(image_url)
    ext = infer_extension(image_url)
    digest = hashlib.sha256(raw_bytes).hexdigest()
    raw_key = f"{raw_prefix.rstrip('/')}/{item_idx:06d}_{digest[:12]}.{ext}"
    put_bytes(container, raw_key, raw_bytes, "application/octet-stream")
    return raw_key, raw_bytes


def upload_curated_object(*, raw_bytes: bytes, container: str, image_url: str) -> tuple[str, str]:
    ext = infer_extension(image_url)
    object_key, checksum = make_object_key_from_bytes(raw_bytes=raw_bytes, extension=ext)
    put_bytes(container, object_key, raw_bytes, "application/octet-stream")
    return object_key, checksum


def write_temp_json(payload: dict[str, Any]) -> str:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
        json.dump(payload, tmp, indent=2)
        return str(Path(tmp.name))
