# DMS Workflow — Step by Step

Once the Kaggle download job shows `succeeded`, follow these steps in order.

---

## Step 1 — Download the dataset

Triggers a Celery worker job that downloads from Kaggle and uploads everything directly to Swift object store.

```bash
curl -X POST "http://192.5.87.45:8000/kaggle/download" \
  -H "Content-Type: application/json" \
  -d '{
    "dataset_slug": "pes12017000148/food-ingredients-and-recipe-dataset-with-images",
    "upload_to_swift": true,
    "swift_prefix": "recipe1m/kaggle-food-images"
  }'
```

**Track progress:**
```bash
curl http://192.5.87.45:8000/jobs/1
```

**Watch live worker logs:**
```bash
ssh -i ~/.ssh/id_rsa_chameleon cc@192.5.87.45 \
  "cd mealie-dms-service && docker compose logs -f dms-worker"
```

**Status flow:** `queued → running → succeeded / failed`

**Done when you see:**
```json
{
  "status": "succeeded",
  "message": "downloaded=847 uploaded=847 container=proj26-obj-store prefix=recipe1m/kaggle-food-images"
}
```

---

## Step 2 — Compile the dataset

Reads the CSV + images from Swift, matches them, deduplicates, applies 70/15/15
split, runs Albumentations augmentation on train images, writes a versioned
Parquet manifest back to Swift.

```bash
# Create a dataset record first
curl -X POST "http://192.5.87.45:8000/datasets" \
  -H "Content-Type: application/json" \
  -d '{"name": "food-images-v1", "description": "Kaggle food images dataset"}'

# Compile it (use dataset_id from above response)
curl -X POST "http://192.5.87.45:8000/datasets/1/compile" \
  -H "Content-Type: application/json" \
  -d '{"version": "v1"}'

# Track it
curl http://192.5.87.45:8000/jobs/2
```

**What gets written to Swift:**
```
recipe1m_versions/v1/manifest.parquet   ← versioned dataset manifest
recipe1m_versions/v1/meta.json          ← stats (record count, splits, augmentation)
```

---

## Step 3 — Build the Qdrant index

Takes the compiled manifest, downloads each image from Swift, runs ResNet50,
stores 2048-D embeddings in Qdrant. This enables visual similarity search.

```bash
curl -X POST "http://192.5.87.45:8000/inference/index/build"

# Track it
curl http://192.5.87.45:8000/jobs/3
```

**Check index status:**
```bash
curl http://192.5.87.45:8000/inference/index
```

Returns how many vectors are indexed and collection status.

---

## Step 4 — Test inference

Upload any food image and get back the most similar recipes from the dataset.

```bash
curl -X POST "http://192.5.87.45:8000/inference/features" \
  -F "file=@/path/to/any_food_image.jpg" \
  -F "top_k=5"
```

**Response:**
```json
{
  "model": "resnet50-pretrained",
  "embedding_dim": 2048,
  "top_k": 5,
  "matches": [
    {
      "object_key": "recipe1m/kaggle-food-images/...",
      "title": "Pasta Carbonara",
      "ingredients": "...",
      "split": "train",
      "score": 0.94
    }
  ]
}
```

---

## Step 5 — Check Qdrant dashboard

See how many vectors are indexed and collection stats.

```
http://192.5.87.45:6333/dashboard
```

---

## Step 6 — Run the full pipeline in one shot (next time)

Instead of steps 1-3 individually, trigger the Prefect flow which does
everything end to end automatically.

```bash
curl -X POST "http://192.5.87.45:8000/pipelines/training?version=v2&dataset_id=1&skip_download=true"
```

**Watch it in Prefect UI:**
```
http://192.5.87.45:4200
```

You will see each task (compile, quality check, augmentation, manifest write,
Qdrant index build) as a separate step with status and duration.

---

## Step 7 — Run dbt transforms

Builds clean summary tables in Postgres for Metabase dashboards.

```bash
ssh -i ~/.ssh/id_rsa_chameleon cc@192.5.87.45 \
  "cd mealie-dms-service && docker compose --profile transforms run dbt"
```

**Then in Metabase** (`http://192.5.87.45:3001`):
- Browse data → DMS Postgres → `dbt_dms` schema
- `mart_dataset_summary` → train/val/test counts per version
- `mart_upload_funnel` → upload status breakdown by day

---

## Step 8 — Run Soda quality checks

Validates data integrity in Postgres before publishing a new version.

```bash
ssh -i ~/.ssh/id_rsa_chameleon cc@192.5.87.45 \
  "cd mealie-dms-service && docker compose --profile quality run dms-soda"
```

Pass → exit 0. Fail → exit 1 + error report showing exactly which check failed.

---

## Full flow summary

```
Step 1 — Download (Kaggle → Swift)
    ↓
Step 2 — Compile (CSV × images → Parquet manifest + augmented images in Swift)
    ↓
Step 3 — Build Qdrant index (images → ResNet50 → 2048-D vectors in Qdrant)
    ↓
Step 4 — Inference works (upload image → top-K similar recipes)
    ↓
Step 7 — dbt transforms (Postgres → clean marts → Metabase dashboards)
    ↓
Step 8 — Soda quality checks (gate before publishing next version)
```

---

## Service URLs

| Service | URL | Purpose |
|---|---|---|
| DMS API | http://192.5.87.45:8000 | Main API |
| API Docs (Swagger) | http://192.5.87.45:8000/docs | Interactive API explorer |
| API Metrics | http://192.5.87.45:8000/metrics | Prometheus metrics |
| Qdrant UI | http://192.5.87.45:6333/dashboard | Vector index browser |
| Prefect UI | http://192.5.87.45:4200 | Pipeline run history |
| Grafana | http://192.5.87.45:3000 | Metrics dashboards (admin/admin) |
| Prometheus | http://192.5.87.45:9090 | Raw metrics |
| Metabase | http://192.5.87.45:3001 | Data exploration |

---

## Job tracking

Every operation (download, compile, index build) creates a job you can track:

```bash
# Check any job by id
curl http://192.5.87.45:8000/jobs/{job_id}

# Watch worker logs live
ssh -i ~/.ssh/id_rsa_chameleon cc@192.5.87.45 \
  "cd mealie-dms-service && docker compose logs -f dms-worker"

# Or track in Metabase → jobs table
http://192.5.87.45:3001
```
