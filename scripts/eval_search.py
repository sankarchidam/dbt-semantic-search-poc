#!/usr/bin/env python3
"""
Eval harness: runs every golden query in eval/golden_queries.json through the
real pipeline -- search_index.py's embedding ranking, then
semantic_search.resolve_query_params's two resolution paths -- and prints
accuracy tables.

Three things get graded, separately, because they test different layers:

  1. Search ranking (search_index.SemanticSearchIndex.search) -- did the
     expected catalog item come back top-1, and did it come back at all in
     the top-k candidates an LLM call would even get to choose among. This
     is what the README's disambiguation examples ("search 'units sold by
     product' and items_sold wins") are informally demonstrating; this
     harness scores all of them, not just the hand-picked ones.

  2. Heuristic resolution (catalog.guess_group_by) -- always runs, no API
     key needed.

  3. LLM resolution (llm_interpreter.interpret) -- only runs, and is only
     graded, when OPENAI_API_KEY is set. Scored on metric choice, group_by,
     filters, and resolved date ranges -- the things the heuristic can't do
     at all (see llm_interpreter.py's docstring), so this is the harness
     that actually tells you whether that extra capability works.

Grading is deliberately NOT LLM-as-judge -- same reasoning as
triagegraph/scripts/eval_scenarios.py: judging an LLM pipeline with another
LLM call just stacks a second source of non-determinism on the first.
Instead every fixture in golden_queries.json carries its expected
metric/dimensions/filters/time-range, written before ever running the
pipeline, and grading is exact-match against those fields -- simple,
deterministic, and auditable straight from the printed tables.

Usage:
    python scripts/eval_search.py
    python scripts/eval_search.py --top-k 3   # stricter "did search even surface it" bar
"""
from __future__ import annotations

import argparse
import datetime
import json
from pathlib import Path

import llm_interpreter
from search_index import SemanticSearchIndex
from semantic_search import resolve_query_params

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_PATH = PROJECT_ROOT / "eval" / "golden_queries.json"


def load_fixtures() -> list[dict]:
    return json.loads(FIXTURES_PATH.read_text())


def normalize_group_by(dims: list[str]) -> set[str]:
    """Collapse metric_time__<granularity> down to bare metric_time -- which
    granularity (day vs month) got picked isn't what this harness tests."""
    return {d.split("__")[0] if d.startswith("metric_time") else d for d in dims}


def expected_time_range(label: str, today: datetime.date) -> tuple[str, str]:
    if label == "last_month":
        first_of_this_month = today.replace(day=1)
        last_month_end = first_of_this_month - datetime.timedelta(days=1)
        return last_month_end.replace(day=1).isoformat(), last_month_end.isoformat()
    if label == "ytd":
        return today.replace(month=1, day=1).isoformat(), today.isoformat()
    raise ValueError(f"no resolver for expected_relative_time={label!r}")


def grade_search(fixture: dict, ranked: list, top_k: int) -> dict:
    def matches(item: dict) -> bool:
        return item["type"] == fixture["expected_type"] and item["name"] == fixture["expected_name"]

    return {
        "top1": matches(ranked[0][1]),
        "topk": any(matches(item) for _, item in ranked[:top_k]),
    }


def grade_resolution(
    fixture: dict, metric: dict, group_by: list, start_date, end_date, filters: list, today: datetime.date
) -> dict:
    result = {"metric": metric["name"] == fixture["expected_name"]}

    result["group_by"] = normalize_group_by(group_by) == normalize_group_by(fixture.get("expected_group_by", []))

    expected_filters = fixture.get("expected_filters")
    if expected_filters:
        result["filters"] = all(
            any(f["dimension"] == ef["dimension"] and ef["contains"].lower() in f["value"].lower() for f in filters)
            for ef in expected_filters
        )
    else:
        result["filters"] = len(filters) == 0

    expected_time = fixture.get("expected_relative_time")
    if expected_time:
        want_start, want_end = expected_time_range(expected_time, today)
        result["time"] = start_date == want_start and end_date == want_end
    else:
        result["time"] = start_date is None and end_date is None

    return result


def _print_table(title: str, rows: list[dict], columns: list[tuple[str, str]]):
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")
    if not rows:
        print("(no fixtures graded here)")
        return
    q_width = max(len("query"), *(len(r["query"]) for r in rows)) + 2
    header = f"{'query':<{q_width}}" + "".join(f"{label:<9}" for _, label in columns)
    print(header)
    print("-" * len(header))
    totals = {key: 0 for key, _ in columns}
    for r in rows:
        line = f"{r['query']:<{q_width}}"
        for key, _ in columns:
            ok = r[key]
            totals[key] += int(ok)
            line += f"{'ok' if ok else 'FAIL':<9}"
        if r.get("note"):
            line += f"  ({r['note']})"
        print(line)
    print("-" * len(header))
    n = len(rows)
    print("  ".join(f"{label} {totals[key]}/{n}" for key, label in columns))


def run(top_k: int):
    fixtures = load_fixtures()
    print("Building catalog + loading embedding model...")
    index = SemanticSearchIndex()
    today = datetime.date.today()
    llm_on = llm_interpreter.available()
    print(f"  indexed {len(index.catalog)} items, {len(fixtures)} golden queries")
    print(f"  LLM interpretation: {'on' if llm_on else 'off (set OPENAI_API_KEY to also grade the LLM pipeline)'}")

    search_rows, heuristic_rows, llm_rows = [], [], []

    for fixture in fixtures:
        query = fixture["query"]
        ranked = index.search(query, top_k=max(top_k, 8))
        search_rows.append({"query": query, **grade_search(fixture, ranked, top_k)})

        best = ranked[0][1]
        if best["type"] != "metric" or fixture["expected_type"] != "metric":
            continue  # dimension/table fixtures: the search table above is the whole story

        metric, group_by, start_date, end_date, filters, _ = resolve_query_params(query, ranked, best, use_llm=False)
        heuristic_rows.append(
            {"query": query, **grade_resolution(fixture, metric, group_by, start_date, end_date, filters, today)}
        )

        if llm_on:
            metric, group_by, start_date, end_date, filters, mode = resolve_query_params(
                query, ranked, best, use_llm=True
            )
            if mode != "llm":
                # llm_interpreter raised and resolve_query_params silently fell back --
                # that's a real failure to surface, not something to grade as heuristic.
                llm_rows.append(
                    {"query": query, "metric": False, "group_by": False, "filters": False, "time": False,
                     "note": "LLM call failed, fell back to heuristic"}
                )
            else:
                llm_rows.append(
                    {"query": query, **grade_resolution(fixture, metric, group_by, start_date, end_date, filters, today)}
                )

    _print_table("Search ranking (embedding similarity)", search_rows, [("top1", "top1"), ("topk", f"top{top_k}")])
    _print_table(
        "Heuristic resolution (catalog.guess_group_by -- always runs, no API key)",
        heuristic_rows,
        [("metric", "metric"), ("group_by", "group_by"), ("filters", "filters"), ("time", "time")],
    )
    if llm_on:
        _print_table(
            "LLM resolution (llm_interpreter.interpret)",
            llm_rows,
            [("metric", "metric"), ("group_by", "group_by"), ("filters", "filters"), ("time", "time")],
        )
    else:
        print("\n(skipped LLM resolution grading -- set OPENAI_API_KEY to include it)")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--top-k", type=int, default=5,
        help="How many ranked results count as 'surfaced' for the top-k search check (default 5, matching semantic_search.py's default)",
    )
    args = parser.parse_args()
    run(args.top_k)


if __name__ == "__main__":
    main()
