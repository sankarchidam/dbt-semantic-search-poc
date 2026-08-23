#!/usr/bin/env python3
"""
Generate this project's synthetic seed CSVs -- a small personal-care shop:
customers, products, orders, order line items, and payment transactions
across five independent processor feeds (Square, Stripe, PayPal, e-check,
Venmo) -- deliberately with different schemas, units, and status
vocabularies, so the staging -> intermediate -> marts CDM unification in
models/ has real unification work to do, not a toy union.

Order dates are anchored to *today* (not a hardcoded past date), spanning
the last ~8 months up to and including today. This is what makes
relative-time queries like "revenue last month" or "items sold this year"
actually have data to return -- a hardcoded date range goes stale the
moment "today" moves past it (see README's "LLM query interpretation"
section for the failure mode this caused before this script existed).

Run this whenever you want the demo data to look "current" again, then
rebuild:
    python scripts/generate_seed_data.py
    DBT_PROFILES_DIR=. dbt seed --full-refresh
    DBT_PROFILES_DIR=. dbt build --full-refresh
    DBT_PROFILES_DIR=. dbt parse

Deterministic given the same "today" (random.seed is fixed), so the
customer/product roster doesn't reshuffle on every run -- only the order
and payment dates shift forward as "today" does.
"""
import csv
import datetime
import random
from pathlib import Path

SEEDS_DIR = Path(__file__).resolve().parent.parent / "seeds"

random.seed(7)

FIRST_NAMES = [
    "Michael", "Sarah", "James", "Emily", "David", "Jessica", "Daniel", "Ashley",
    "Matthew", "Amanda", "Andrew", "Melissa", "Joshua", "Stephanie", "Christopher",
    "Nicole", "Ryan", "Elizabeth", "Brandon", "Rachel", "Justin", "Lauren", "Kevin",
    "Megan", "Brian", "Amy", "Jason", "Kimberly", "Eric", "Laura", "Adam", "Rebecca",
    "Jonathan", "Michelle", "Aaron", "Samantha", "Nathan", "Victoria", "Tyler", "Hannah",
    "Olivia", "Ethan", "Sophia", "Noah", "Isabella", "Liam", "Mia", "William",
    "Charlotte", "Benjamin", "Amelia", "Lucas", "Harper", "Henry", "Evelyn", "Alexander",
    "Abigail", "Sebastian", "Emily", "Jack", "Elizabeth", "Owen", "Sofia", "Wyatt",
]
LAST_NAMES = [
    "Arkinson", "Johnson", "Williams", "Brown", "Davis", "Miller", "Wilson", "Moore",
    "Taylor", "Anderson", "Thomas", "Jackson", "White", "Harris", "Martin", "Thompson",
    "Garcia", "Martinez", "Robinson", "Clark", "Rodriguez", "Lewis", "Walker", "Hall",
    "Allen", "Young", "King", "Wright", "Scott", "Green", "Baker", "Nelson",
    "Carter", "Mitchell", "Perez", "Roberts", "Turner", "Phillips", "Campbell", "Parker",
]

REGIONS = ["West", "East", "North", "South"]
SEGMENTS = ["Enterprise", "SMB", "Consumer"]

# (product_id, name, category)
PRODUCTS = [
    (1, "Lavender Shampoo", "Hair Care"),
    (2, "Coconut Conditioner", "Hair Care"),
    (3, "Argan Skin Oil", "Skin Care"),
    (4, "Shea Butter Lotion", "Skin Care"),
    (5, "Oatmeal Bar Soap", "Bath & Body"),
    (6, "Charcoal Bar Soap", "Bath & Body"),
    (7, "Citrus Bloom Perfume", "Fragrance"),
    (8, "Sandalwood Perfume", "Fragrance"),
]

UNIT_PRICE_RANGES = {
    1: (7.99, 14.99), 2: (7.99, 14.99),
    3: (12.99, 24.99), 4: (9.99, 18.99),
    5: (4.99, 8.99), 6: (4.99, 8.99),
    7: (29.99, 59.99), 8: (29.99, 59.99),
}

ORDER_WINDOW_DAYS = 227   # ~7.5 months of history, ending today
NUM_CUSTOMERS = 120
NUM_ORDERS = 400

