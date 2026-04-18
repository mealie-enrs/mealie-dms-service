# DMS System Flow

## The big picture

Two servers available. Everything below runs on the **CPU node** (`192.5.87.45`).

```
User / Client
     │
     ▼
┌─────────────┐     ┌─────────────┐     ┌──────────────┐
│   dms-api   │────▶│  dms-worker │────▶│  dms-sched   │
│  (FastAPI)  │     │  (Celery)   │     │  (Celery Beat│
└─────────────┘     └─────────────┘     └──────────────┘
       │                   │
       ▼                   ▼
┌─────────────┐     ┌─────────────┐
│   Postgres  │     │    Redis    │
│  (metadata) │     │  (job queue)│
└─────────────┘     └─────────────┘
       │                   │
       ▼                   ▼
┌─────────────┐     ┌─────────────┐
│   Qdrant    │     │   Prefect   │
│  (vectors)  │     │  (pipeline) │
└─────────────┘     └─────────────┘
       │
       ▼
┌─────────────────────────┐
│  Chameleon Swift        │
│  (images + manifests)   │
└─────────────────────────┘
```

---

## Three separate workflows

### 1. Online inference (real-time, per request)

This is the path a user hits when they upload a food image and want similar recipes back.

```
User uploads image
        │
        ▼
POST /inference/features
        │
        ▼
  ResNet50 (in-process)
  → 2048-D embedding
        │
        ▼
  Qdrant cosine search
  → top-K recipe matches
  (object_key, title, ingredients, score)
        │
        ▼
  JSON response to user
```

**Before:** The API loaded a giant NPZ file from Swift on startup. If the file didn't exist yet → crash. If it existed it was loaded entirely into RAM.

**Now:** Qdrant handles storage and search. The API just sends a vector query. Startup no longer crashes if the index is empty — it just returns zero results until you build the index.

---

### 2. Data pipeline (batch, triggered manually or on a schedule)

This runs when you want to compile a new versioned dataset from your Kaggle data in Swift.

```
POST /pipelines/training  (or: python -m pipelines.training_pipeline)
        │
        ▼
  Celery task → run_training_pipeline()
        │
        ▼
  Prefect @flow kicks off these @tasks in order:

  1. step_download_kaggle    — download from Kaggle → upload to Swift
                               (skipped if skip_download=True)

  2. step_compile            — read CSV from Swift
                               match image names → images actually in Swift
                               deduplicate, size-filter (<1KB skip)
                               deterministic 70/15/15 split by recipe title hash

  3. step_soda_check         — quality gate:
                               • zero records? → fail
                               • missing object_keys? → fail
                               • >10% duplicate titles? → fail
                               • train/val/test all present? → fail

  4. step_write_manifest     — if dataset < 5 GB:
                                 → Albumentations augments every train image ×3
                                   (flip, brightness, hue, rotation, blur, etc.)
                                   → uploads augmented images back to Swift
                               write Parquet manifest + meta.json to Swift

  5. step_register_version   — insert DatasetVersion row into Postgres

  6. step_build_qdrant_index — queue another Celery job that:
                                 → reads the manifest
                                 → downloads each image from Swift
                                 → runs ResNet50 on it
                                 → upserts (vector + metadata) into Qdrant
```

At the end of this you have:
- A versioned Parquet manifest in Swift (`recipe1m_versions/v1/manifest.parquet`)
- A meta.json with stats (`total_records`, `augmented_records`, split counts)
- All recipe embeddings living in Qdrant → inference works immediately

---

### 3. Data transforms (on demand, dbt)

This is separate from the pipeline. It runs SQL models against your Postgres DB to build clean views for Metabase dashboards.

```
docker compose --profile transforms run dbt
        │
        ▼
  staging models (views):
    stg_dataset_versions  — cleans versions table, adds version_num int
    stg_objects           — cleans objects, adds file_ext, sha_bucket

  mart models (tables):
    mart_dataset_summary  — one row per version: train/val/test counts
    mart_upload_funnel    — uploads grouped by status + day
        │
        ▼
  Written to dbt_dms schema in Postgres
  → visible in Metabase as clean tables
```

