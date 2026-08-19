select
    customer_id,
    customer_name,
    region,
    segment
from {{ ref('raw_customers') }}
