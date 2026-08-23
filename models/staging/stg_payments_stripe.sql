select
    stripe_charge_id as payment_id,
    order_id,
    'stripe' as channel,
    amount_cents / 100.0 as amount,
    upper(currency) as currency,
    status as status_raw,
    created as paid_at
from {{ ref('raw_payments_stripe') }}
