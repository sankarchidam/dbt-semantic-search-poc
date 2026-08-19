#!/usr/bin/env python3
"""
Natural-language search over this mini lakehouse's dbt Semantic Layer.

Searches across everything scripts/catalog.py indexes -- metrics,
dimensions, and mart tables/columns -- and, for a metric result, can run
it live against DuckDB via MetricFlow.

Two ways to turn the query into group-by dimensions + a time range:
  - LLM interpretation (scripts/llm_interpreter.py), used automatically
    when OPENAI_API_KEY is set -- understands real dates ("last month")
    and paraphrased dimensions, constrained to each metric's actual
    available dimensions.
  - catalog.guess_group_by, a local keyword heuristic used as a fallback
    (or always, with --no-llm) -- no API key or network call needed, but
    can only pick a time *granularity*, never a time *range*.

Usage:
    python scripts/semantic_search.py "monthly revenue by region"
    python scripts/semantic_search.py "how many items sold last month" --run
    python scripts/semantic_search.py "revenue by product category" --run --no-llm
"""
import argparse
import datetime
import os
import subprocess
import sys

import llm_interpreter
from catalog import PROJECT_ROOT, guess_group_by
from search_index import SemanticSearchIndex

TYPE_LABELS = {"metric": "METRIC", "dimension": "DIMENSION", "model": "TABLE"}


def resolve_query_params(query: str, ranked: list, best: dict, use_llm: bool):
    """Returns (metric_item, group_by, start_date, end_date, filters, mode_note)."""
    if use_llm and llm_interpreter.available():
        metric_candidates = [item for _, item in ranked if item["type"] == "metric"][:4]
        try:
            result = llm_interpreter.interpret(query, metric_candidates, today=datetime.date.today())
            chosen = next(c for c in metric_candidates if c["name"] == result["metric"])
            return chosen, result["group_by"], result["start_date"], result["end_date"], result["filters"], "llm"
        except Exception as e:
            print(f"  (LLM interpretation failed ({e}) -- falling back to keyword heuristic)")

    return best, guess_group_by(query, best["dimensions"]), None, None, [], "heuristic"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("query", help="Natural-language search, e.g. 'how many items sold last month'")
    parser.add_argument("--top-k", type=int, default=5, help="Number of candidates to show (default 5)")
    parser.add_argument("--run", action="store_true", help="If the best match is a metric, execute it via MetricFlow")
    parser.add_argument("--no-llm", action="store_true", help="Skip LLM interpretation even if OPENAI_API_KEY is set")
    args = parser.parse_args()

    print("Building catalog + loading embedding model...")
    index = SemanticSearchIndex()
    by_type = {}
    for item in index.catalog:
        by_type.setdefault(item["type"], 0)
        by_type[item["type"]] += 1
    print(f"  indexed {len(index.catalog)} items: " + ", ".join(f"{n} {t}" for t, n in by_type.items()))
    print(f"  LLM interpretation: {'on (OPENAI_API_KEY set)' if llm_interpreter.available() and not args.no_llm else 'off (using keyword heuristic)'}")

    ranked = index.search(args.query, top_k=args.top_k)

    print(f'\nQuery: "{args.query}"\n')
    print(f"Top {args.top_k} matches:")
    for score, item in ranked:
        label = TYPE_LABELS[item["type"]]
        print(f"  [{score:.3f}] {label:<10} {item['name']} — {item['description']}")

    best_score, best = ranked[0]
    print(f"\nBest match: {best['name']}  ({TYPE_LABELS[best['type']]}, similarity {best_score:.3f})")

    if best["type"] != "metric":
        print(
            f"\nThis is a {best['type']}, not a metric, so there's nothing to run directly."
        )
        if best["type"] == "dimension":
            print(f"It can slice these metrics: {', '.join(best['metrics'])}")
        elif best["type"] == "model":
            print(f"Columns: {', '.join(best['columns'])}")
        return

    metric, group_by, start_date, end_date, filters, mode = resolve_query_params(
        args.query, ranked, best, use_llm=not args.no_llm
    )
    if mode == "llm" and metric["name"] != best["name"]:
        print(f"(LLM picked {metric['name']} over the top embedding match {best['name']})")

    cmd = ["mf", "query", "--metrics", metric["name"]]
    if group_by:
        cmd += ["--group-by", ",".join(group_by)]
        time_dims = [d for d in group_by if d.startswith("metric_time")]
        if time_dims:
            cmd += ["--order", time_dims[0]]
    if start_date:
        cmd += ["--start-time", start_date]
    if end_date:
        cmd += ["--end-time", end_date]
    for f in filters:
        cmd += ["--where", llm_interpreter.filter_to_where_clause(f)]

    print(f"Mode: {mode}")
    print(f"Group-by dimensions: {group_by or '(none)'}")
    if start_date or end_date:
        print(f"Time range: {start_date or '(open)'} to {end_date or '(open)'}")
    if filters:
        filter_descriptions = [f"{f['dimension']} ILIKE '{f['value']}'" for f in filters]
        print(f"Filters: {', '.join(filter_descriptions)}")
    print(f"Equivalent MetricFlow query:\n  {' '.join(cmd)}")

    if args.run:
        print("\nRunning query against DuckDB via MetricFlow...\n")
        result = subprocess.run(cmd, cwd=PROJECT_ROOT, env={"DBT_PROFILES_DIR": ".", **os.environ})
        sys.exit(result.returncode)
    else:
        print("\n(pass --run to execute this query and print results)")


if __name__ == "__main__":
    main()
