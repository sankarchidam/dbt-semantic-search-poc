-- One row per payment attempt (order grain, not line-item grain -- deliberately
-- not joined to fct_order_items/dim_products, since a payment is for the whole
-- order and joining to line items would fan out and double-count the amount).
-- An order can have more than one row here: a failed attempt followed by a
-- successful retry, often on a different channel.
select
    payment_id,
    order_id,
    channel,
    amount,
    currency,
    status,
    paid_at
from {{ ref('int_payments_unioned') }}
