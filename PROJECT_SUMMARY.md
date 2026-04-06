# DMS (Data Management Service) — Project Summary

## What it is

A single Python application (`dms/`) deployed as three runtime roles on Kubernetes:

- **dms-api** — FastAPI HTTP control plane (uploads, datasets, versions, jobs)
- **dms-worker** — Celery async worker (validation, promotion, manifest build, ingest)
- **dms-scheduler** — Celery beat (periodic cleanup, integrity checks)

Backend services: **PostgreSQL** (metadata), **Redis** (task queue).
Durable storage: **Chameleon Object Store (Swift)**.

## Where it runs

- **1 Chameleon baremetal VM** (`proj26-dms-k3s`, floating IP `192.5.87.69`)
- **k3s** single-node Kubernetes cluster
- **Namespace:** `dms`
- **API exposed at:** `http://192.5.87.69:30080`

## Infrastructure

- **Terraform** (`infra/terraform/`) provisions security group, floating IP, and VM via OpenStack on Chameleon.
- **Kubernetes manifests** (`infra/k8s/`) define all workloads: api, worker, scheduler, postgres, redis.
- **GitHub Actions** (`.github/workflows/deploy-main.yml`) builds `ghcr.io/nidhish1/dms-app`, pushes to GHCR, and rolls out deployments on merge to `main`.

## Object store layout

Container: **`proj26-obj-store`** (also designed for `proj26-user-uploads` and `proj26-training-data`).

| Prefix | Contents |
|--------|----------|
| `incoming/{user_id}/{uuid}-{filename}` | Raw user uploads (untrusted) |
| `objects/sha256/{ab}/{cd}/{sha256}.jpg` | Immutable curated blobs (content-addressed) |
| `raw/{dataset}/{timestamp}/...` | Staging copies from ingest jobs |
| `recipe1m_versions/{version}/manifest.parquet` | Frozen dataset snapshot (one row per training example) |
| `recipe1m_versions/{version}/meta.json` | Version metadata (counts, timestamps, dataset name) |

## Current data

- **Food-101 sample** (500 images) ingested from laptop into `proj26-obj-store`
- Manifest: `recipe1m_versions/food101-sample-v1/manifest.parquet` (500 rows)
- Meta: `recipe1m_versions/food101-sample-v1/meta.json`
- Raw: `raw/food101/20260403T101210Z/000000_xxx.jpg` … `000499_xxx.jpg`
- Curated: `objects/sha256/xx/yy/{sha256}.jpg`

## API endpoints

| Method | Path | What it does |
|--------|------|--------------|
| GET | `/healthz` | Liveness check |
| POST | `/datasets` | Create dataset record |
| GET | `/datasets/{id}/versions` | List published versions (manifest keys, meta keys) |
| POST | `/uploads/init` | Register incoming upload, return object key |
| POST | `/uploads/{id}/approval` | Queue async approve/reject job |
| POST | `/datasets/{id}/publish` | Queue async version publish from object IDs |
| POST | `/datasets/{id}/ingest/recipe1m` | Queue async sample ingest from manifest URL |
| GET | `/jobs/{id}` | Poll async job status/result |

## How another service (e.g. inference/training) uses DMS

1. **Discover versions:** `GET /datasets/{id}/versions` → get `manifest_key` and `meta_key`.
2. **Download manifest from Swift:** read `manifest.parquet` using Swift credentials (not through the API).
3. **Iterate rows:** each row has `object_key`, `label`, `split`, `checksum`, `width`, `height`.
4. **Download images from Swift:** `GET` each `object_key` from the same container.
5. **Pin to a version string** for reproducibility; new training run = pick a new version.

DMS is the **control plane** (catalog + versioning). Swift is the **data plane** (bulk bytes). The inference/training service never streams large payloads through the DMS API.

## Versioning model

- Objects are stored **once** (content-addressed under `objects/sha256/...`).
- A **version** is a **manifest** (Parquet file listing which objects belong), not a copy of every image.
- Creating `v2` from `v1`: load v1 manifest, add/remove/relabel rows, write new manifest. Unchanged images are reused.

## Key credentials / secrets

| Secret | Location | Purpose |
|--------|----------|---------|
| OpenStack app credential | `secrets/app-cred-nidhish-mac-openrc.sh` | Swift + OpenStack CLI auth |
| GitHub PAT | `secrets/github.pat` | GHCR image push |
| Kubeconfig | `~/.kube/dms-k3s.yaml` | kubectl access to k3s |
| SSH key | `~/.ssh/id_rsa_chameleon` | VM access (`cc@192.5.87.69`) |

## Known limitations (current state)

- **Cluster DNS/egress broken:** pods cannot resolve external hostnames (huggingface.co, pypi.org, etc.). Workaround: ingest from laptop directly to Swift.
- **Pod-side Swift auth incomplete:** worker pods fail with `No project name or project id specified` because `dms-secrets` uses simple user/key auth instead of application-credential style. Fix: update `dms/storage.py` to use `os_options` with application credentials, or inject correct env vars.
- **CD pipeline:** GHCR push works with PAT; kubeconfig deploy step needs valid `KUBECONFIG_B64` secret in GitHub.

## Repo structure

```
mealie/
├── dms/                  # Application code (api, tasks, models, config, storage, celery)
├── db/                   # Reference SQL schema
├── scripts/              # Entrypoint (role selector) + CLI helpers
├── infra/
│   ├── terraform/        # VM / network / cloud-init provisioning
│   └── k8s/              # Kubernetes manifests (deployments, services, jobs)
├── quality/              # Optional Soda data quality checks
├── secrets/              # Local-only credentials (gitignored)
├── .github/workflows/    # CI/CD pipeline
├── Dockerfile            # Single image for all roles
├── docker-compose.yml    # Local dev stack
├── requirements.txt      # Python deps
├── API_SPEC.md           # Live-tested endpoint reference
├── readme.md             # Project overview
└── deploy_readme.md      # Deployment log (commands, errors, fixes)
```
