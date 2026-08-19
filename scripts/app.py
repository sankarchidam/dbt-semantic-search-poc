#!/usr/bin/env python3
"""
Mini lakehouse search UI.

A small local web app in front of the same catalog + embedding index the
CLI (semantic_search.py) uses: search across metrics/dimensions/tables,
then run the winning metric live against DuckDB via MetricFlow and see
results in the browser.

Two ways to turn a query into group-by dimensions + a time range -- see
llm_interpreter.py's docstring for why this split exists:
  - /api/search: instant, local, keyword-heuristic group-by guessing for
    every ranked result as you type (catalog.guess_group_by) -- no network
    call, but can't parse a time *range* like "last month", only a
    granularity.
  - /api/interpret: one LLM call (OpenAI, opt-in via OPENAI_API_KEY) that
    resolves the query's actual metric, dimensions, and a real start/end
    date range. Used by the "Ask AI" toggle in the UI, not on every
    keystroke.

Run:
    DBT_PROFILES_DIR=. python scripts/app.py
    open http://127.0.0.1:5001
"""
import csv
import datetime
import json
import os
import subprocess
import tempfile
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import llm_interpreter
from catalog import PROJECT_ROOT, guess_group_by
from search_index import SemanticSearchIndex

app = Flask(__name__)

print("Building catalog + loading embedding model (one-time startup cost)...")
INDEX = SemanticSearchIndex()
print(f"Ready: {len(INDEX.catalog)} catalog items indexed.")
print(f"LLM interpretation: {'available (OPENAI_API_KEY set)' if llm_interpreter.available() else 'unavailable (set OPENAI_API_KEY to enable)'}")


@app.get("/")
def home():
    return render_template("index.html", llm_available=llm_interpreter.available())


@app.get("/api/search")
def api_search():
    query = request.args.get("q", "").strip()
    top_k = int(request.args.get("top_k", 8))
    if not query:
        return jsonify([])

    ranked = INDEX.search(query, top_k=top_k)
    results = []
    for score, item in ranked:
        entry = {
            "type": item["type"],
            "name": item["name"],
            "description": item["description"],
            "score": round(score, 3),
        }
        if item["type"] == "metric":
            entry["group_by"] = guess_group_by(query, item["dimensions"])
            entry["available_dimensions"] = item["dimensions"]
        elif item["type"] == "dimension":
            entry["metrics"] = item["metrics"]
        elif item["type"] == "model":
            entry["columns"] = item["columns"]
        results.append(entry)
    return jsonify(results)


@app.get("/api/interpret")
def api_interpret():
    """LLM-backed: resolve a natural-language query into a concrete metric +
    group-by + date range. Falls back to the keyword heuristic (no date
    range) if OPENAI_API_KEY isn't set or the call fails."""
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "missing 'q' parameter"}), 400

    ranked = INDEX.search(query, top_k=8)
    metric_candidates = [item for _, item in ranked if item["type"] == "metric"][:4]
    if not metric_candidates:
        return jsonify({"mode": "none", "message": "No metric matched this query."})

    if llm_interpreter.available():
        try:
            result = llm_interpreter.interpret(query, metric_candidates, today=datetime.date.today())
            chosen = next(c for c in metric_candidates if c["name"] == result["metric"])
            return jsonify({
                "mode": "llm",
                "metric": chosen["name"],
                "description": chosen["description"],
                "available_dimensions": chosen["dimensions"],
                "group_by": result["group_by"],
                "start_date": result["start_date"],
                "end_date": result["end_date"],
                "filters": result["filters"],
            })
        except Exception as e:
            app.logger.warning("LLM interpretation failed: %s", e)
            # fall through to heuristic below

    best = metric_candidates[0]
    return jsonify({
        "mode": "heuristic",
        "metric": best["name"],
        "description": best["description"],
        "available_dimensions": best["dimensions"],
        "group_by": guess_group_by(query, best["dimensions"]),
        "start_date": None,
        "end_date": None,
        "filters": [],  # the keyword heuristic never guesses filter values -- too easy to get wrong silently
    })


@app.get("/api/query")
def api_query():
    metric = request.args.get("metric", "").strip()
    group_by = [g for g in request.args.get("group_by", "").split(",") if g]
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()
    try:
        filters = json.loads(request.args.get("filters", "[]"))
    except json.JSONDecodeError:
        return jsonify({"error": "malformed 'filters' parameter (expected JSON)"}), 400
    if not metric:
        return jsonify({"error": "missing 'metric' parameter"}), 400

    cmd = ["mf", "query", "--metrics", metric, "--quiet"]
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
        if "dimension" in f and "value" in f:
            cmd += ["--where", llm_interpreter.filter_to_where_clause(f)]

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tmp:
        csv_path = Path(tmp.name)
    cmd += ["--csv", str(csv_path)]

    result = subprocess.run(
        cmd, cwd=PROJECT_ROOT, capture_output=True, text=True,
        env={"DBT_PROFILES_DIR": ".", **os.environ},
    )

    if result.returncode != 0:
        csv_path.unlink(missing_ok=True)
        return jsonify({"error": result.stderr or result.stdout, "command": " ".join(cmd)}), 500

    with open(csv_path, newline="") as f:
        rows = list(csv.reader(f))
    csv_path.unlink(missing_ok=True)

    if not rows:
        return jsonify({"columns": [], "rows": [], "command": " ".join(cmd)})
    return jsonify({"columns": rows[0], "rows": rows[1:], "command": " ".join(cmd)})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False)
