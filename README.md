# Lakehouse Semantic Search — mini working model

A small but real end-to-end system, seeded with a small personal-care shop's
data (shampoo, skin oil, bar soap, perfume, lotion, real-looking customer
names): a DuckDB lakehouse, transformed by dbt, with a dbt Semantic Layer
(MetricFlow) on top, searchable in plain English through **one shared
catalog** of metrics, dimensions, and tables — via either a CLI or a local
web UI. Search never re-derives business logic; it only finds the right
pointer into the semantic layer, so a result is always consistent with what
the semantic layer itself would compute.

```
                    ┌─────────────────────────────────────────┐
                    │        seeds/  (raw CSVs)                │
                    │  customers · orders · order_items · products
                    └───────────────────┬───────────────────────┘
                                        │ dbt (staging → marts)
                                        ▼
     ┌──────────────────────────────────────────────────────────┐
     │                DuckDB lakehouse (dev.duckdb)                │
     │   dim_customers · dim_products · fct_orders · fct_order_items │
     └───────────────────────────┬──────────────────────────────┘
                                 │ semantic_models.yml / metrics.yml
                                 ▼
     ┌──────────────────────────────────────────────────────────┐
     │        dbt Semantic Layer (MetricFlow) — 6 metrics           │
     │    revenue · order_count · average_order_value ·             │
     │    total_items_sold · product_revenue · items_sold           │
     └───────────────────────────┬──────────────────────────────┘
                                 │ scripts/catalog.py
                                 ▼
     ┌──────────────────────────────────────────────────────────┐
     │      Unified search catalog — metrics + dimensions + tables   │
     │      embedded locally with sentence-transformers               │
     └───────┬───────────────────────────────────────┬────────────┘
             │                                       │
             ▼                                       ▼
   scripts/semantic_search.py               scripts/app.py (Flask)
        CLI search + run                  web UI: search → pick metric
                                          → toggle dimensions → run → table
```

## Why this shape

The semantic layer (not the search layer) owns the ground truth about what
"revenue" means, how it's calculated, and what it can be sliced by. Search's
only job is to find the right *pointer* into that catalog — metric,
dimension, or table — never to re-derive business logic itself. That's why
`scripts/catalog.py` is built directly from dbt's own compiled artifacts
(`semantic_manifest.json`, `manifest.json`) and MetricFlow's own dimension
resolution (`mf list metrics`), rather than from hand-maintained metadata.

## Architecture

| Layer | Tool | Role |
|---|---|---|
| Lakehouse storage/query engine | **DuckDB** | Local, zero-setup stand-in for a lakehouse (swap for Snowflake/Databricks/BigQuery later — same dbt project) |
| Transformation | **dbt-core** (`dbt-duckdb` adapter) | Raw seeds → staging views → 4 mart tables across 3 domains (customers, products, orders) |
| Semantic layer | **dbt Semantic Layer / MetricFlow** (`semantic_models.yml`, `metrics.yml`) | Entities, dimensions, measures, and 6 metrics defined on top of the marts — the single source of truth |
| Query execution | **MetricFlow CLI (`mf`)** | Compiles a metric + dimension request into SQL and runs it against DuckDB, no dbt Cloud needed |
| **Unified catalog** | `scripts/catalog.py` | Reads dbt's compiled manifests + `mf list metrics` and produces one list of searchable items: **metrics**, **dimensions**, and **tables/columns** |
| Search index | `scripts/search_index.py` + **sentence-transformers** (`all-MiniLM-L6-v2`, local, no API key) | Embeds every catalog item once at startup; ranks by cosine similarity against a query |
| Query interpretation | `scripts/llm_interpreter.py` (**opt-in**, OpenAI, `OPENAI_API_KEY`) | Resolves group-by dimensions, real dates ("last month" → concrete start/end), *and* value filters ("shampoo" → `product__product_name ILIKE '%shampoo%'`) via structured output, constrained to each metric's actual dimensions. Falls back to `catalog.guess_group_by`'s keyword heuristic (no dates, no filters) if no key is set |
| **CLI** | `scripts/semantic_search.py` | `python scripts/semantic_search.py "..." --run` |
| **Web UI** | `scripts/app.py` (Flask) + `scripts/templates/index.html` | Search box → ranked results with type badges → toggle group-by dimension chips on a metric → Run → live result table, all in the browser. "Ask AI" toggle switches to LLM interpretation |

