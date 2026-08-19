-- Required by the dbt Semantic Layer (MetricFlow) for time-based metric calculations.
-- DuckDB-native date spine (no dbt_utils dependency needed for this POC).
select cast(gs as date) as date_day
from generate_series(date '2000-01-01', date '2030-01-01', interval '1 day') as t(gs)
