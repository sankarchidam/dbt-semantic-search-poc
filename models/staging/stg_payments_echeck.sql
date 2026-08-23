select
    check_txn_id as payment_id,
    order_id,
    'echeck' as channel,
    amount,
    'USD' as currency,
    status as status_raw,
    cleared_date as paid_at
from {{ ref('raw_payments_echeck') }}
