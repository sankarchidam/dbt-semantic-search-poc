select
    customer_id,
    customer_name,
    region,
    segment
from {{ ref('stg_customers') }}
