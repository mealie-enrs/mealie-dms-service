# DMS (Single App, Multi-Role Runtime)

<!-- CI: push to main triggers deploy when matched paths change -->

This repository now defines a **single DMS application** deployed as three runtime roles:

- `dms-api`: HTTP control plane (uploads, datasets, versions, approvals, export requests)
- `dms-worker`: asynchronous heavy jobs (validation, dedupe, manifests, shards/exports prep)
- `dms-scheduler`: periodic tasks (`celery beat`) for maintenance jobs

Infra dependencies:

- `postgres`: metadata source of truth
- `redis`: async broker/backend

External storage:

- Chameleon Object Store (Swift)

## Runtime Topology

- 1 VM
- 1 attached persistent volume (for Postgres data)
- 1 Docker Compose stack
- 2 external object-store containers:
  - `proj26-user-uploads`
  - `proj26-training-data`

## Quick Start

1. Copy environment file:

   ```bash
   cp .env.example .env
   ```

2. Edit `.env` with your Swift credentials.

3. Start stack:

   ```bash
   docker compose up --build
   ```

4. Verify:
   - API health: `http://localhost:8000/healthz`
   - Containers: `docker compose ps`

## API Surface (Initial)

- `POST /uploads/init`: create incoming upload record and key
- `POST /uploads/{upload_id}/approval`: approve/reject upload asynchronously
- `POST /datasets`: create dataset
- `GET /datasets/{dataset_id}/versions`: list published versions
- `POST /datasets/{dataset_id}/publish`: publish version asynchronously
- `POST /datasets/{dataset_id}/ingest/recipe1m`: ingest tiny Recipe1M sample asynchronously
- `POST /kaggle/download`: download a Kaggle dataset on the worker (optional Swift upload)
- `GET /jobs/{job_id}`: track background job

## Data Ownership

### Postgres tables

- `uploads`
- `objects`
- `datasets`
- `dataset_versions`
- `dataset_items`
- `jobs`

Schema SQL is in `db/schema.sql`.

### Redis

Queue/broker and task state backend for Celery.

### Object Store layout

`proj26-user-uploads`:

```text
incoming/{user_id}/{upload_id}.jpg
quarantine/{upload_id}.jpg
processed/{upload_id}.jpg
rejected/{upload_id}.jpg
```

`proj26-training-data`:

```text
objects/sha256/ab/cd/<hash>.jpg
recipe1m_versions/v1/manifest.parquet
recipe1m_versions/v1/meta.json
recipe1m_versions/v2/manifest.parquet
recipe1m_versions/v2/meta.json
shards/v1/train-000000.tar
exports/v1/...
recipe1m/...                    # Kaggle / Recipe1M data files
```

## Versioning Model

- Raw curated objects are stored once (immutable object keys).
- Dataset versions are **manifests**, not full data copies.
- `v2` is produced by applying metadata membership changes to `v1`.
- Bulk training reads should fetch from object storage, not stream through API.

## Kaggle dataset download (server-side)

The worker can download any Kaggle dataset using `kagglehub` and optionally upload files to Swift under `recipe1m/`.

**Prerequisites:** set `KAGGLE_USERNAME` and `KAGGLE_KEY` on the worker (K8s Secret or Compose env).

```bash
curl -X POST "http://localhost:8000/kaggle/download" \
  -H "Content-Type: application/json" \
  -d '{
    "dataset_slug": "pes12017000148/food-ingredients-and-recipe-dataset-with-images",
    "upload_to_swift": true,
    "swift_subpath": "kaggle/food-images"
  }'
# Track: GET /jobs/{job_id}
```

Files are uploaded to `recipe1m/kaggle/food-images/...` inside `SWIFT_TRAINING_CONTAINER`.

## Recipe1M tiny sample ingest

Staging script (downloads + writes raw sample data to object store):

```bash
python scripts/ingest_recipe1m_sample.py \
  --manifest-source "<jsonl-file-or-url>" \
  --sample-size 1000 \
  --container proj26-obj-store \
  --raw-prefix raw/recipe1m/v1
```

Asynchronous DMS ingestion API flow:

1. Create dataset (`POST /datasets`)
2. Trigger ingest:

```bash
curl -X POST "http://localhost:8000/datasets/<dataset_id>/ingest/recipe1m" \
  -H "Content-Type: application/json" \
  -d '{
    "manifest_source": "<jsonl-file-or-url>",
    "sample_size": 1000,
    "raw_prefix": "raw/recipe1m",
    "target_container": "proj26-obj-store",
    "auto_publish_version": "v1"
  }'
```

3. Track job with `GET /jobs/{job_id}`

Cloud-side note: ingestion runs in the worker pod and supports both:
- JSONL manifest URLs/files (one JSON object per line)
- Large JSON array metadata sources (for example `layer2+.json`) via streaming parser

### Optional bonus quality gates (Soda)

The publish path enforces built-in checks (non-empty manifest, no duplicate checksums),
and can run Soda scan as a hard gate when enabled:

```bash
export ENABLE_SODA_CHECKS=true
```

Place Soda configuration/checks at:

- `quality/soda/configuration.yml`
- `quality/soda/checks.yml`

## OpenStack/Chameleon CLI setup

If you need local CLI access:

```bash
mv ~/Downloads/clouds.yaml ~/.config/openstack/clouds.yaml
source /Users/mudrex/Desktop/mealie/secrets/app-cred-nidhish-mac-openrc.sh
openstack container list
```

If you are using both conda (`ml`) and a Python venv (`oscli`), environment commands are:

```bash
# deactivate only ml
conda deactivate

# deactivate oscli venv
deactivate
```

Recommended OpenStack CLI setup for lease commands:

```bash
python3 -m venv ~/.venvs/oscli
source ~/.venvs/oscli/bin/activate
pip install -U pip
pip install python-openstackclient python-blazarclient
source /Users/mudrex/Desktop/mealie/secrets/app-cred-nidhish-mac-openrc.sh
openstack token issue
openstack reservation lease list
openstack reservation lease show <LEASE_ID>
```

## Terraform + Kubernetes deployment

If you want Chameleon deployment with Terraform and Kubernetes instead of Docker Compose, use `infra/`:

- Terraform stack: `infra/terraform`
- Kubernetes manifests: `infra/k8s`
- Full runbook: `infra/README.md`
