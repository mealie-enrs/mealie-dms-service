-- Mart: one row per dataset version with object counts and split breakdown.
-- Used by Metabase dashboards and DataHub lineage.

with versions as (
    select * from {{ ref('stg_dataset_versions') }}
),

items as (
    select
        dataset_version_id,
        split,
        count(*) as item_count
    from dataset_items
    group by dataset_version_id, split
),

pivoted as (
    select
        dataset_version_id,
        sum(case when split = 'train' then item_count else 0 end)  as train_count,
        sum(case when split = 'val'   then item_count else 0 end)  as val_count,
        sum(case when split = 'test'  then item_count else 0 end)  as test_count,
        sum(item_count)                                             as total_count
    from items
    group by dataset_version_id
)

select
    v.version_id,
    v.dataset_id,
    v.version,
    v.version_num,
    v.manifest_key,
    v.swift_prefix,
    v.created_at,
    coalesce(p.train_count, 0)  as train_count,
    coalesce(p.val_count,   0)  as val_count,
    coalesce(p.test_count,  0)  as test_count,
    coalesce(p.total_count, 0)  as total_count

from versions v
left join pivoted p on p.dataset_version_id = v.version_id
order by v.dataset_id, v.version_num desc
