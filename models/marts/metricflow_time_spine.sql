-- Required by the dbt Semantic Layer (MetricFlow) for time-based metric calculations.
-- The date-spine function itself is not portable SQL -- DuckDB's table function
-- (generate_series) and Spark/Databricks' array function (sequence + explode)
-- spell the same thing differently. Branching on target.type keeps one model
-- working for both adapters instead of forking the file (or pulling in
-- dbt_utils' date_spine macro, which this POC's single model doesn't need).
{% if target.type == 'duckdb' %}
select cast(gs as date) as date_day
from generate_series(date '2000-01-01', date '2030-01-01', interval '1 day') as t(gs)
{% else %}
select explode(sequence(date('2000-01-01'), date('2030-01-01'), interval 1 day)) as date_day
{% endif %}
