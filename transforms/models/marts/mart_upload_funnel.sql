-- Mart: upload funnel — how many uploads reach each status.
-- Useful for monitoring data ingestion health in Metabase / Grafana.

select
    status,
    count(*)                                        as upload_count,
    min(created_at)                                 as first_seen,
    max(created_at)                                 as last_seen,
    date_trunc('day', created_at)                   as upload_day

from uploads
group by status, date_trunc('day', created_at)
order by upload_day desc, status