## Project layout

```
seeds/                       synthetic raw CSVs: customers, products, orders, order_items (generated, not hand-written)
models/staging/               1:1 cleaned views over the seeds
models/marts/                 dim_customers, dim_products, fct_orders, fct_order_items, metricflow_time_spine
models/semantic/               semantic_models.yml, metrics.yml   ← the semantic layer
scripts/generate_seed_data.py  (re)generates seeds/*.csv with order dates anchored to today
scripts/catalog.py             builds the unified metrics+dimensions+tables catalog
scripts/search_index.py        embeds the catalog, ranks a query against it
scripts/llm_interpreter.py     opt-in OpenAI call: real dates + validated group-by, replacing the keyword heuristic
scripts/semantic_search.py     CLI: search (+ optionally run a metric)
scripts/app.py                 Flask web UI (same catalog/index, browser front end)
scripts/templates/index.html   the search UI page
```

## Data model — a small multi-domain lakehouse

Seeded to look like a real small business: a personal-care shop. 30 customers
with realistic full names (e.g. "Michael Arkinson", not "Customer 1"), and 8
household/personal-care products across four categories:

| Product | Category |
|---|---|
| Lavender Shampoo, Coconut Conditioner | Hair Care |
| Argan Skin Oil, Shea Butter Lotion | Skin Care |
| Oatmeal Bar Soap, Charcoal Bar Soap | Bath & Body |
| Citrus Bloom Perfume, Sandalwood Perfume | Fragrance |

Two grains, deliberately, to make the semantic layer's job of disambiguation
real rather than trivial:

- `dim_customers` — one row per customer (`region`, `segment`)
- `dim_products` — one row per product (`category`)
- `fct_orders` — **order grain**: total amount/quantity per order, with status and date. Product detail is lost at this grain.
- `fct_order_items` — **line-item grain**: one row per order line, preserving `product_id`, so it can be broken down by product/category.

## The semantic layer — 6 metrics, including two near-duplicate pairs on purpose

| Metric | Grain | Sliceable by |
|---|---|---|
| `revenue` | order | region, segment, status, time |
| `order_count` | order | region, segment, status, time |
| `average_order_value` | order (ratio) | region, segment, status, time |
| `total_items_sold` | order | region, segment, status, time — **not** product |
| `product_revenue` | line item | region, segment, status, time, **product, category** |
| `items_sold` | line item | region, segment, status, time, **product, category** |

`total_items_sold` vs `items_sold`, and `revenue` vs `product_revenue`, are
intentionally near-duplicates with different grain and different available
dimensions. Each has a description explaining when to use it — this is the
disambiguation problem semantic search actually needs to solve, and you can
see it happen: search `"units sold by product"` and `items_sold` wins over
`total_items_sold`, correctly.

## Setup

