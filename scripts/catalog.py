"""
Build a unified, embeddable search catalog for this mini lakehouse.

This is the "built-in semantic search" index: one place that knows about
everything a person might search for --

  - metrics       (from the dbt Semantic Layer: metrics.yml)
  - dimensions    (how each metric can be sliced, derived from the same
                   semantic layer via `mf list metrics --show-all-dimensions`)
  - tables/columns (the underlying dbt mart models, from the compiled
                   manifest -- plain data-catalog discovery)

Both `scripts/semantic_search.py` (CLI) and `scripts/app.py` (web UI) import
`build_catalog()` from here so there is exactly one definition of what's
searchable, whichever interface someone uses.

Requires the project to have been compiled first: `DBT_PROFILES_DIR=. dbt parse`.
"""
import json
import os
import re
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = PROJECT_ROOT / "target" / "manifest.json"
SEMANTIC_MANIFEST_PATH = PROJECT_ROOT / "target" / "semantic_manifest.json"

# Plumbing model, not a business table -- not useful as a search result.
EXCLUDED_MODELS = {"metricflow_time_spine"}


def _run_mf_list_metrics() -> dict[str, list[str]]:
    """Parse `mf list metrics --show-all-dimensions` into {metric_name: [qualified_dims]}."""
    result = subprocess.run(
        ["mf", "list", "metrics", "--show-all-dimensions"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        env={"DBT_PROFILES_DIR": ".", **os.environ},
    )
    dims_by_metric: dict[str, list[str]] = {}
    line_re = re.compile(r"^[•\-]\s*(\w+):\s*(.+)$")
    for line in result.stdout.splitlines():
        match = line_re.match(line.strip())
        if match:
            name, dims_str = match.groups()
            dims_by_metric[name] = [d.strip() for d in dims_str.split(",")]
    if not dims_by_metric:
        raise RuntimeError(
            "Could not parse `mf list metrics` output. Raw output was:\n"
            + result.stdout + result.stderr
        )
    return dims_by_metric


def _metric_items(semantic_manifest: dict, dims_by_metric: dict) -> list[dict]:
    items = []
    for m in semantic_manifest["metrics"]:
        name = m["name"]
        dims = dims_by_metric.get(name, [])
        description = m.get("description") or ""
        text = (
            f"Metric: {name.replace('_', ' ')}. {description} "
            f"Breaks down by: {', '.join(d.replace('__', ' ') for d in dims)}."
        )
        items.append({
            "type": "metric",
            "id": f"metric:{name}",
            "name": name,
            "description": description,
            "metric_type": m["type"],
            "dimensions": dims,
            "search_text": text,
        })
    return items


def _dimension_items(dims_by_metric: dict) -> list[dict]:
    """Collapse per-metric qualified dimension paths (e.g. customer__region,
    order__customer__region) down to one searchable entry per leaf dimension
    name (region), listing every metric it can slice."""
    agg: dict[str, dict] = {}
    for metric, dims in dims_by_metric.items():
        for d in dims:
            leaf = "time" if d == "metric_time" else d.split("__")[-1]
            entry = agg.setdefault(leaf, {"qualified_paths": set(), "metrics": set()})
            entry["qualified_paths"].add(d)
            entry["metrics"].add(metric)

    items = []
    for leaf, info in agg.items():
        metrics = sorted(info["metrics"])
        description = f"Breaks down: {', '.join(metrics)}"
        text = f"Dimension: {leaf.replace('_', ' ')}. {description}."
        items.append({
            "type": "dimension",
            "id": f"dimension:{leaf}",
            "name": leaf,
            "description": description,
            "qualified_paths": sorted(info["qualified_paths"]),
            "metrics": metrics,
            "search_text": text,
        })
    return items


def _model_items(manifest: dict) -> list[dict]:
    project_name = manifest["metadata"]["project_name"]
    items = []
    for node in manifest["nodes"].values():
        if node.get("resource_type") != "model" or node.get("package_name") != project_name:
            continue
        name = node["name"]
        if name in EXCLUDED_MODELS or not node.get("description"):
            continue  # skip undocumented staging views -- nothing useful to search on
        columns = node.get("columns", {})
        col_text = "; ".join(
            f"{col_name}: {meta.get('description') or ''}".rstrip(": ")
            for col_name, meta in columns.items()
        )
        text = f"Table: {name.replace('_', ' ')}. {node['description']} Columns: {col_text}."
        items.append({
            "type": "model",
            "id": f"model:{name}",
            "name": name,
            "description": node["description"],
            "columns": list(columns.keys()),
            "search_text": text,
        })
    return items


def build_catalog() -> list[dict]:
    if not MANIFEST_PATH.exists() or not SEMANTIC_MANIFEST_PATH.exists():
        raise SystemExit(
            f"Compiled manifests not found under {PROJECT_ROOT / 'target'}.\n"
            f"Run `DBT_PROFILES_DIR=. dbt parse` from {PROJECT_ROOT} first."
        )
    manifest = json.loads(MANIFEST_PATH.read_text())
    semantic_manifest = json.loads(SEMANTIC_MANIFEST_PATH.read_text())
    dims_by_metric = _run_mf_list_metrics()

    catalog = []
    catalog += _metric_items(semantic_manifest, dims_by_metric)
    catalog += _dimension_items(dims_by_metric)
    catalog += _model_items(manifest)
    return catalog


TIME_GRANULARITY_WORDS = {
    "day": "day", "daily": "day",
    "week": "week", "weekly": "week",
    "month": "month", "monthly": "month",
    "quarter": "quarter", "quarterly": "quarter",
    "year": "year", "yearly": "year", "annual": "year", "annually": "year",
}

_TIME_WORDS = tuple(TIME_GRANULARITY_WORDS) + ("trend", "over time", "date")


def guess_group_by(query: str, dims: list[str]) -> list[str]:
    """Heuristic keyword match: pick a metric's dimensions whose name appears
    in the query, plus a time dimension at the granularity implied by the
    query (default: month) if the query sounds time-oriented."""
    query_lower = query.lower()
    matched = []
    for dim in dims:
        if dim == "metric_time":
            continue  # handled separately, with granularity
        leaf = dim.split("__")[-1]
        tokens = leaf.replace("_", " ").split()
        if all(tok in query_lower for tok in tokens) or leaf.rstrip("s") in query_lower:
            matched.append(dim)

    if "metric_time" in dims and any(w in query_lower for w in _TIME_WORDS):
        granularity = "month"
        for word, gran in TIME_GRANULARITY_WORDS.items():
            if word in query_lower:
                granularity = gran
                break
        matched.append(f"metric_time__{granularity}")

    return matched
