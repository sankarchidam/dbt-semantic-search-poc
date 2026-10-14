#!/usr/bin/env python3
"""
Generate native Unity Catalog Metric Views from this project's own
models/semantic/semantic_models.yml + metrics.yml -- so Genie/DBSQL
dashboards can discover the same metrics UC-natively, without MetricFlow
(`mf`) in the loop. One metric view per *measure-bearing* semantic model
(orders, order_items, payments here) -- dimension-only models
(customers, products) never get their own view; they're join targets
inside the three above, the same role they play in MetricFlow's own entity
graph.

Why generate instead of hand-write: these 9 metrics' real definitions
already live in metrics.yml/semantic_models.yml. A second, hand-maintained
copy in Databricks' own YAML would drift the first time someone edited one
and not the other -- re-running this script after any change is the same
discipline `scripts/catalog.py` already applies to the search catalog
(built from dbt's own compiled artifacts, never hand-maintained metadata).

Entity-graph traversal mirrors MetricFlow's own rule: a foreign entity on
one semantic model joins to whichever semantic model declares that same
entity *name* as primary -- not by guessing a foreign-key column name. This
generator ports that graph to Unity Catalog's own join syntax exactly as
confirmed against a live Databricks workspace (see README "Unity Catalog
Metric Views"):

  - a join's `on:` references the immediately-enclosing alias on the left
    ("source" at the top level, or the parent join's own name one level
    down) and the new join's own alias on the right
  - a *nested* join's dimensions are reached through a dotted path, e.g.
    `order.customer.region` for a 2-hop payments -> order -> customer
    traversal (`joins: [...]` nested inside a join entry, not a second
    top-level join)
  - a ratio metric (average_order_value) becomes one measure expression
    combining both aggregates directly: `SUM(x) / COUNT(y)` -- Unity
    Catalog has no separate "derived from two named measures" mechanism
  - a metric-level filter (payments_collected, failed_payments) becomes an
    inline `FILTER (WHERE ...)` clause on the measure's own expr, not a
    separate field -- MetricFlow's `{{ Dimension('payment__status') }} = 'x'`
    Jinja is translated to the plain column it resolves to (only handles a
    same-semantic-model dimension reference, which is all this project's
    metrics.yml currently has)
  - dimensions is the key that actually works (the create-tutorial example
    and this project's own live test both used `dimensions:`); Databricks'
    YAML reference page separately documents `fields:` as the current name
    for the same concept -- if a future Databricks Runtime stops accepting
    `dimensions:`, that's the one line to change

Usage:
    python scripts/generate_uc_metric_views.py              # print the generated DDL (dry run)
    python scripts/generate_uc_metric_views.py --apply       # also execute it against Databricks
    python scripts/generate_uc_metric_views.py --apply --catalog foo --schema bar
"""
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SEMANTIC_MODELS_PATH = PROJECT_ROOT / "models" / "semantic" / "semantic_models.yml"
METRICS_PATH = PROJECT_ROOT / "models" / "semantic" / "metrics.yml"


def _load_dotenv() -> None:
    """Same minimal loader as llm_interpreter.py -- see its docstring."""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _ref_to_table_name(model_ref: str) -> str:
    """"ref('fct_orders')" -> "fct_orders" -- the only Jinja this project's
    semantic_models.yml ever uses for `model:`."""
    match = re.match(r"ref\('([\w_]+)'\)", model_ref)
    if not match:
        raise ValueError(f"Unsupported model ref syntax: {model_ref!r}")
    return match.group(1)


def load_semantic_layer() -> tuple[dict[str, dict], list[dict]]:
    semantic_models = yaml.safe_load(SEMANTIC_MODELS_PATH.read_text())["semantic_models"]
    metrics = yaml.safe_load(METRICS_PATH.read_text())["metrics"]
    models_by_name = {m["name"]: m for m in semantic_models}
    return models_by_name, metrics


def _entity_expr(model: dict, entity_name: str) -> str:
    for e in model["entities"]:
        if e["name"] == entity_name:
            return e["expr"]
    raise ValueError(f"No entity {entity_name!r} on semantic model {model['name']!r}")


def _primary_model_for_entity(models_by_name: dict[str, dict], entity_name: str, exclude: str) -> dict | None:
    """MetricFlow's own join rule: a foreign entity joins to whichever OTHER
    semantic model declares that same entity name as primary."""
    for name, model in models_by_name.items():
        if name == exclude:
            continue
        for e in model["entities"]:
            if e["name"] == entity_name and e["type"] == "primary":
                return model
    return None


