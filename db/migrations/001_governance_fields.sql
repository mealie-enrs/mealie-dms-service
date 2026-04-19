-- Migration 001: Add data governance fields to uploads and objects tables
-- Run once: psql $DATABASE_URL -f db/migrations/001_governance_fields.sql

BEGIN;

-- ── uploads ──────────────────────────────────────────────────────────────────
ALTER TABLE uploads
    ADD COLUMN IF NOT EXISTS country         VARCHAR(64)  DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS is_test_account BOOLEAN      NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS risk_score      FLOAT        DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS deleted_at      TIMESTAMP    DEFAULT NULL;

CREATE INDEX IF NOT EXISTS ix_uploads_risk_score ON uploads (risk_score);
CREATE INDEX IF NOT EXISTS ix_uploads_deleted_at ON uploads (deleted_at);
CREATE INDEX IF NOT EXISTS ix_uploads_country    ON uploads (country);

-- ── objects ──────────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'objectsource') THEN
        CREATE TYPE objectsource AS ENUM (
            'user_upload',
            'kaggle',
            'recipe1m',
            'synthetic'
        );
    END IF;
END
$$;

ALTER TABLE objects
    ADD COLUMN IF NOT EXISTS source          objectsource NOT NULL DEFAULT 'user_upload',
    ADD COLUMN IF NOT EXISTS deleted_at      TIMESTAMP    DEFAULT NULL,
    ADD COLUMN IF NOT EXISTS is_test_account BOOLEAN      NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS ix_objects_source     ON objects (source);
CREATE INDEX IF NOT EXISTS ix_objects_deleted_at ON objects (deleted_at);

COMMIT;