```bash
cd dbt-semantic-search-poc
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Build the lakehouse + semantic layer

```bash
export DBT_PROFILES_DIR=.
dbt seed --full-refresh   # load synthetic CSVs into DuckDB (--full-refresh forces schema changes to apply)
dbt build                 # build staging/mart models + run tests
dbt parse                 # compile the semantic layer -> target/semantic_manifest.json, target/manifest.json
```

Sanity-check the semantic layer directly:

```bash
mf list metrics --show-all-dimensions
mf query --metrics product_revenue --group-by product__category
```

## Run it — CLI

```bash
python scripts/semantic_search.py "monthly revenue by region"
python scripts/semantic_search.py "revenue by product category" --run
python scripts/semantic_search.py "customer region" --top-k 5   # a dimension, not a metric -- shows what it can slice
python scripts/semantic_search.py "how many items sold last month" --run   # needs OPENAI_API_KEY for the real date range
```

By default this uses `catalog.guess_group_by`'s keyword heuristic. If
`OPENAI_API_KEY` is set, it automatically switches to LLM interpretation
(see **LLM query interpretation**, below) — pass `--no-llm` to force the
heuristic even with a key set.

## Run it — web UI (the "mini working model")

```bash
export OPENAI_API_KEY=sk-...   # optional -- enables the "Ask AI" toggle
DBT_PROFILES_DIR=. python scripts/app.py
# open http://127.0.0.1:5001
```

Type a question (e.g. *"revenue by product category"*). Results come back
ranked with a type badge — `METRIC` / `DIMENSION` / `TABLE`. A metric result
shows chips for every dimension it can be grouped by (pre-selected using the
same guess-from-query-words heuristic as the CLI); toggle chips and click
**Run query** to execute it live via MetricFlow against DuckDB and render
the result table in the browser. A dimension result shows which metrics it
slices; a table result shows its columns — plain data-catalog discovery,
no query execution needed.

If `OPENAI_API_KEY` is set on the server, an **"Ask AI"** checkbox appears
above the search box. With it on, the same query goes through
`llm_interpreter.py` instead: the result card shows a resolved date range
(e.g. `📅 2025-07-01 → 2025-07-31`) alongside the chips, computed from the
actual calendar rather than guessed from a keyword.

## How seeds load into DuckDB

No custom ETL script — `dbt seed` is the entire loading mechanism here. It
reads the CSVs in `seeds/` and writes tables directly into the single
`dev.duckdb` file that *is* the lakehouse:

1. **Source: plain CSVs.** `seeds/raw_customers.csv`, `raw_products.csv`,
   `raw_orders.csv`, `raw_order_items.csv` — anything under `seed-paths` in
   `dbt_project.yml` is a loadable seed. These are generated by
   `scripts/generate_seed_data.py`, not hand-written or committed as static
   fixtures forever: order dates are anchored to *today* (the last ~7.5
   months up to and including whenever you run it), so relative-time
   queries like "revenue last month" always have real data to return — see
   **LLM query interpretation**, below, for what happens when they don't.
2. **Column types, pinned.** `seeds/schema.yml` overrides dbt's type
   inference per column — e.g. forcing `order_date` to `date` rather than
   leaving it as text.
3. **`dbt seed` writes the tables.** Using the connection in `profiles.yml`
   (`type: duckdb`, `path: dev.duckdb`), dbt issues `CREATE TABLE` + a bulk
   `INSERT` into the `main` schema — one table per CSV, named after the file.
4. **Everything downstream reads via `ref()`.** Staging models select
   straight from the seed tables (`from {{ ref('raw_customers') }}`);
   `dbt build` then runs marts on top — all inside that same `.duckdb` file.

> **Schema changes need `--full-refresh`.** A plain `dbt seed` rerun replaces
> *rows*, but on `dbt-duckdb` it doesn't reliably replace a table's *schema*
> if a CSV's columns changed — e.g. swapping `raw_order_items`'s old
> `product_name` column for `product_id` silently kept the old schema until
> rerun with `--full-refresh`, which forces a drop + recreate before loading.

> **Keeping the demo data current.** `generate_seed_data.py`'s order dates
> only stay recent as of whenever you last ran it — rerun it (then reseed +
> rebuild) periodically if "last month"/"this year"-style queries have gone
> stale:
> ```bash
> python scripts/generate_seed_data.py
> dbt seed --full-refresh && dbt build --full-refresh && dbt parse
> ```

This is also why swapping DuckDB for a real warehouse later is low-friction:
the same `dbt seed` mechanism applies unchanged on Snowflake, Databricks, or
BigQuery — only the connection in `profiles.yml` changes. (`dbt seed` itself
is really only meant for small, static reference data even in production —
see **Transformation**, below, for how real operational data gets in.)

## Transformation

Between raw rows and the semantic layer sits dbt's actual job: turning
loosely-typed source tables into the star schema `metrics.yml` depends on.
Two layers, two different jobs:

| Layer | Job | Models |
|---|---|---|
| **Staging** | One view per source table. Renames/casts columns, computes trivial derived fields. No joins, no business logic, no aggregation. | `stg_customers`, `stg_products`, `stg_orders`, `stg_order_items` |
| **Marts** | Joins staging models together, chooses a grain, and materializes the fact/dim tables the semantic layer references by name. | `dim_customers`, `dim_products`, `fct_orders`, `fct_order_items` |

**A real example — choosing a grain.** `fct_order_items.sql` is where the
"product detail" decision actually gets made. Staging just cleans the
line-item rows and computes `line_amount`:

```sql
-- models/staging/stg_order_items.sql
select
    order_item_id,
    order_id,
    product_id,
    quantity,
    unit_price,
    quantity * unit_price as line_amount
