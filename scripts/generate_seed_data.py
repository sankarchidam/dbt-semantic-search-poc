#!/usr/bin/env python3
"""
Generate this project's synthetic seed CSVs -- a small personal-care shop:
customers, products, orders, and order line items.

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
dates shift forward as "today" does.
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
]
LAST_NAMES = [
    "Arkinson", "Johnson", "Williams", "Brown", "Davis", "Miller", "Wilson", "Moore",
    "Taylor", "Anderson", "Thomas", "Jackson", "White", "Harris", "Martin", "Thompson",
    "Garcia", "Martinez", "Robinson", "Clark", "Rodriguez", "Lewis", "Walker", "Hall",
    "Allen", "Young", "King", "Wright", "Scott", "Green",
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
NUM_CUSTOMERS = 30
NUM_ORDERS = 220


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


def main():
    today = datetime.date.today()
    write_products()
    customers = write_customers()
    orders = write_orders(customers, today)
    items = write_order_items(orders)
    start = today - datetime.timedelta(days=ORDER_WINDOW_DAYS)
    print(f"Generated seeds anchored to today ({today.isoformat()}):")
    print(f"  products={len(PRODUCTS)} customers={len(customers)} orders={len(orders)} items={len(items)}")
    print(f"  order_date range: {start.isoformat()} .. {today.isoformat()}")


if __name__ == "__main__":
    main()