def build_joins(models_by_name: dict[str, dict], model: dict, visited: set[str]) -> list[dict]:
    """One join entry per foreign entity on `model`, each recursively
    carrying its own nested joins (depth-first) -- the same traversal
    MetricFlow does to build a qualified dimension path like
    order__customer__region, translated into UC's join-entry nesting."""
    joins = []
    for entity in model["entities"]:
        if entity["type"] != "foreign":
            continue
        target = _primary_model_for_entity(models_by_name, entity["name"], exclude=model["name"])
        if target is None or target["name"] in visited:
            continue  # no model declares it primary (shouldn't happen here), or already joined upstream
        alias = entity["name"]
        joins.append({
            "name": alias,
            "model_name": target["name"],
            "source": _ref_to_table_name(target["model"]),  # resolved to catalog.schema.<table> at emit time
            "on": f"{{left}}.{entity['expr']} = {alias}.{_entity_expr(target, entity['name'])}",
            "joins": build_joins(models_by_name, target, visited | {target["name"], model["name"]}),
        })
    return joins


def _resolve_on(join: dict, left_alias: str) -> str:
    return join["on"].format(left=left_alias)


def collect_dimensions(model: dict, joins: list[dict], path_prefix: str = "") -> list[dict]:
    """Own dimensions (bare name) plus every reachable joined model's
    dimensions (name prefixed by the join alias path when it collides with
    a dimension already seen, e.g. payments' own `status` vs. the order
    join's `status` -> emitted as `order_status`)."""
    dims = [{"name": d["name"], "expr": d["name"]} for d in model.get("dimensions", [])]
    seen = {d["name"] for d in dims}

    def walk(join: dict, alias_path: str):
        target = _MODELS_BY_NAME[join["model_name"]]
        for d in target.get("dimensions", []):
            leaf = d["name"]
            dim_name = leaf if leaf not in seen else f"{join['name']}_{leaf}"
            seen.add(dim_name)
            dims.append({"name": dim_name, "expr": f"{alias_path}.{leaf}"})
        for nested in join["joins"]:
            walk(nested, f"{alias_path}.{nested['name']}")

    for j in joins:
        walk(j, j["name"])

    return dims


# Set by generate_all() before collect_dimensions() needs it -- avoids
# threading models_by_name through every recursive call above.
_MODELS_BY_NAME: dict[str, dict] = {}


def _translate_filter(filter_jinja: str, model: dict) -> str:
    """"{{ Dimension('payment__status') }} = 'completed'" -> "status = 'completed'".
    Only handles a dimension on the metric's own semantic model -- the only
    kind this project's metrics.yml currently uses."""
    def replace(match: re.Match) -> str:
        qualified = match.group(1)
        leaf = qualified.split("__")[-1]
        return _entity_expr(model, leaf) if any(e["name"] == leaf for e in model["entities"]) else leaf
    return re.sub(r"\{\{\s*Dimension\('([\w_]+)'\)\s*\}\}", replace, filter_jinja).strip()


def _measure_expr(model: dict, measure_name: str) -> tuple[str, str]:
    """Returns (agg, column_expr) for a named measure on `model`."""
    for m in model["measures"]:
        if m["name"] == measure_name:
            return m["agg"], m["expr"]
    raise ValueError(f"No measure {measure_name!r} on semantic model {model['name']!r}")


def collect_measures(model_name: str, model: dict, metrics: list[dict]) -> list[dict]:
    """Every metric in metrics.yml whose measure(s) live on this semantic
    model, as one UC measure entry each. A ratio metric's numerator/
    denominator are themselves simple metrics defined elsewhere in
    metrics.yml -- resolved recursively, one level, which is as deep as
    this project's metrics.yml ever nests."""
    simple_by_name = {m["name"]: m for m in metrics if m["type"] == "simple"}

    def owns(simple_metric: dict) -> bool:
        try:
            _measure_expr(model, simple_metric["type_params"]["measure"])
            return True
        except ValueError:
            return False

    def simple_expr(simple_metric: dict) -> str:
        agg, col = _measure_expr(model, simple_metric["type_params"]["measure"])
        expr = f"{agg.upper()}({col})"
        if simple_metric.get("filter"):
            expr += f" FILTER (WHERE {_translate_filter(simple_metric['filter'], model)})"
        return expr

    out = []
    for metric in metrics:
        if metric["type"] == "simple" and owns(metric):
            out.append({"name": metric["name"], "expr": simple_expr(metric), "comment": metric.get("description", "")})
        elif metric["type"] == "ratio":
            num = simple_by_name[metric["type_params"]["numerator"]]
            den = simple_by_name[metric["type_params"]["denominator"]]
            if owns(num) and owns(den):
                out.append({
                    "name": metric["name"],
                    "expr": f"{simple_expr(num)} / {simple_expr(den)}",
                    "comment": metric.get("description", ""),
                })
    return out


