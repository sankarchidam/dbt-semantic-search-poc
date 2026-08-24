"""
LLM-based query interpretation via the OpenAI API.

catalog.guess_group_by is a keyword heuristic: it scans the query for
dimension names and a handful of time words, and can only ever pick a
*granularity* ("group by month"), never a *range* ("only last month") or a
*filter to one value* ("just shampoo"). This module replaces that step with
a real language-understanding call, while keeping the model's choices
constrained to what's actually valid:

  - it only sees a shortlist of candidate metrics (from search_index.py's
    embedding search), each with its own real available dimensions
  - it can only choose group-by dimensions and filter dimensions that exist
    on the metric it picks
  - "today" is supplied explicitly, so relative phrases ("last month",
    "this quarter", "past 7 days") resolve to concrete calendar dates
    instead of a fuzzy granularity guess
  - filter values are matched case-insensitively with SQL wildcards
    (ILIKE), not exact equality -- the model rarely knows a dimension's
    exact stored spelling ("shampoo" vs the real value "Lavender Shampoo"),
    and a silently-empty exact match is worse than a fuzzy one
  - every dimension it returns (group-by or filter) is re-validated against
    ground truth after the call (defense in depth: even a well-constrained
    prompt can still hallucinate)

This is opt-in. If OPENAI_API_KEY isn't set, `available()` returns False and
callers should fall back to catalog.guess_group_by -- nothing here is a hard
dependency of the rest of the project. catalog.guess_group_by has no filter
equivalent: keyword-guessing a *value* to filter on is too easy to get wrong
silently, so the heuristic path stays filter-less by design.
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path


def _load_dotenv() -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments, no expansion.
    Doesn't override a variable already set in the real environment. Written
    by hand instead of depending on python-dotenv -- this project's .env
    needs are this simple, and it keeps the dependency list smaller."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()

_SCHEMA = {
    "name": "metric_query",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "metric": {
                "type": "string",
                "description": "The name of the single best-matching metric, chosen from the candidate list.",
            },
            "group_by": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Qualified dimension paths to group by, taken only from the chosen "
                    "metric's own available_dimensions. For the special 'metric_time' "
                    "dimension you may append a granularity, e.g. 'metric_time__month' "
                    "or 'metric_time__day'. Empty array if no breakdown was asked for."
                ),
            },
            "start_date": {
                "type": ["string", "null"],
                "description": "ISO 8601 date (YYYY-MM-DD) the time range starts, inclusive. Null if the query has no time constraint.",
            },
            "end_date": {
                "type": ["string", "null"],
                "description": "ISO 8601 date (YYYY-MM-DD) the time range ends, inclusive. Null if the query has no time constraint.",
            },
            "filters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "dimension": {
                            "type": "string",
                            "description": "A dimension from the chosen metric's own available_dimensions to restrict to one value.",
                        },
                        "value": {
                            "type": "string",
                            "description": (
                                "The value to match, as a SQL ILIKE pattern (case-insensitive). "
                                "Wrap in %...% for a casual/partial name mention (e.g. the user "
                                "said 'shampoo' -> value '%shampoo%', to match a real value like "
                                "'Lavender Shampoo'). Omit the %'s only for a value you're "
                                "confident is exact, like a status or region named outright."
                            ),
                        },
                    },
                    "required": ["dimension", "value"],
                    "additionalProperties": False,
                },
                "description": (
                    "Restrict results to a specific named value the user mentioned -- a product, "
                    "customer segment, region, status, category, etc. Only add a filter when the "
                    "query actually names a specific value to narrow to; do not add one for a "
                    "plain aggregate question. This is different from group_by: group_by breaks "
                    "results down by every value of a dimension, filters restrict to just one."
                ),
            },
        },
        "required": ["metric", "group_by", "start_date", "end_date", "filters"],
        "additionalProperties": False,
    },
}

MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")


def available() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def _dimension_is_valid(dim: str, valid_dims: set) -> bool:
    if dim in valid_dims:
        return True
    # metric_time accepts any granularity suffix (metric_time__month, __day, ...)
    if dim.startswith("metric_time__") and "metric_time" in valid_dims:
        return True
    return False


def filter_to_where_clause(filter_: dict) -> str:
    """Build one MetricFlow --where argument from a validated {dimension, value}
    filter. Always ILIKE (case-insensitive); the value carries its own %
    wildcards, or none for an exact-ish match. Single quotes in the value are
    escaped SQL-style (doubled) so a literal quote can't break out of the
    string or malform the query."""
    escaped_value = filter_["value"].replace("'", "''")
    return f"{{{{ Dimension('{filter_['dimension']}') }}}} ILIKE '{escaped_value}'"


def interpret(query: str, candidates: list[dict], today: datetime.date | None = None) -> dict:
    """
    candidates: metric-type items from search_index.SemanticSearchIndex.search()
                (each has name/description/dimensions). Pass the top few, not
                the whole catalog, to keep the prompt small and relevant.

    Returns {"metric": str, "group_by": [str], "start_date": str|None,
    "end_date": str|None, "filters": [{"dimension": str, "value": str}]}.
    Raises if the OpenAI call fails -- callers should catch and fall back to
    catalog.guess_group_by so a flaky/missing API key never breaks search.
    """
    from openai import OpenAI  # imported lazily so this module loads without the package installed

    today = today or datetime.date.today()
    client = OpenAI()

    catalog_text = "\n".join(
        f"- {c['name']}: {c['description']} | available_dimensions: {', '.join(c['dimensions']) or '(none)'}"
        for c in candidates
    )

    system = (
        f"Today's date is {today.isoformat()}. Translate the user's analytics question "
        f"into a structured query over the candidate metrics below.\n\n"
        f"Pick exactly one metric -- but pick it based on what the FULL question needs, not "
        f"just which name reads closest. If the question names a specific thing to filter or "
        f"group by (a product, category, channel, etc.), the metric you choose MUST have a "
        f"matching dimension in its own available_dimensions -- a dimension whose leaf name "
        f"(the part after the last '__') actually corresponds to that kind of value. For "
        f"example, a product name like 'shampoo' belongs on a dimension named product_name or "
        f"category, never on a dimension named status (status holds outcomes like "
        f"completed/cancelled/failed, not product names) or channel (channel holds a payment "
        f"processor name). If the closest-sounding metric by name doesn't have a dimension that "
        f"fits, and a different candidate metric does, choose that other candidate instead -- "
        f"even though its name matches the question less exactly, it is the one that can "
        f"actually answer it. Never force a filter onto a dimension that doesn't semantically "
        f"match the value, and never invent a metric or dimension not listed below.\n\n"
        f"group_by: include a dimension ONLY if the user's wording explicitly asks for a "
        f"breakdown, split, or comparison -- phrases like 'by region', 'per category', 'for "
        f"each segment', 'broken down by'. A plain question with no such phrase (e.g. 'total "
        f"revenue', 'how much did we make in July') gets an EMPTY group_by, even if a "
        f"dimension seems related or interesting -- do not add one just because it exists or "
        f"might be useful. When you do include dimensions, use ONLY names from that metric's "
        f"own available_dimensions below.\n\n"
        f"filters: restrict to ONE value of a dimension ONLY if the query names a specific "
        f"thing to narrow to -- a product, region, segment, status, or category mentioned by "
        f"name (e.g. 'shampoo sales', 'orders from the West', 'cancelled orders'). This is not "
        f"the same as group_by: a filter picks one value, group_by breaks down by all of them "
        f"-- never use both for the same idea in one query. Match filter values with SQL "
        f"wildcards (value wrapped in %...%) whenever the user's wording is a casual or partial "
        f"mention rather than the exact stored value -- 'shampoo' should become value "
        f"'%shampoo%' to match a real product name like 'Lavender Shampoo', not an exact-match "
        f"'shampoo' that would match nothing. Only skip the %'s for a value you are confident is "
        f"already exact, such as a region or status named outright.\n\n"
        f"start_date / end_date: resolve any relative time phrase (e.g. 'last month', 'this "
        f"year', 'past 7 days', 'year to date') into concrete dates. If the query names no "
        f"time constraint, leave both null.\n\n"
        f"Candidate metrics:\n{catalog_text}"
    )

    response = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": query},
        ],
        response_format={"type": "json_schema", "json_schema": _SCHEMA},
    )
    result = json.loads(response.choices[0].message.content)

    # Defense in depth: validate the model's choices against ground truth
    # before anything reaches MetricFlow.
    chosen = next((c for c in candidates if c["name"] == result["metric"]), candidates[0])
    valid_dims = set(chosen["dimensions"])
    result["metric"] = chosen["name"]
    result["group_by"] = [d for d in result["group_by"] if _dimension_is_valid(d, valid_dims)]
    result["filters"] = [
        f for f in result["filters"]
        if _dimension_is_valid(f["dimension"], valid_dims) and f["value"].strip()
    ]
    return result
