-- Feedback capture tables for Priority 3.
-- Run once against the dms database if you want to pre-create the tables
-- before the API startup hook issues Base.metadata.create_all(...).

CREATE TABLE IF NOT EXISTS draft_captures (
  draft_id VARCHAR(36) PRIMARY KEY,
  image_key VARCHAR(1024),
  draft_shown JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_draft_captures_image_key ON draft_captures(image_key);
CREATE INDEX IF NOT EXISTS idx_feedback_draft_id ON feedback(draft_id);
CREATE INDEX IF NOT EXISTS idx_feedback_action ON feedback(action);
CREATE INDEX IF NOT EXISTS idx_feedback_image_key ON feedback(image_key);
