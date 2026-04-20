CREATE TABLE IF NOT EXISTS uploads (
  id SERIAL PRIMARY KEY,
  user_id VARCHAR(128) NOT NULL,
  original_filename VARCHAR(512) NOT NULL,
  status VARCHAR(32) NOT NULL,
  incoming_key VARCHAR(1024) NOT NULL UNIQUE,
  curated_object_key VARCHAR(1024),
  mime_type VARCHAR(128),
  checksum_sha256 VARCHAR(128),
  width INTEGER,
  height INTEGER,
  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS objects (
  id SERIAL PRIMARY KEY,
  object_key VARCHAR(1024) NOT NULL UNIQUE,
  checksum_sha256 VARCHAR(128) NOT NULL,
  mime_type VARCHAR(128),
  width INTEGER,
  height INTEGER,
  source_upload_id INTEGER REFERENCES uploads(id),
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS datasets (
  id SERIAL PRIMARY KEY,
  name VARCHAR(256) NOT NULL UNIQUE,
  description TEXT,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dataset_versions (
  id SERIAL PRIMARY KEY,
  dataset_id INTEGER NOT NULL REFERENCES datasets(id),
  version VARCHAR(64) NOT NULL,
  manifest_key VARCHAR(1024) NOT NULL,
  meta_key VARCHAR(1024) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS dataset_items (
  id SERIAL PRIMARY KEY,
  dataset_version_id INTEGER NOT NULL REFERENCES dataset_versions(id),
  object_id INTEGER NOT NULL REFERENCES objects(id),
  label VARCHAR(256),
  split VARCHAR(32),
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS jobs (
  id SERIAL PRIMARY KEY,
  kind VARCHAR(64) NOT NULL,
  status VARCHAR(32) NOT NULL,
  payload_json TEXT NOT NULL,
  celery_task_id VARCHAR(64),
  message TEXT,
  created_at TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS draft_captures (
  draft_id VARCHAR(36) PRIMARY KEY,
  image_key VARCHAR(1024),
  draft_shown JSONB NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS feedback (
  id SERIAL PRIMARY KEY,
  draft_id VARCHAR(36) NOT NULL REFERENCES draft_captures(draft_id),
  image_key VARCHAR(1024),
  draft_shown JSONB NOT NULL,
  final_saved JSONB,
  edit_distance FLOAT,
  action VARCHAR(32) NOT NULL CHECK (action IN ('approved', 'edited', 'rejected')),
  consent BOOLEAN NOT NULL DEFAULT FALSE,
  mealie_recipe_slug VARCHAR(512),
  created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_uploads_user_id ON uploads(user_id);
CREATE INDEX IF NOT EXISTS idx_uploads_checksum ON uploads(checksum_sha256);
CREATE INDEX IF NOT EXISTS idx_objects_checksum ON objects(checksum_sha256);
CREATE INDEX IF NOT EXISTS idx_versions_dataset_id ON dataset_versions(dataset_id);
CREATE INDEX IF NOT EXISTS idx_dataset_items_version_id ON dataset_items(dataset_version_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_draft_captures_image_key ON draft_captures(image_key);
CREATE INDEX IF NOT EXISTS idx_feedback_draft_id ON feedback(draft_id);
CREATE INDEX IF NOT EXISTS idx_feedback_action ON feedback(action);
CREATE INDEX IF NOT EXISTS idx_feedback_image_key ON feedback(image_key);
