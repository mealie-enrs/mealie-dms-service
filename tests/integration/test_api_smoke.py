"""HTTP integration smoke tests: real Postgres, no object-store uploads or Celery tasks."""

from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest


BASE_URL = os.environ.get("DMS_API_URL", "http://127.0.0.1:8000")


@pytest.fixture(scope="module")
def client() -> httpx.Client:
    deadline = time.monotonic() + 60.0
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{BASE_URL}/healthz", timeout=2.0)
            if r.status_code == 200:
                break
        except Exception as exc:  # noqa: BLE001 — retry until API listens
            last_err = exc
        time.sleep(0.5)
    else:
        raise RuntimeError(f"API not reachable at {BASE_URL}") from last_err

    with httpx.Client(base_url=BASE_URL, timeout=30.0) as http:
        yield http


def test_healthz(client: httpx.Client) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_create_dataset_and_list_versions_empty(client: httpx.Client) -> None:
    name = f"ci-smoke-{uuid.uuid4().hex[:12]}"
    r = client.post("/datasets", json={"name": name, "description": "integration"})
    assert r.status_code == 200
    body = r.json()
    assert "dataset_id" in body
    dataset_id = body["dataset_id"]
    assert body["name"] == name

    r2 = client.get(f"/datasets/{dataset_id}/versions")
    assert r2.status_code == 200
    data = r2.json()
    assert data["dataset_id"] == dataset_id
    assert data["versions"] == []


def test_upload_init_persists_metadata_only(client: httpx.Client) -> None:
    """Registers an upload row + incoming key; does not call Swift or Celery."""
    r = client.post(
        "/uploads/init",
        json={"user_id": "ci-user", "filename": "sample.jpg"},
    )
    assert r.status_code == 200
    payload = r.json()
    assert payload["upload_id"] >= 1
    assert payload["incoming_key"].startswith("incoming/ci-user/")


def test_job_not_found(client: httpx.Client) -> None:
    r = client.get("/jobs/999999999")
    assert r.status_code == 404


def test_feedback_requires_existing_draft_snapshot(client: httpx.Client) -> None:
    r = client.post(
        "/feedback",
        json={
            "draft_id": str(uuid.uuid4()),
            "final_title": "Example",
            "final_ingredients": ["1 egg"],
            "final_steps": ["Cook it"],
            "action": "approved",
            "consent": True,
        },
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "draft not found"
