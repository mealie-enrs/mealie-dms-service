-- Staging: clean objects table.
-- Objects are content-addressed food images approved and stored in Swift.

select
    id                  as object_id,
    object_key,
    checksum_sha256,
    mime_type,
    width,
    height,
    source_upload_id,
    created_at,

    -- Derived: file extension from object key
    lower(split_part(object_key, '.', -1))      as file_ext,

    -- Derived: content-addressed path depth (sha256 prefix bucketing)
    split_part(object_key, '/', 3)              as sha_bucket

from objects
