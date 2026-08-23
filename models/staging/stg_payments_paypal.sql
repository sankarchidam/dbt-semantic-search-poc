select
    paypal_transaction_id as payment_id,
    order_id,
    'paypal' as channel,
    gross_amount as amount,
    currency_code as currency,
    payment_status as status_raw,
    transaction_time as paid_at
from {{ ref('raw_payments_paypal') }}