def _emit_joins_yaml(joins: list[dict], left_alias: str, indent: str, catalog: str, schema: str) -> str:
    lines = []
    for j in joins:
        lines.append(f"{indent}- name: {j['name']}")
        lines.append(f"{indent}  source: {catalog}.{schema}.{j['source']}")
        lines.append(f"{indent}  on: {_resolve_on(j, left_alias)}")
        if j["joins"]:
            lines.append(f"{indent}  joins:")
            lines.append(_emit_joins_yaml(j["joins"], j["name"], indent + "    ", catalog, schema))
    return "\n".join(lines)


def generate_view_ddl(model_name: str, catalog: str, schema: str) -> str:
    model = _MODELS_BY_NAME[model_name]
    joins = build_joins(_MODELS_BY_NAME, model, visited={model_name})
    dimensions = collect_dimensions(model, joins)
    measures = collect_measures(model_name, model, _METRICS)

    table = _ref_to_table_name(model["model"])
    view_name = f"mv_{model_name}"

    yaml_lines = [
        "version: 1.1",
        f"comment: \"Generated from models/semantic/semantic_models.yml + metrics.yml -- {model.get('description', '').strip()}\"",
        f"source: {catalog}.{schema}.{table}",
    ]
    if joins:
        yaml_lines.append("joins:")
        yaml_lines.append(_emit_joins_yaml(joins, "source", "  ", catalog, schema))
    yaml_lines.append("dimensions:")
    for d in dimensions:
        yaml_lines.append(f"  - name: {d['name']}")
        yaml_lines.append(f"    expr: {d['expr']}")
    yaml_lines.append("measures:")
    for m in measures:
        yaml_lines.append(f"  - name: {m['name']}")
        yaml_lines.append(f"    expr: {m['expr']}")
        if m["comment"]:
            yaml_lines.append(f"    comment: \"{m['comment']}\"")

    yaml_body = "\n".join(yaml_lines)
    return f"CREATE OR REPLACE VIEW {catalog}.{schema}.{view_name}\nWITH METRICS\nLANGUAGE YAML\nAS $$\n{yaml_body}\n$$"


_METRICS: list[dict] = []


def generate_all(catalog: str, schema: str) -> dict[str, str]:
    global _MODELS_BY_NAME, _METRICS
    _MODELS_BY_NAME, _METRICS = load_semantic_layer()

    measure_bearing = [name for name, m in _MODELS_BY_NAME.items() if m.get("measures")]
    return {name: generate_view_ddl(name, catalog, schema) for name in measure_bearing}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Execute the generated DDL against Databricks (default: print only)")
    parser.add_argument("--catalog", default=os.environ.get("DATABRICKS_CATALOG", "semantic_search_poc"))
    parser.add_argument("--schema", default=os.environ.get("DATABRICKS_SCHEMA", "dev"))
    args = parser.parse_args()

    ddls = generate_all(args.catalog, args.schema)

    for name, ddl in ddls.items():
        print(f"\n{'=' * 70}\n-- mv_{name}\n{'=' * 70}")
        print(ddl)
        print()

    if args.apply:
        from databricks import sql

        conn = sql.connect(
            server_hostname=os.environ["DATABRICKS_HOST"],
            http_path=os.environ["DATABRICKS_HTTP_PATH"],
            access_token=os.environ["DATABRICKS_TOKEN"],
        )
        cur = conn.cursor()
        for name, ddl in ddls.items():
            print(f"Creating mv_{name}...")
            cur.execute(ddl)
            print(f"  OK")
        cur.close()
        conn.close()
    else:
        print("(pass --apply to create these views on Databricks)")


if __name__ == "__main__":
    main()
