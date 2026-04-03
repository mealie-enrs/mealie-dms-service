import hashlib
import json
from datetime import datetime

import swiftclient

from dms.config import settings


def _swift_conn():
    if not (settings.swift_auth_url and settings.swift_username and settings.swift_password):
        return None
    return swiftclient.Connection(
        authurl=settings.swift_auth_url,
        user=settings.swift_username,
        key=settings.swift_password,
        auth_version="3",
    )


def put_text(container: str, key: str, content: str, content_type: str = "text/plain") -> None:
    conn = _swift_conn()
    if conn is None:
        return
    conn.put_object(container, key, contents=content.encode("utf-8"), content_type=content_type)


def put_bytes(container: str, key: str, content: bytes, content_type: str = "application/octet-stream") -> None:
    conn = _swift_conn()
    if conn is None:
        return
    conn.put_object(container, key, contents=content, content_type=content_type)


def copy_object(container_from: str, key_from: str, container_to: str, key_to: str) -> None:
    conn = _swift_conn()
    if conn is None:
        return
    conn.copy_object(container_from, key_from, destination=f"{container_to}/{key_to}")


def make_object_key_from_bytes(raw_bytes: bytes, extension: str = "jpg") -> tuple[str, str]:
    digest = hashlib.sha256(raw_bytes).hexdigest()
    key = f"objects/sha256/{digest[0:2]}/{digest[2:4]}/{digest}.{extension}"
    return key, digest


def write_version_meta(version: str, dataset_name: str, object_count: int) -> str:
    meta_key = f"versions/{version}/meta.json"
    payload = {
        "dataset": dataset_name,
        "version": version,
        "object_count": object_count,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    put_text(settings.swift_training_container, meta_key, json.dumps(payload, indent=2), "application/json")
    return meta_key
