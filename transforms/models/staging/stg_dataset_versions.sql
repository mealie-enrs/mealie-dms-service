-- Staging: clean and normalise dataset_versions from the DMS Postgres DB.
-- Adds derived columns useful for downstream analysis.

select
    id                                          as version_id,
    dataset_id,
    version,
    manifest_key,
    meta_key,
    created_at,

    -- Extract version number for sorting (e.g. "v2" → 2)
    regexp_replace(version, '[^0-9]', '', 'g')::int  as version_num,

    -- Swift container derived from manifest_key prefix
    split_part(manifest_key, '/', 1)            as swift_prefix

from dataset_versions
