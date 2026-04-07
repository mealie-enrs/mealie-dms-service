import hashlib
import json
import mimetypes
from datetime import datetime
from pathlib import Path

import swiftclient
from keystoneauth1 import session as ks_session
from keystoneauth1.identity import v3 as v3_identity

from dms.config import settings

VERSION_PREFIX = "recipe1m_versions"
LARGE_OBJECT_THRESHOLD_BYTES = 100 * 1024 * 1024
LARGE_OBJECT_SEGMENT_BYTES = 64 * 1024 * 1024


def _swift_conn():
    if settings.swift_app_credential_id and settings.swift_app_credential_secret:
        auth = v3_identity.ApplicationCredential(
            auth_url=settings.swift_auth_url,
            application_credential_id=settings.swift_app_credential_id,
            application_credential_secret=settings.swift_app_credential_secret,
        )
        sess = ks_session.Session(auth=auth)
        return swiftclient.Connection(session=sess)

    if not (settings.swift_auth_url and settings.swift_username and settings.swift_password):
        return None
    os_options = {}
    if settings.swift_project_name:
        os_options["project_name"] = settings.swift_project_name
    if settings.swift_user_domain_name:
        os_options["user_domain_name"] = settings.swift_user_domain_name
    if settings.swift_project_domain_name:
        os_options["project_domain_name"] = settings.swift_project_domain_name
    return swiftclient.Connection(
        authurl=settings.swift_auth_url,
        user=settings.swift_username,
        key=settings.swift_password,
        auth_version="3",
        os_options=os_options,
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


def get_bytes(container: str, key: str) -> bytes:
    conn = require_swift()
    _, data = conn.get_object(container, key)
    return data


def require_swift() -> swiftclient.Connection:
    conn = _swift_conn()
    if conn is None:
        raise RuntimeError(
            "Swift is not configured. Set SWIFT_APP_CREDENTIAL_ID + SWIFT_APP_CREDENTIAL_SECRET, "
            "or SWIFT_AUTH_URL + SWIFT_USERNAME + SWIFT_PASSWORD."
        )
    return conn


def ensure_container(container: str) -> None:
    conn = require_swift()
    conn.put_container(container)


def put_file_path(
    container: str,
    key: str,
    path: str | Path,
    content_type: str | None = None,
) -> None:
    """Upload a local file without loading it all into memory."""
    conn = require_swift()
    p = Path(path)
    size = p.stat().st_size
    ct = content_type
    if ct is None:
        guessed, _ = mimetypes.guess_type(p.name)
        ct = guessed or "application/octet-stream"

    if size > LARGE_OBJECT_THRESHOLD_BYTES:
        _put_large_object_file_path(
            conn=conn,
            container=container,
            key=key,
            path=p,
            size=size,
            content_type=ct,
        )
        return

    with open(p, "rb") as fh:
        conn.put_object(container, key, contents=fh, content_length=size, content_type=ct)


def _put_large_object_file_path(
    *,
    conn: swiftclient.Connection,
    container: str,
    key: str,
    path: Path,
    size: int,
    content_type: str,
) -> None:
    """Upload large files as segmented static large objects to avoid gateway timeouts."""
    segment_prefix = f"_segments/{key}"
    manifest_entries: list[dict[str, object]] = []

    with open(path, "rb") as fh:
        segment_index = 0
        while True:
            chunk = fh.read(LARGE_OBJECT_SEGMENT_BYTES)
            if not chunk:
                break

            segment_key = f"{segment_prefix}/{segment_index:08d}"
            etag = conn.put_object(
                container,
                segment_key,
                contents=chunk,
                content_length=len(chunk),
                content_type="application/octet-stream",
            )
            manifest_entries.append(
                {
                    "path": f"/{container}/{segment_key}",
                    "etag": etag,
                    "size_bytes": len(chunk),
                }
            )
            segment_index += 1

    conn.put_object(
        container,
        key,
        contents=json.dumps(manifest_entries).encode("utf-8"),
        content_length=len(json.dumps(manifest_entries).encode("utf-8")),
        content_type=content_type,
        query_string="multipart-manifest=put",
        headers={"X-Static-Large-Object": "true"},
    )


def make_object_key_from_bytes(raw_bytes: bytes, extension: str = "jpg") -> tuple[str, str]:
    digest = hashlib.sha256(raw_bytes).hexdigest()
    key = f"objects/sha256/{digest[0:2]}/{digest[2:4]}/{digest}.{extension}"
    return key, digest


def write_version_meta(version: str, dataset_name: str, object_count: int) -> str:
    meta_key = f"{VERSION_PREFIX}/{version}/meta.json"
    payload = {
        "dataset": dataset_name,
        "version": version,
        "object_count": object_count,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    put_text(settings.swift_training_container, meta_key, json.dumps(payload, indent=2), "application/json")
    return meta_key
