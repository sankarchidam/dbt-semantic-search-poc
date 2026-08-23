select
    square_payment_id as payment_id,
    order_id,
    'square' as channel,
    amount_cents / 100.0 as amount,
    currency,
    status as status_raw,
    created_at as paid_at
from {{ ref('raw_payments_square') }}
