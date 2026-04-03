# DMS API spec sheet (live-tested)

**Base URL**

```bash
BASE_URL="http://192.5.87.69:30080"
```

Last live smoke run: exercise all routes below against this base URL; job outcomes polled after ~2–6s.

---

## Summary table

| Method | Path | HTTP (sync) | Notes |
|--------|------|-------------|--------|
| GET | `/healthz` | 200 | Works |
| POST | `/datasets` | 200 | Works |
| GET | `/datasets/{dataset_id}/versions` | 200 | Works |
| POST | `/uploads/init` | 200 | Works |
| POST | `/uploads/{upload_id}/approval` | 200 | Queues job; **worker fails** Swift auth: `No project name or project id specified.` |
| POST | `/datasets/{dataset_id}/publish` | 200 | Queues job; **worker fails** if `include_object_ids` empty (`no objects selected`); needs real object IDs + Swift fix for success |
| POST | `/datasets/{dataset_id}/ingest/recipe1m` | 200 | Queues job; **worker fails** on external URL: `Temporary failure in name resolution` (cluster DNS) |
| GET | `/jobs/{job_id}` | 200 | Works |

---

## Live run snapshot (example IDs from last test)

These IDs will differ on your next run; the failure modes should match until infra is fixed.

| Job kind | Example `job_id` | Final `status` | `message` |
|----------|------------------|----------------|-----------|
| `upload_approval` | 8 | `failed` | `No project name or project id specified.` |
| `publish_version` | 9 | `failed` | `no objects selected` (empty `include_object_ids`) |
| `recipe1m_ingest` | 10 | `failed` | `Temporary failure in name resolution` |

---

## 1. `GET /healthz`

**Try**

```bash
curl -sS "$BASE_URL/healthz"
```

**Expected:** `{"status":"ok"}`

---

## 2. `POST /datasets`

**Body**

```json
{
  "name": "my-dataset",
  "description": "optional"
}
```

**Try**

```bash
curl -sS -X POST "$BASE_URL/datasets" \
  -H "Content-Type: application/json" \
  -d '{"name":"my-dataset","description":"demo"}'
```

**Expected:** `{"dataset_id": <int>, "name": "my-dataset"}`

---

## 3. `GET /datasets/{dataset_id}/versions`

**Try**

```bash
curl -sS "$BASE_URL/datasets/4/versions"
```

**Expected:** `{"dataset_id": 4, "versions": [...]}`

---

## 4. `POST /uploads/init`

**Body**

```json
{
  "user_id": "u1",
  "filename": "photo.jpg"
}
```

**Try**

```bash
curl -sS -X POST "$BASE_URL/uploads/init" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"u1","filename":"photo.jpg"}'
```

**Expected:** `{"upload_id": <int>, "incoming_key": "incoming/..." }`

---

## 5. `POST /uploads/{upload_id}/approval`

**Body**

```json
{
  "approve": true
}
```

**Try**

```bash
curl -sS -X POST "$BASE_URL/uploads/3/approval" \
  -H "Content-Type: application/json" \
  -d '{"approve":true}'
```

**Expected (sync):** `{"job_id": <int>, "task_id": "<uuid>", "status": "queued"}`

**Poll**

```bash
curl -sS "$BASE_URL/jobs/<job_id>"
```

**Known cluster issue:** job may end `failed` with Swift `No project name or project id specified.` until `dms-secrets` / Swift client config matches application-credential style auth.

---

## 6. `POST /datasets/{dataset_id}/publish`

**Body** (`include_object_ids` is required)

```json
{
  "version": "v1",
  "include_object_ids": [1, 2, 3]
}
```

**Try (will fail worker if list empty)**

```bash
curl -sS -X POST "$BASE_URL/datasets/4/publish" \
  -H "Content-Type: application/json" \
  -d '{"version":"v1","include_object_ids":[]}'
```

**Try (valid shape — still needs real DB object IDs + Swift)**

```bash
curl -sS -X POST "$BASE_URL/datasets/4/publish" \
  -H "Content-Type: application/json" \
  -d '{"version":"v1","include_object_ids":[1]}'
```

**Poll:** `GET /jobs/{job_id}`

---

## 7. `POST /datasets/{dataset_id}/ingest/recipe1m`

**Body**

```json
{
  "manifest_source": "https://host/path.jsonl",
  "sample_size": 1000,
  "raw_prefix": "raw/recipe1m",
  "target_container": "proj26-obj-store",
  "auto_publish_version": "v1"
}
```

**Try**

```bash
curl -sS -X POST "$BASE_URL/datasets/4/ingest/recipe1m" \
  -H "Content-Type: application/json" \
  -d '{
    "manifest_source":"https://example.com/sample.jsonl",
    "sample_size":10,
    "raw_prefix":"raw/recipe1m",
    "target_container":"proj26-obj-store",
    "auto_publish_version":null
  }'
```

**Known cluster issue:** worker often fails with DNS resolution for external hosts. Laptop-side ingest to Swift remains a working fallback.

---

## 8. `GET /jobs/{job_id}`

**Try**

```bash
curl -sS "$BASE_URL/jobs/10"
```

**Expected:** JSON with `id`, `kind`, `status`, `message`, `payload`, timestamps.

---

## Quick full sequence

```bash
BASE_URL="http://192.5.87.69:30080"

curl -sS "$BASE_URL/healthz"

curl -sS -X POST "$BASE_URL/datasets" \
  -H "Content-Type: application/json" \
  -d '{"name":"spec-demo","description":"test"}'

curl -sS -X POST "$BASE_URL/uploads/init" \
  -H "Content-Type: application/json" \
  -d '{"user_id":"demo","filename":"f.jpg"}'
```

Then substitute returned `dataset_id` / `upload_id` / `job_id` in the calls above.
