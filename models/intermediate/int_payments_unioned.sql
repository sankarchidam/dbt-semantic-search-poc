-- The actual CDM (conformed data model) step: union five payment processor
-- feeds -- already renamed to common column names in staging, but each
-- still speaking its own status vocabulary -- into one grain with one
-- shared meaning of "completed" / "failed" / "pending". This is business
-- logic (deciding that Square's "COMPLETED" and PayPal's "Completed" and
-- e-check's "Cleared" are the same fact), so it belongs here, one layer up
-- from staging, not duplicated across five staging models.
with unioned as (
    select * from {{ ref('stg_payments_square') }}
    union all
    select * from {{ ref('stg_payments_stripe') }}
    union all
    select * from {{ ref('stg_payments_paypal') }}
    union all
    select * from {{ ref('stg_payments_echeck') }}
    union all
    select * from {{ ref('stg_payments_venmo') }}
)

select
    payment_id,
    order_id,
    channel,
    amount,
    currency,
    paid_at,
    case lower(status_raw)
        when 'completed' then 'completed'
        when 'succeeded'  then 'completed'
        when 'cleared'    then 'completed'
        when 'settled'    then 'completed'
        when 'failed'     then 'failed'
        when 'denied'     then 'failed'
        when 'returned'   then 'failed'
        when 'pending'    then 'pending'
        when 'processing' then 'pending'
        else 'unknown'
    end as status
from unioned
