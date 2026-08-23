select
    venmo_txn_id as payment_id,
    order_id,
    'venmo' as channel,
    amount,
    'USD' as currency,
    status as status_raw,
    txn_date as paid_at
from {{ ref('raw_payments_venmo') }}
