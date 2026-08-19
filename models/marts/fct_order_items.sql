-- Line-item grain fact: one row per order line. Kept separate from fct_orders
-- (order grain) so the semantic layer can offer product/category breakdowns
-- that fct_orders' order-level aggregation has already thrown away.
select
    oi.order_item_id,
    oi.order_id,
    oi.product_id,
    o.order_date,
    oi.quantity,
    oi.unit_price,
    oi.line_amount
from {{ ref('stg_order_items') }} oi
left join {{ ref('stg_orders') }} o on oi.order_id = o.order_id