# Rough real-world mix for a small shop: cards dominate, Venmo/e-check are a
# long tail.
CHANNEL_WEIGHTS = {"square": 30, "stripe": 30, "paypal": 20, "venmo": 12, "echeck": 8}
CARD_BRANDS = ["Visa", "Mastercard", "Amex", "Discover"]
BANK_NAMES = ["Chase", "Wells Fargo", "Bank of America", "Citibank", "US Bank"]


def write_products():
    with open(SEEDS_DIR / "raw_products.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["product_id", "product_name", "category"])
        w.writeheader()
        for pid, name, cat in PRODUCTS:
            w.writerow({"product_id": pid, "product_name": name, "category": cat})


def write_customers():
    used = set()
    customers = []
    for i in range(1, NUM_CUSTOMERS + 1):
        while True:
            name = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
            if name not in used:
                used.add(name)
                break
        customers.append({
            "customer_id": i,
            "customer_name": name,
            "region": random.choice(REGIONS),
            "segment": random.choice(SEGMENTS),
        })
    with open(SEEDS_DIR / "raw_customers.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["customer_id", "customer_name", "region", "segment"])
        w.writeheader()
        w.writerows(customers)
    return customers


def write_orders(customers, today: datetime.date):
    start = today - datetime.timedelta(days=ORDER_WINDOW_DAYS)
    orders = []
    for order_id in range(1, NUM_ORDERS + 1):
        cust = random.choice(customers)
        d = start + datetime.timedelta(days=random.randint(0, ORDER_WINDOW_DAYS))
        orders.append({
            "order_id": order_id,
            "customer_id": cust["customer_id"],
            "order_date": d.isoformat(),
            "status": random.choices(["completed", "cancelled", "returned"], weights=[85, 10, 5])[0],
        })
    with open(SEEDS_DIR / "raw_orders.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["order_id", "customer_id", "order_date", "status"])
        w.writeheader()
        w.writerows(orders)
    return orders


def write_order_items(orders):
    items = []
    item_id = 1
    for o in orders:
        for _ in range(random.randint(1, 4)):
            pid, _, _ = random.choice(PRODUCTS)
            lo, hi = UNIT_PRICE_RANGES[pid]
            items.append({
                "order_item_id": item_id,
                "order_id": o["order_id"],
                "product_id": pid,
                "quantity": random.randint(1, 3),
                "unit_price": round(random.uniform(lo, hi), 2),
            })
            item_id += 1
    with open(SEEDS_DIR / "raw_order_items.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["order_item_id", "order_id", "product_id", "quantity", "unit_price"])
        w.writeheader()
        w.writerows(items)
    return items


def order_totals(items):
    totals = {}
    for it in items:
        totals[it["order_id"]] = totals.get(it["order_id"], 0.0) + it["quantity"] * it["unit_price"]
    return {oid: round(total, 2) for oid, total in totals.items()}


# --- one row-builder per channel, each returning that channel's own schema ---

def _square_row(seq, order_id, amount, status, dt):
    return {
        "square_payment_id": f"sq_{seq:06d}",
        "order_id": order_id,
        "amount_cents": round(amount * 100),
        "currency": "USD",
        "status": {"completed": "COMPLETED", "failed": "FAILED", "pending": "PENDING"}[status],
        "created_at": dt.isoformat(),
        "card_brand": random.choice(CARD_BRANDS),
    }


def _stripe_row(seq, order_id, amount, status, dt):
    return {
        "stripe_charge_id": f"ch_{seq:06d}",
        "order_id": order_id,
        "amount_cents": round(amount * 100),
        "currency": "usd",
        "status": {"completed": "succeeded", "failed": "failed", "pending": "pending"}[status],
        "created": dt.isoformat(),
        "payment_method_type": random.choices(["card", "us_bank_account"], weights=[9, 1])[0],
    }


def _paypal_row(seq, order_id, amount, status, dt):
    return {
        "paypal_transaction_id": f"PP-{seq:06d}",
        "order_id": order_id,
        "gross_amount": amount,
        "currency_code": "USD",
        "payment_status": {"completed": "Completed", "failed": "Denied", "pending": "Pending"}[status],
        "transaction_time": dt.isoformat(),
    }


def _echeck_row(seq, order_id, amount, status, dt):
    return {
        "check_txn_id": f"ECHK-{seq:06d}",
        "order_id": order_id,
        "amount": amount,
        "status": {"completed": "Cleared", "failed": "Returned", "pending": "Processing"}[status],
        "cleared_date": dt.isoformat(),
        "bank_name": random.choice(BANK_NAMES),
    }


def _venmo_row(seq, order_id, amount, status, dt):
    return {
        "venmo_txn_id": f"VNM-{seq:06d}",
        "order_id": order_id,
        "amount": amount,
        "status": {"completed": "settled", "failed": "failed", "pending": "pending"}[status],
        "txn_date": dt.isoformat(),
        "note": "",
    }


CHANNELS = {
    "square": {
        "fields": ["square_payment_id", "order_id", "amount_cents", "currency", "status", "created_at", "card_brand"],
        "build": _square_row,
    },
    "stripe": {
        "fields": ["stripe_charge_id", "order_id", "amount_cents", "currency", "status", "created", "payment_method_type"],
        "build": _stripe_row,
    },
    "paypal": {
        "fields": ["paypal_transaction_id", "order_id", "gross_amount", "currency_code", "payment_status", "transaction_time"],
        "build": _paypal_row,
    },
    "echeck": {
        "fields": ["check_txn_id", "order_id", "amount", "status", "cleared_date", "bank_name"],
        "build": _echeck_row,
    },
    "venmo": {
        "fields": ["venmo_txn_id", "order_id", "amount", "status", "txn_date", "note"],
        "build": _venmo_row,
    },
}


def _pick_channel():
    return random.choices(list(CHANNEL_WEIGHTS), weights=list(CHANNEL_WEIGHTS.values()))[0]


def write_payments(orders, totals, today: datetime.date):
    """One or two payment attempts per order, spread across 5 processor
    feeds with different schemas. ~12% of orders have a failed attempt
    followed by a successful retry (often on a different channel) -- this
    is what makes transaction count exceed order count, same as real
    payment data."""
    rows_by_channel = {ch: [] for ch in CHANNELS}
    seq_by_channel = {ch: 1 for ch in CHANNELS}

    def emit(channel, order_id, amount, status, dt):
        seq = seq_by_channel[channel]
        seq_by_channel[channel] += 1
        rows_by_channel[channel].append(CHANNELS[channel]["build"](seq, order_id, amount, status, dt))

    for o in orders:
        order_id = o["order_id"]
        amount = totals.get(order_id, 0.0)
        order_date = datetime.date.fromisoformat(o["order_date"])
        r = random.random()

        if r < 0.12:
            # failed attempt, then a successful retry -- often a different channel
            first_channel = _pick_channel()
            emit(first_channel, order_id, amount, "failed", order_date)
            retry_channel = _pick_channel()
            retry_date = min(order_date + datetime.timedelta(days=1), today)
            emit(retry_channel, order_id, amount, "completed", retry_date)
        elif r < 0.15:
            emit(_pick_channel(), order_id, amount, "failed", order_date)
        elif r < 0.18:
            emit(_pick_channel(), order_id, amount, "pending", order_date)
        else:
            emit(_pick_channel(), order_id, amount, "completed", order_date)

    total_rows = 0
    for channel, spec in CHANNELS.items():
        rows = rows_by_channel[channel]
        total_rows += len(rows)
        with open(SEEDS_DIR / f"raw_payments_{channel}.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=spec["fields"])
            w.writeheader()
            w.writerows(rows)
    return total_rows, {ch: len(rows) for ch, rows in rows_by_channel.items()}


def main():
    today = datetime.date.today()
    write_products()
    customers = write_customers()
    orders = write_orders(customers, today)
    items = write_order_items(orders)
    totals = order_totals(items)
    payment_count, by_channel = write_payments(orders, totals, today)

    start = today - datetime.timedelta(days=ORDER_WINDOW_DAYS)
    print(f"Generated seeds anchored to today ({today.isoformat()}):")
    print(f"  products={len(PRODUCTS)} customers={len(customers)} orders={len(orders)} items={len(items)}")
    print(f"  payments={payment_count} across channels: " + ", ".join(f"{ch}={n}" for ch, n in by_channel.items()))
    print(f"  order_date range: {start.isoformat()} .. {today.isoformat()}")


if __name__ == "__main__":
    main()