from {{ ref('raw_order_items') }}
```

The mart then joins in `order_date` from `stg_orders` — denormalizing it onto
the line-item grain specifically so this table can carry its own time
dimension for MetricFlow, independent of `fct_orders`':

```sql
-- models/marts/fct_order_items.sql
select
    oi.order_item_id,
    oi.order_id,
    oi.product_id,
    o.order_date,
    oi.quantity,
    oi.unit_price,
    oi.line_amount
from {{ ref('stg_order_items') }} oi
left join {{ ref('stg_orders') }} o on oi.order_id = o.order_id
```

> **The only thing production changes here: `ref()` vs `source()`.** Staging
> models in this POC read seed tables via `{{ ref('raw_order_items') }}`. In
> production, an ingestion tool (Fivetran/Airbyte/CDC/streaming) lands raw
> data instead, staging reads it via `{{ source('raw', 'order_items') }}`,
> and every model downstream — marts, the semantic layer, the catalog,
> search — is untouched.

### The full cycle

Transformation is the hinge between two phases of the same loop: data is
built once, offline, and then a search query walks back through it, live,
every time someone asks a question.

**Build time** — runs once, whenever data changes:
1. **Raw data** — seed CSVs here; a real ingestion tool in production.
2. **Staging** — clean, typed, 1:1 views (`stg_orders`, `stg_order_items`, ...).
3. **Marts** — joined, grained fact/dim tables, materialized into DuckDB (`fct_orders`, `fct_order_items`, `dim_customers`, `dim_products`).
4. **Semantic layer** — MetricFlow compiles `semantic_models.yml` + `metrics.yml` against those marts into 6 queryable metrics.

**Runtime** — every time someone searches:

5. **Question** — `"revenue by product category"`, typed into the CLI or the web UI.
6. **Catalog + search index** — the question is embedded and ranked against every metric, dimension, and table — the winner is `product_revenue`.
7. **MetricFlow query** — `mf query --metrics product_revenue --group-by product__category`, compiled to SQL by the same semantic layer stage 4 built.
8. **Result — closes the loop.** That SQL runs against the exact `fct_order_items` table Transformation (stage 3) materialized — the runtime query lands back in the data Transformation built, every time.

## How the catalog + search works

1. **`scripts/catalog.py`** builds three kinds of searchable items purely
   from dbt's own compiled output:
   - **metrics** — name/description/type from `semantic_manifest.json`,
     available dimensions from `mf list metrics --show-all-dimensions`
   - **dimensions** — every qualified dimension path (e.g. `customer__region`,
     `product__category`) collapsed to one entry per leaf name, listing every
     metric it can slice
   - **tables/columns** — documented mart models and their columns, from
     `manifest.json` (undocumented staging views are skipped — nothing
     useful to search on)
2. **`scripts/search_index.py`** embeds every item's blurb with a local
   sentence-transformer (`all-MiniLM-L6-v2`) once at startup, and ranks a
   query against all of them by cosine similarity — one index, all three
   item types compete for the same ranked list.
3. **Dimension + date resolution** turns the winning metric plus the rest
   of the query into `group_by`/`start_date`/`end_date` one of two ways —
   see **LLM query interpretation**, below, for why these are split rather
   than one growing pile of special cases:
   - `catalog.guess_group_by` — local keyword matching against the metric's
     own dimensions, plus a time-*granularity* detector (`monthly` →
     `metric_time__month`). Always available, no network call, but can't
     express a time *range* like "last month."
   - `llm_interpreter.interpret` — opt-in (`OPENAI_API_KEY`), a structured
     OpenAI call that resolves real dates and picks dimensions, validated
     against the metric's real dimension list after the call.
4. **Execution** shells out to `mf query --metrics ... --group-by ...
   [--start-time ... --end-time ...]` (CLI: human-readable table via
   stdout; web UI: `--csv` to a temp file, parsed into JSON for the
   browser).

## LLM query interpretation (opt-in)

`"how many things I have sold in the last month"` exposes the limit of pure
keyword matching: the embedding search correctly finds `total_items_sold`
despite zero word overlap with its description (genuine semantic retrieval),
but `guess_group_by` only recognizes the word "month" and groups by month
instead of *filtering* to the most recent one — you get every month back and
have to eyeball the last row.

The fix isn't more keyword special cases — it's separating two different
jobs that were conflated in one heuristic function:

1. **Retrieval** — which metric matches? Embeddings already do this well;
   `llm_interpreter.py` doesn't touch it.
2. **Compilation** — turn the rest of the sentence into a structured query.
   This is the part `llm_interpreter.interpret()` replaces: it calls OpenAI
   (`gpt-4o-mini` by default, `OPENAI_MODEL` to override) with **structured
   output** (`response_format: json_schema`, strict mode) — not free text to
   parse — constrained to the top few metric candidates from the embedding
   search, each with its own real `available_dimensions`. The model is told
   today's date explicitly, so "last month" / "this year" / "past 7 days"
   resolve to concrete ISO dates instead of a granularity guess. Every
   dimension it returns — for `group_by` *and* for `filters` (below) — is
   then re-validated against that metric's real dimension list before it
   ever reaches MetricFlow — defense in depth even though the prompt
   already constrains it, since a hallucinated dimension name would
   otherwise fail (or worse, silently misquery) downstream.

### Filters — restricting to one named value

`group_by` breaks a metric down by *every* value of a dimension; it has no
way to restrict to *one* value someone actually named — "how was my
**shampoo** sales," "revenue from the **West**," "**cancelled** orders."
Without a filter mechanism, a query like that either gets the request
silently dropped (returning a company-wide total mislabeled as the specific
answer) or nothing at all.

`llm_interpreter.py`'s schema has a `filters` field: a list of
`{dimension, value}` pairs, applied via MetricFlow's `--where
"{{ Dimension('...') }} ILIKE '...'"`. Matching is **always case-insensitive
with SQL wildcards** (`ILIKE`, not `=`) — the model is instructed to wrap a
casual/partial mention in `%...%` (`"shampoo"` → `'%shampoo%'`) since it
rarely knows a dimension's exact stored spelling (the real value is
`"Lavender Shampoo"`), and a silently-empty exact match is worse than a
fuzzy one. `filter_to_where_clause()` also escapes stray single quotes in
the value SQL-style before it's interpolated into the `--where` string.

**Verified**: `"How was my shampoo sales last month"` was returning **114**
— the *entire store's* item count for the period, because there was no way
to apply "shampoo" as a constraint at all. With filters wired in, the same
query now correctly returns **25** (confirmed independently against the
raw per-product breakdown: `6+25+13+20+19+15+13+3 = 114` — the old number
*was* the unfiltered total, hiding in plain sight).

```bash
python scripts/semantic_search.py "How was my shampoo sales last month" --run
#   Filters: product__product_name ILIKE '%shampoo%'
#   mf query --metrics items_sold --start-time ... --end-time ... \
#     --where {{ Dimension('product__product_name') }} ILIKE '%shampoo%'
#
#     items_sold
#   ------------
#             25
```

The keyword heuristic never gets filters — guessing a *value* (not just a
dimension name) from keywords alone is too easy to get wrong silently, so
`catalog.guess_group_by`'s fallback path stays filter-less by design; a
query needing a filter just returns an unfiltered aggregate in heuristic
mode instead of a wrong-looking-right one in LLM mode with no filters.

This is opt-in and degrades gracefully: `llm_interpreter.available()` checks
for the env var, and both the CLI and the web app fall back to
`catalog.guess_group_by` (no date range, but no API key/network/cost either)
if it's unset or the call fails. Enabling it changes a real property of this
project — it stops being zero-external-dependency — which is why it's a
flag/env var, not the default.

### Verified against a live key

```bash
export OPENAI_API_KEY=sk-...
python scripts/semantic_search.py "how many things I have sold in the last month" --run
#   Mode: llm
#   Group-by dimensions: (none)
#   Time range: 2026-07-18 to 2026-08-18
#
#     total_items_sold
#   -------------------
#                   123
```

This is the exact query that motivated building this feature in the first
place — the keyword heuristic could only *group by* month, never *filter*
to the most recent one. Here it resolves an exact calendar range from
today's real date and returns a real, populated count.

Two things live testing surfaced along the way, both since fixed:

- **A dataset-vintage gap.** The first time this ran, the range resolved
  correctly but came back empty — this POC's seed data was hardcoded to
  Jan–Aug 2025, so "last month" fell entirely outside it once run for
  real. Fixed by `scripts/generate_seed_data.py` (see **How seeds load**,
  above): order dates are now generated relative to *today*, so this stays
  meaningful whenever you regenerate. The interpretation was never the
  problem — the fixture data just wasn't dated relative to "now."
- **An over-eager first pass.** The original prompt let the model attach a
  `group_by` dimension it merely thought was relevant, even when the query
  never asked for a breakdown (`"how much revenue did we make in July
  2025"` initially came back grouped by region, unprompted). The system
  prompt now explicitly requires breakdown language ("by region," "per
  category") before including a dimension — confirmed fixed, without
  breaking queries that *do* ask for a breakdown (`"revenue by region"`
  still correctly returns `group_by: ['customer__region']`).

## Known simplifications

- Dimension collapsing by leaf name means `order_date` (order grain) and
  `metric_time`/`time` (MetricFlow's synthetic time alias) show up as two
  separate catalog entries even though they largely overlap conceptually. A
  production version would normalize time dimensions into one entry.
- Without `OPENAI_API_KEY`, group-by inference is keyword matching — it
  won't catch paraphrases like "by area" for "region," and can't express a
  time range at all (see **LLM query interpretation**, above).
- `dbt seed` on DuckDB doesn't auto-replace a table's *schema* on a plain
  reseed if columns changed — use `dbt seed --full-refresh` after editing a
  seed CSV's columns.
- Seed dates are anchored to today only as of whenever `generate_seed_data.py`
  was last run, not continuously — leave it long enough and "last month"
  goes stale again until you rerun it (see **How seeds load**, above). A
  real lakehouse doesn't have this problem since data keeps arriving.
- The model has no visibility into a categorical dimension's *actual*
  values, only its name — so it can pick a plausible-sounding but wrong
  dimension/value pair. `"items sold for perfume products"` filtered
  `product__category ILIKE '%perfume%'` and silently returned nothing,
  because the real category value is `"Fragrance"` (the word "perfume"
  only appears in *product names*, not the category). The dimension chosen
  was valid — it just wasn't the one that actually contained a matching
  value. A production fix would surface each categorical dimension's
  distinct values into the prompt (or the catalog itself), so the model
  can ground its choice in real vocabulary instead of guessing from the
  dimension's name.

## Extending toward a real system

- **Better retrieval**: swap the brute-force cosine loop for a real vector
  index (`lancedb`, `chroma`, `pgvector`) once the catalog has hundreds of
  items, not ~17.
- **Let the LLM see more than metrics**: `llm_interpreter.py` today only
  receives the top metric candidates. Passing dimension/table candidates
  too would let it handle mixed asks like "what's revenue, and what can I
  slice it by" in one call instead of two separate searches.
- **Richer catalog metadata**: `metrics.yml`/`semantic_models.yml` support
  `label` and `meta` — use them to attach synonyms ("revenue" ≈ "sales",
  "GMV") that get folded into the embedding text.
- **Swap the lakehouse**: change `profiles.yml` to `type: snowflake` /
  `databricks` / `bigquery` — nothing in `models/semantic/` needs to change,
  since MetricFlow is engine-agnostic.
- **Serve it for real**: put `scripts/app.py` behind a production WSGI
  server (gunicorn/uvicorn), and once you're on dbt Cloud / a hosted
  Semantic Layer, call its GraphQL API instead of the local `mf` CLI so
  multiple consumers (Slack bot, BI tool, this web UI) share one deployment.

## Troubleshooting

- **"time spine model" parse error** — MetricFlow requires a day-granularity
  date-spine model (`models/marts/metricflow_time_spine.sql`, declared with
  `time_spine:` config in `marts.yml`). Already included.
- **`mf` not found** — activate the venv (`source .venv/bin/activate`); it's
  installed by the `dbt-metricflow` package.
- **Manifest/catalog errors ("Compiled manifests not found")** — run
  `dbt parse` (or `dbt build`) before the CLI or web app; both read
  `target/manifest.json` and `target/semantic_manifest.json`.
- **Seed schema didn't change after editing a CSV's columns** — run
  `dbt seed --full-refresh`.
- **Spurious `RuntimeWarning: overflow/invalid value in matmul`** on Apple
  Silicon — a known cosmetic quirk of numpy's Accelerate BLAS backend on
  well-formed unit-norm float32 arrays; `search_index.py` suppresses it with
  `np.errstate` since the embeddings are verified finite.