---

### 4. Data quality checks (on demand, Soda)

```
docker compose --profile quality run dms-soda
        │
        ▼
  Connects to Postgres, runs checks:
    objects          → row_count > 0, no null object_keys, no duplicate checksums
    dataset_versions → row_count > 0, no null manifest_keys
    uploads          → status in [incoming, approved, rejected, quarantined]
    jobs             → status in [queued, running, succeeded, failed]
        │
        ▼
  Pass → exit 0
  Fail → exit 1 + error report
```

---

## DVC — reproducibility layer

DVC ties it all together so anyone can reproduce the exact dataset that produced a given model.

```
dvc run compile     → runs step_compile with params from params.yaml
dvc run quality     → runs soda checks
dvc run transform   → runs dbt

params.yaml controls:
  version: v1
  enable_augmentation: true
  augment_factor: 3
  split_ratios: 70/15/15
```

If you change `augment_factor: 5` and rerun — DVC knows only the compile + downstream stages need to re-execute. Swift acts as the DVC remote so dataset files are versioned alongside code.

---

## What Prefect UI shows you

Go to `http://192.5.87.45:4200` once deployed. You will see:
- Every pipeline run with its status (success / failed)
- Which task failed and the exact error
- Timeline: how long each step took
- Flow runs history — so you can compare v1 vs v2 dataset builds

---

## Summary table

| Tool | Role | Replaces |
|---|---|---|
| **Qdrant** | Stores recipe embeddings, serves cosine search | NPZ file loaded into RAM |
| **Prefect** | Orchestrates the batch pipeline, tracks runs | Raw Celery task with no visibility |
| **Albumentations** | Expands train set ×3 without new data | Nothing (new capability) |
| **dbt** | Transforms Postgres tables into clean marts for Metabase | Raw SQL queries |
| **Soda** | Data quality gate before publishing a version | 3-line ad-hoc check in tasks.py |
| **DVC** | Versions datasets + pipeline params in Swift | No versioning |

---

## Service URLs (CPU node — `192.5.87.45`)

| Service | URL | Credentials |
|---|---|---|
| DMS API | http://192.5.87.45:8000 | — |
| API Docs | http://192.5.87.45:8000/docs | — |
| Qdrant UI | http://192.5.87.45:6333/dashboard | — |
| Prefect UI | http://192.5.87.45:4200 | — |
| Grafana | http://192.5.87.45:3000 | admin / admin |
| Prometheus | http://192.5.87.45:9090 | — |
| Metabase | http://192.5.87.45:3001 | set on first login |

---

## Useful commands

```bash
# Check all running services
ssh -i ~/.ssh/id_rsa_chameleon cc@192.5.87.45 "cd mealie-dms-service && docker compose ps"

# Trigger the full batch pipeline (compile + augment + index build)
curl -X POST "http://192.5.87.45:8000/pipelines/training?version=v1&dataset_id=1&skip_download=true"

# Check Qdrant index status (how many recipes are indexed)
curl http://192.5.87.45:8000/inference/index

# Build Qdrant index from an existing manifest
curl -X POST http://192.5.87.45:8000/inference/index/build

# Run inference on an image
curl -X POST http://192.5.87.45:8000/inference/features \
  -F "file=@/path/to/food.jpg" \
  -F "top_k=5"

# Run dbt transforms
ssh -i ~/.ssh/id_rsa_chameleon cc@192.5.87.45 \
  "cd mealie-dms-service && docker compose --profile transforms run dbt"

# Run Soda quality checks
ssh -i ~/.ssh/id_rsa_chameleon cc@192.5.87.45 \
  "cd mealie-dms-service && docker compose --profile quality run dms-soda"

# Watch worker logs (pipeline progress)
ssh -i ~/.ssh/id_rsa_chameleon cc@192.5.87.45 \
  "cd mealie-dms-service && docker compose logs -f dms-worker"

# Watch Prefect server logs
ssh -i ~/.ssh/id_rsa_chameleon cc@192.5.87.45 \
  "cd mealie-dms-service && docker compose logs -f prefect-server"
```
