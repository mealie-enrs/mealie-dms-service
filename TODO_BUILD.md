# DMS — Remaining Deliverables

## What Already Exists


| Requirement                                | Status         | What Exists                                                                             |
| ------------------------------------------ | -------------- | --------------------------------------------------------------------------------------- |
| Live object storage on Chameleon           | **DONE**       | Swift containers `proj26-training-data`, `proj26-user-uploads` with Kaggle food dataset |
| External data ingestion pipeline           | **DONE**       | `POST /kaggle/download` + Celery worker pipeline                                        |
| Dataset versioning                         | **PARTIAL**    | Manifest-based versioning exists, but no train/val/test splits                          |
| Data design document                       | **NEEDS WORK** | `readme.md` has basics, needs formal doc with diagrams                                  |
| Data generator (synthetic load)            | **NOT BUILT**  |                                                                                         |
| Online feature computation                 | **NOT BUILT**  |                                                                                         |
| Batch pipeline (versioned train/eval sets) | **NOT BUILT**  |                                                                                         |
| Candidate selection + leakage avoidance    | **NOT BUILT**  |                                                                                         |


---

## Requirement-by-Requirement Plan

### 1. Data Flow Description (1 pt)

**Inference path (real-time):**

- User uploads a food image via the mealie app
- `POST /uploads/init` creates an incoming record, image lands in `proj26-user-uploads/incoming/`
- An online feature extraction service takes the image, resizes it, extracts embeddings (e.g. ResNet/CLIP features), and returns a recipe prediction or ingredient list
- No training data is written here — this is read-only against the model

**Training path (batch):**

- External data (Kaggle food-images dataset) is ingested via `POST /kaggle/download` into `proj26-training-data/recipe1m/`
- User-uploaded images that are approved (`POST /uploads/{id}/approval`) get promoted from `proj26-user-uploads` to `proj26-training-data/objects/sha256/...`
- A batch pipeline compiles a versioned dataset from these objects, applying candidate selection and train/val/test splitting
- The versioned dataset is published as a Parquet manifest (`recipe1m_versions/v2/manifest.parquet`) that points to the immutable objects
- Training jobs read from the manifest + object store

---

### 2. Training Data Specifics (1 pt) — Candidate Selection + Leakage Avoidance

**Candidate selection (4.7.2):**

- Filter out corrupt/truncated images (can't be decoded)
- Filter out tiny images (<64px)
- Filter out duplicates (SHA-256 dedup in `objects` table)
- Filter out images with missing labels (images without a matching CSV row get excluded)

**Leakage avoidance (4.7.5):**

- Split by **recipe identity**, not by individual image — multiple images of the same dish go to the same split
- Use the recipe title/ID from the CSV as the grouping key
- 70/15/15 train/val/test split at the recipe level
- `DatasetItem.split` field stores the assignment

---

### 3. Data Repositories


| Repository                     | Type         | What's Stored                                                | Writers             | Versioning                             |
| ------------------------------ | ------------ | ------------------------------------------------------------ | ------------------- | -------------------------------------- |
| `proj26-user-uploads` (Swift)  | Object store | Raw user uploads                                             | dms-api, dms-worker | Not versioned (transient)              |
| `proj26-training-data` (Swift) | Object store | Curated objects, manifests, Kaggle data                      | dms-worker          | Manifest-versioned (v1, v2, ...)       |
| PostgreSQL `dms`               | RDBMS        | Metadata (uploads, objects, datasets, versions, items, jobs) | dms-api, dms-worker | Row-level timestamps + version records |
| Redis                          | In-memory    | Celery task queue + results                                  | dms-api, dms-worker | Not versioned (ephemeral)              |


---

## What Needs to Be Built

### A. Batch Pipeline — Versioned Train/Eval Sets (🎥📄)

A Celery task `compile_training_dataset` that:

1. Reads the Kaggle CSV to get labels (recipe title, ingredients)
2. Matches images to labels
3. Applies candidate selection (filter corrupt, tiny, unlabeled)
4. Splits by recipe-level grouping (70/15/15)
5. Writes a versioned `manifest.parquet` with columns: `object_key, label, split, ingredients, checksum`
6. Publishes as a `DatasetVersion`

### B. Data Generator / Synthetic Load (🎥📄)

A script `scripts/data_generator.py` that:

1. Hits `POST /uploads/init` with synthetic user IDs
2. Uploads synthetic food images (augmented versions of existing dataset images — rotations, color jitter, crops)
3. Calls `POST /uploads/{id}/approval` to approve them
4. Runs continuously for the demo video

Since dataset is <5GB (206MB), must expand with synthetic augmentation (Pillow transforms: random crops, flips, color jitter, rotation).

### C. Online Feature Computation (🎥📄)

A new endpoint `POST /inference/features` that:

1. Accepts an image (base64 or file upload)
2. Resizes to standard size (224x224)
3. Runs through a pre-trained model (ResNet-50 or CLIP) to extract an embedding vector
4. Returns the embedding + top-k nearest recipe matches from the training set

Doesn't need full mealie integration, just "integrate-able" — a standalone endpoint demonstrating the feature computation path.

### D. Data Design Document (📝)

Formal document with:

- All data repositories enumerated with schemas
- Services that write/update each store
- Versioning strategy
- Data flow diagrams (ingestion, inference, training)

---

## Priority Order

1. **A — Batch pipeline** (highest value: covers candidate selection, leakage, versioning, batch pipeline demo)
2. **B — Data generator** (relatively simple script)
3. **C — Online feature computation** (needs pre-trained model, most complex)
4. **D — Data design document** (write based on what exists)
