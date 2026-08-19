select
    order_item_id,
    order_id,
    product_id,
    quantity,
    unit_price,
    quantity * unit_price as line_amount
from {{ ref('raw_order_items') }}
