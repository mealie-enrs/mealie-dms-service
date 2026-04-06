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
    with open(p, "rb") as fh:
        conn.put_object(container, key, contents=fh, content_length=size, content_type=ct)


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
