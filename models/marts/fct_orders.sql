with order_totals as (
    select
        order_id,
        sum(line_amount) as order_amount,
        sum(quantity) as item_quantity
    from {{ ref('stg_order_items') }}
    group by 1
)

select
    o.order_id,
    o.customer_id,
    o.order_date,
    o.status,
    coalesce(t.order_amount, 0) as order_amount,
    coalesce(t.item_quantity, 0) as item_quantity
from {{ ref('stg_orders') }} o
left join order_totals t on o.order_id = t.order_id
