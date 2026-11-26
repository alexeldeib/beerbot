"""Model token usage and estimated spend.

Every model response is counted in memory, then written to `model_usage` only while the database
is already awake: after each message and on the hourly sweep (docs/idle-compute.md). Costs use
list prices: update PRICES when they change. Claude Code runs on the owner's subscription and is
reported in the Actions logs instead.
"""

import logging
from calendar import monthrange
from datetime import UTC, date, datetime, timedelta

from .config import settings

logger = logging.getLogger(__name__)

# USD per 1M tokens: (in effect from, input, output (thinking included), cached input).
PRICES: dict[str, list[tuple[date, float, float, float]]] = {
    "gemini-3.6-flash": [
        (date(2026, 1, 1), 0.75, 3.75, 0.075),
        (date(2027, 1, 1), 1.50, 7.50, 0.15),
    ],
    "gemini-3.8-flash": [
        (date(2026, 1, 1), 0.75, 3.75, 0.075),
        (date(2027, 1, 1), 1.50, 7.50, 0.15),
    ],
    "gemini-3.5-flash-lite": [(date(2026, 1, 1), 0.30, 2.50, 0.03)],
    "gemini-3.1-flash-lite": [(date(2026, 1, 1), 0.25, 1.50, 0.025)],
    "gemini-2.5-flash-lite": [(date(2026, 1, 1), 0.10, 0.40, 0.01)],
    # Gemini 4 caches repeated prompt prefixes implicitly (no explicit caching).
    "gemini-4-hydrogen": [(date(2026, 1, 1), 1.50, 7.50, 0.075)],
    "gemini-4-neon": [(date(2026, 1, 1), 3.00, 15.00, 0.15)],
    "gemini-4-argon": [
        (date(2026, 1, 1), 2.00, 10.00, 0.10),
        (date(2026, 12, 1), 4.00, 20.00, 0.20),
    ],
    "gemini-4-carbon": [
        (date(2026, 1, 1), 2.00, 10.00, 0.10),
        (date(2026, 12, 1), 4.00, 20.00, 0.20),
    ],
}
FIELDS = ("calls", "input_tokens", "output_tokens", "cached_tokens")

# (day, source, model) -> counts not yet written; and totals since boot for /admin/workers/status.
_pending: dict[tuple[date, str, str], list[int]] = {}
SINCE_BOOT: dict[str, list[int]] = {}


def add(source: str, model: str, counts: list[int]) -> None:
    key = (datetime.now(UTC).date(), source, model)
    for totals in (_pending.setdefault(key, [0] * 4), SINCE_BOOT.setdefault(model, [0] * 4)):
        for i, n in enumerate(counts):
            totals[i] += n


def record(source: str, model: str, usage: object) -> None:
    """Count one model response: Google usage_metadata or an OpenAI-style usage object."""
    if usage is None:
        return

    def get(name: str) -> int:
        return getattr(usage, name, None) or 0

    if hasattr(usage, "prompt_token_count"):  # Google bills thinking as output.
        counts = [
            get("prompt_token_count"),
            get("candidates_token_count") + get("thoughts_token_count"),
            get("cached_content_token_count"),
        ]
    else:
        details = getattr(usage, "prompt_tokens_details", None)
        counts = [
            get("prompt_tokens"),
            get("completion_tokens"),
            getattr(details, "cached_tokens", None) or 0,
        ]
    add(source, model, [1, *counts])


async def flush(conn) -> None:
    """Write counted usage on a connection whose database is already awake. Never raises:
    counts that fail to write wait for the next flush."""
    while _pending:
        key, counts = _pending.popitem()
        try:
            await conn.execute(
                """INSERT INTO model_usage(day, source, model, calls, input_tokens,
                       output_tokens, cached_tokens)
                   VALUES($1, $2, $3, $4, $5, $6, $7)
                   ON CONFLICT(day, source, model) DO UPDATE SET
                       calls = model_usage.calls + EXCLUDED.calls,
                       input_tokens = model_usage.input_tokens + EXCLUDED.input_tokens,
                       output_tokens = model_usage.output_tokens + EXCLUDED.output_tokens,
                       cached_tokens = model_usage.cached_tokens + EXCLUDED.cached_tokens""",
                *key,
                *counts,
            )
        except Exception as exc:
            for i, n in enumerate(counts):  # Back into pending only: SINCE_BOOT already has it.
                _pending.setdefault(key, [0] * 4)[i] += n
            logger.warning("Model usage not written yet: %s", type(exc).__name__)
            return


def cost(model: str, day: date, input_tokens: int, output_tokens: int, cached: int) -> float | None:
    """List-price USD, or None for a model without known prices."""
    periods = [period for period in PRICES.get(model, []) if period[0] <= day]
    if not periods:
        return None
    _, per_input, per_output, per_cached = periods[-1]
    billed_input = (input_tokens - cached) * per_input + cached * per_cached
    return (billed_input + output_tokens * per_output) / 1_000_000


async def report(conn) -> dict:
    """This month by source, last month's total, and this month's projection against budget."""
    today = datetime.now(UTC).date()
    month = today.replace(day=1)
    last_month = (month - timedelta(days=1)).replace(day=1)
    rows = await conn.fetch(
        """SELECT day, source, model, calls, input_tokens, output_tokens, cached_tokens
           FROM model_usage WHERE day >= $1""",
        last_month,
    )
    by_source: dict[str, dict] = {}
    spent, previous, unpriced = 0.0, 0.0, set()
    for row in rows:
        usd = cost(row["model"], row["day"], *(row[f] for f in FIELDS[1:]))
        if usd is None:
            unpriced.add(row["model"])
            usd = 0.0
        if row["day"] < month:
            previous += usd
            continue
        spent += usd
        totals = by_source.setdefault(row["source"], dict.fromkeys(FIELDS, 0) | {"usd": 0.0})
        for field in FIELDS:
            totals[field] += row[field]
        totals["usd"] += usd
    days = monthrange(today.year, today.month)[1]
    projected = spent / today.day * days
    budget = settings.model_budget_usd
    return {
        "month": month.isoformat()[:7],
        "usd": round(spent, 2),
        "projected_usd": round(projected, 2),
        "budget_usd": budget,
        # A projection from the first few days is mostly noise.
        "over_budget": spent > budget or (today.day >= 7 and projected > budget),
        "last_month_usd": round(previous, 2),
        "by_source": {name: t | {"usd": round(t["usd"], 2)} for name, t in by_source.items()},
        "unpriced_models": sorted(unpriced),
        "prices": "Gemini list prices; gemini-3.6-flash doubles on 2027-01-01",
    }


def since_boot() -> dict:
    """Memory-only totals for /admin/workers/status (never touches the database)."""
    return {model: dict(zip(FIELDS, counts)) for model, counts in SINCE_BOOT.items()}
